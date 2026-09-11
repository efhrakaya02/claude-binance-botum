"""
Risk & Position Manager.

Bu modül SADECE karar mantığını içerir (state machine) — gerçek emir
gönderimi Execution Engine'in işi. Orchestrator, her fiyat güncellemesinde
`update_position_risk()` çağırır ve dönen RiskAction'a göre Execution
Engine'e stop/tp güncelle veya pozisyonu kapat talimatı verir.

Uygulanan kurallar (üç fazlı ATR bazlı stop mantığı):
- Faz A (0% -> trailing_activate_pct=%1.5): SABİT, ATR bazlı başlangıç stop'u
  (entry_price ± ATR*atr_multiplier) — coin'in kendi volatilitesine göre.
- Faz B (%1.5 -> breakeven_trigger_pct=%2.5): ATR bazlı TRAILING stop — anlık
  fiyatı ATR*atr_multiplier mesafeden takip eder, sadece lehte günceller.
- Faz C (%2.5'ten itibaren): stop en az breakeven'a zorlanır, ardından
  "stop = zirvenin trailing_lock_ratio (%60) kadarı" formülüyle kâr kademe
  kademe kilitlenir — zirve yükseldikçe stop da yükselir, asla geri gitmez.
  (Örn: zirve tam %3'e ulaştığında stop = entry + %3*0.6 = kârın %60'ı kilitli.)
- Runway (momentum) devam ettiği sürece TP de zirveyle birlikte yükselir;
  tersine dönüş sinyali geldiğinde en yüksek kazançla market'ten kapatılır.
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
        """Slotlar doluyken daha büyük bir fırsat çıkarsa kapatılacak aday:
        henüz breakeven'a bile ulaşmamış (en zayıf durumdaki) pozisyon."""
        candidates = [p for p in self.open_positions.values() if not p.breakeven_triggered]
        if not candidates:
            # hepsi breakeven'ı geçmişse, trailing'i en az ilerlemiş olanı seç
            candidates = list(self.open_positions.values())
        if not candidates:
            return None
        return min(candidates, key=lambda p: p.peak_favorable_price or p.entry_price)

    def open_position(
        self, symbol: str, side: str, entry_price: float, quantity: float, atr: float
    ) -> Position:
        position = Position(
            symbol=symbol,
            side=side,
            entry_price=entry_price,
            quantity=quantity,
            margin_usdt=self._cfg.margin_per_position_usdt,
            leverage=self._cfg.max_leverage,
            atr=atr,
        )
        # Faz A: breakeven/kâr kilitleme fazlarına ulaşılana kadar korumasız
        # kalmasın diye AÇILIŞTA hemen ATR bazlı bir başlangıç stop'u konuyor.
        # Sabit bir yüzde yerine coin'in kendi volatilitesine göre ayarlanır.
        direction = 1 if position.is_long else -1
        position.stop_price = entry_price - direction * atr * self._cfg.atr_multiplier

        self.open_positions[symbol] = position
        logger.info(
            "Pozisyon açıldı: %s %s @ %s (ATR=%s, başlangıç stop=%s)",
            symbol, side, entry_price, atr, position.stop_price,
        )
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

        # --- Zirve takibi (trailing aktif olsun olmasın HER tick'te) ---------
        # "En yüksek PNL" raporlaması bu alana dayanıyor; trailing mantığından
        # bağımsız olarak en baştan itibaren güncellenir.
        assert position.peak_favorable_price is not None
        is_new_peak = (
            current_price > position.peak_favorable_price
            if position.is_long
            else current_price < position.peak_favorable_price
        )
        if is_new_peak:
            position.peak_favorable_price = current_price

        # --- Faz B: ATR bazlı trailing (trailing_activate_pct -> breakeven_trigger_pct) ---
        if self._cfg.trailing_activate_pct <= raw < self._cfg.breakeven_trigger_pct:
            if not position.trailing_active:
                position.trailing_active = True
                logger.info("%s: ATR trailing (Faz B) aktif oldu", position.symbol)

            # Stop, anlık fiyatı ATR*atr_multiplier mesafeden takip eder —
            # ama SADECE lehte yönde günceller, asla geri gitmez.
            atr_distance = position.atr * self._cfg.atr_multiplier
            candidate_stop = current_price - direction * atr_distance
            if position.stop_price is None or (
                candidate_stop > position.stop_price if position.is_long else candidate_stop < position.stop_price
            ):
                position.stop_price = candidate_stop
                action.update_stop = position.stop_price

        # --- Faz C: breakeven zorlaması + kademeli kâr kilitleme (breakeven_trigger_pct'ten itibaren) ---
        if raw >= self._cfg.breakeven_trigger_pct:
            if not position.trailing_active:
                position.trailing_active = True
            if not position.breakeven_triggered:
                position.breakeven_triggered = True
                logger.info("%s: breakeven zorlaması + kâr kilitleme (Faz C) başladı", position.symbol)

            # Stop = zirvenin (girişten itibaren kat edilen en yüksek ham fiyat
            # mesafesinin) trailing_lock_ratio kadarı. Zirve sadece yükselebildiği
            # için (yukarıdaki takip bloğu) bu da yalnızca yükselir. Ayrıca hiçbir
            # zaman breakeven'ın (entry_price) altına düşmez.
            peak_move = abs(position.peak_favorable_price - position.entry_price)
            locked_distance = peak_move * self._cfg.trailing_lock_ratio
            candidate_stop = position.entry_price + direction * locked_distance
            floor_stop = position.entry_price  # breakeven — asla bunun altı olmaz
            new_stop = candidate_stop if (
                candidate_stop > floor_stop if position.is_long else candidate_stop < floor_stop
            ) else floor_stop

            if position.stop_price is None or (
                new_stop > position.stop_price if position.is_long else new_stop < position.stop_price
            ):
                position.stop_price = new_stop
                action.update_stop = position.stop_price

            # TP, runway devam ettikçe zirveyle birlikte uzatılır (yumuşak hedef;
            # gerçek kapanış tetikleyicisi reversal_signal'dır).
            position.tp_price = position.peak_favorable_price + direction * peak_move * 0.5
            action.update_tp = position.tp_price

        # --- Zirve / tersine dönüş: en yüksek kazançla kapat -----------------
        if position.trailing_active and reversal_signal:
            action.close_position = True
            action.close_reason = "reversal_signal (zirve tespit edildi)"

        # --- Dahili stop/TP geçiş kontrolü -----------------------------------
        # KRİTİK: Buraya kadar hesaplanan stop_price/tp_price sadece Binance'e
        # gönderilen bir emrin fiyatı. Eğer o emir gönderilemediyse (API hatası,
        # dry-run modu, vb.) fiyat o seviyeyi geçtiğinde HİÇBİR ŞEY pozisyonu
        # kapatmaz. Bu yüzden botun kendisi de, dış emirden tamamen bağımsız
        # olarak, her tick'te "fiyat kendi stop/TP'mi geçti mi" diye kontrol
        # ediyor ve gerekiyorsa kapatma kararı veriyor.
        if not action.close_position and position.stop_price is not None:
            stop_hit = current_price <= position.stop_price if position.is_long else current_price >= position.stop_price
            if stop_hit:
                action.close_position = True
                action.close_reason = f"stop_price_reached (dahili kontrol, stop={position.stop_price:.8g})"
                logger.warning(
                    "%s: fiyat stop seviyesini geçti (dahili kontrol) stop=%s anlık=%s",
                    position.symbol, position.stop_price, current_price,
                )

        if not action.close_position and position.tp_price is not None and position.trailing_active:
            tp_hit = current_price >= position.tp_price if position.is_long else current_price <= position.tp_price
            if tp_hit:
                action.close_position = True
                action.close_reason = f"tp_price_reached (dahili kontrol, tp={position.tp_price:.8g})"

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

    def reenter(self, symbol: str, entry_price: float, quantity: float, atr: float) -> Position:
        tracked = self.tracked_opportunities.pop(symbol, None)
        position = self.open_position(symbol, tracked.side if tracked else "LONG", entry_price, quantity, atr)
        position.reentry_count = (tracked.original_position.reentry_count + 1) if tracked else 1
        logger.info("%s: fırsat kaldığı yerden devam ediyor, yeniden giriş #%d", symbol, position.reentry_count)
        return position

    # ------------------------------------------------------------------ #
    def _unrealized_pnl_usdt(self, position: Position, price: float) -> float:
        direction = 1 if position.is_long else -1
        price_diff = (price - position.entry_price) * direction
        notional = position.margin_usdt * position.leverage
        return (price_diff / position.entry_price) * notional
