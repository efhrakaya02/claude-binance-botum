"""
Risk & Position Manager.

Bu modül SADECE karar mantığını içerir (state machine) — gerçek emir
gönderimi Execution Engine'in işi. Orchestrator, her fiyat güncellemesinde
`update_position_risk()` çağırır ve dönen RiskAction'a göre Execution
Engine'e stop/tp güncelle veya pozisyonu kapat talimatı verir.

Uygulanan kurallar (kullanıcının tarif ettiği mantık):
- Ham fiyatta +%1 hareket -> stop breakeven'a çekilir (risk sıfırlanır).
- Ham fiyatta +%1.5 hareket -> trailing stop devreye girer.
- Trailing aktifken her +%0.3 ham fiyat artışında, o adımda oluşan kazancın
  YARISI stop'a kilitlenir (stop o kadar yükselir).
- Runway (momentum) devam ettiği sürece TP de stop ile birlikte yükselir;
  tersine dönüş sinyali geldiğinde en yüksek kazançla kapatılır.
- Short pozisyonlarda aynı mantık ters yönde uygulanır.

NOT: "Zirve tespiti" burada, dışarıdan (Analyzer'dan) gelen bir
`reversal_signal: bool` parametresiyle temsil ediliyor — gerçek zirve
tahmini asla kesin olmaz, bu yüzden 1m/5m'de pozisyon yönünün TERSİNE bir
CHoCH (character break) veya momentum kaybı Analyzer tarafından tespit
edildiğinde bu bayrak True gönderilir.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..config import RiskConfig
from .position import Position, PositionStatus

logger = logging.getLogger(__name__)


@dataclass
class RiskAction:
    update_stop: float | None = None
    update_tp: float | None = None
    close_position: bool = False
    close_reason: str | None = None


@dataclass
class TrackedOpportunity:
    """Sweep sonrası kapatılan ama fiyat hareketi devam ederse yeniden
    girilmek üzere izlemeye alınan fırsat."""
    symbol: str
    side: str
    original_position: Position
    tracked_since_ms: int


class PositionManager:
    def __init__(self, cfg: RiskConfig | None = None) -> None:
        self._cfg = cfg or RiskConfig()
        self.open_positions: dict[str, Position] = {}
        self.tracked_opportunities: dict[str, TrackedOpportunity] = {}

    # ------------------------------------------------------------------ #
    # Kapasite / slot yönetimi
    # ------------------------------------------------------------------ #
    @property
    def open_count(self) -> int:
        return len(self.open_positions)

    def has_free_slot(self) -> bool:
        return self.open_count < self._cfg.max_concurrent_positions

    def find_riskiest_position(self) -> Position | None:
        """3 slot doluyken daha büyük bir fırsat çıkarsa kapatılacak aday:
        henüz breakeven'a bile ulaşmamış (en zayıf durumdaki) pozisyon."""
        candidates = [p for p in self.open_positions.values() if not p.breakeven_triggered]
        if not candidates:
            # hepsi breakeven'ı geçmişse, trailing'i en az ilerlemiş olanı seç
            candidates = list(self.open_positions.values())
        if not candidates:
            return None
        return min(candidates, key=lambda p: p.peak_favorable_price or p.entry_price)

    def open_position(
        self, symbol: str, side: str, entry_price: float, quantity: float
    ) -> Position:
        position = Position(
            symbol=symbol,
            side=side,
            entry_price=entry_price,
            quantity=quantity,
            margin_usdt=self._cfg.margin_per_position_usdt,
            leverage=self._cfg.max_leverage,
        )
        self.open_positions[symbol] = position
        logger.info("Pozisyon açıldı: %s %s @ %s", symbol, side, entry_price)
        return position

    def close_position(self, symbol: str, close_price: float, reason: str) -> Position | None:
        position = self.open_positions.pop(symbol, None)
        if position is None:
            return None
        position.status = PositionStatus.CLOSED
        position.realized_pnl_usdt = self._unrealized_pnl_usdt(position, close_price)
        logger.info("Pozisyon kapatıldı: %s (%s) pnl=%.2f USDT", symbol, reason, position.realized_pnl_usdt)
        return position

    # ------------------------------------------------------------------ #
    # Breakeven + trailing mantığı
    # ------------------------------------------------------------------ #
    def raw_move_pct(self, position: Position, current_price: float) -> float:
        """Kaldıraçsız, ham fiyat hareketi yüzdesi (pozisyon yönünde pozitif)."""
        raw = (current_price - position.entry_price) / position.entry_price * 100
        return raw if position.is_long else -raw

    def update_position_risk(
        self, position: Position, current_price: float, reversal_signal: bool = False
    ) -> RiskAction:
        action = RiskAction()
        raw = self.raw_move_pct(position, current_price)
        direction = 1 if position.is_long else -1

        # --- Breakeven ----------------------------------------------------
        if not position.breakeven_triggered and raw >= self._cfg.breakeven_trigger_pct:
            position.stop_price = position.entry_price
            position.breakeven_triggered = True
            action.update_stop = position.stop_price
            logger.info("%s: breakeven tetiklendi, stop=%s", position.symbol, position.stop_price)

        # --- Trailing aktivasyonu ------------------------------------------
        if raw >= self._cfg.trailing_activate_pct:
            if not position.trailing_active:
                position.trailing_active = True
                position.last_trail_price = position.entry_price * (
                    1 + direction * self._cfg.trailing_activate_pct / 100
                )
                position.peak_favorable_price = current_price
                logger.info("%s: trailing aktif oldu", position.symbol)

            # zirve takibi
            is_new_peak = (
                current_price > position.peak_favorable_price
                if position.is_long
                else current_price < position.peak_favorable_price
            )
            if is_new_peak:
                position.peak_favorable_price = current_price

            # adım adım stop yükseltme: her %step ham fiyat ilerlemesinde
            # kazancın yarısını kilitle
            step_price_size = position.entry_price * (self._cfg.trailing_step_pct / 100)
            lock_price_size = step_price_size * self._cfg.trailing_lock_ratio

            assert position.last_trail_price is not None
            guard = 0
            while guard < 1000:  # sonsuz döngü koruması
                next_step_price = position.last_trail_price + direction * step_price_size
                reached = current_price >= next_step_price if position.is_long else current_price <= next_step_price
                if not reached:
                    break
                new_stop = (position.stop_price or position.entry_price) + direction * lock_price_size
                position.stop_price = new_stop
                position.last_trail_price = next_step_price
                action.update_stop = position.stop_price
                guard += 1

            # TP, runway devam ettikçe zirveyle birlikte uzatılır (yumuşak hedef;
            # gerçek kapanış tetikleyicisi reversal_signal'dır).
            runway = abs(position.peak_favorable_price - position.entry_price)
            position.tp_price = position.peak_favorable_price + direction * runway * 0.5
            action.update_tp = position.tp_price

        # --- Zirve / tersine dönüş: en yüksek kazançla kapat -----------------
        if position.trailing_active and reversal_signal:
            action.close_position = True
            action.close_reason = "reversal_signal (zirve tespit edildi)"

        return action

    # ------------------------------------------------------------------ #
    # Sweep sonrası hızlı çıkış + yeniden giriş döngüsü
    # ------------------------------------------------------------------ #
    def handle_sweep_exit(self, position: Position, current_price: float) -> Position:
        """Sweep tespit edildiğinde çağrılır: kâr varsa hemen kilitleyip kapatır,
        fırsatı TRACKING'e alır ki fiyat kaldığı yerden devam ederse tekrar girilsin."""
        self.close_position(position.symbol, current_price, reason="sweep_exit")
        position.status = PositionStatus.TRACKING
        self.tracked_opportunities[position.symbol] = TrackedOpportunity(
            symbol=position.symbol,
            side=position.side,
            original_position=position,
            tracked_since_ms=position.closed_at_ms or 0,
        )
        logger.info("%s: sweep sonrası TRACKING'e alındı, düzeltme sonrası yeniden giriş bekleniyor", position.symbol)
        return position

    def should_reenter(self, symbol: str, current_price: float, resumed_signal: bool) -> bool:
        """Analyzer, hareketin kaldığı yerden devam ettiğini (BOS tekrar aynı yönde)
        tespit ettiğinde resumed_signal=True gönderir; bu durumda yeniden giriş uygundur."""
        tracked = self.tracked_opportunities.get(symbol)
        return tracked is not None and resumed_signal

    def reenter(self, symbol: str, entry_price: float, quantity: float) -> Position:
        tracked = self.tracked_opportunities.pop(symbol, None)
        position = self.open_position(symbol, tracked.side if tracked else "LONG", entry_price, quantity)
        position.reentry_count = (tracked.original_position.reentry_count + 1) if tracked else 1
        logger.info("%s: fırsat kaldığı yerden devam ediyor, yeniden giriş #%d", symbol, position.reentry_count)
        return position

    # ------------------------------------------------------------------ #
    def _unrealized_pnl_usdt(self, position: Position, price: float) -> float:
        direction = 1 if position.is_long else -1
        price_diff = (price - position.entry_price) * direction
        notional = position.margin_usdt * position.leverage
        return (price_diff / position.entry_price) * notional
