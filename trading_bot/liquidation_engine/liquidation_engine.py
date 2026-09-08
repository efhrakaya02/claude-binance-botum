"""
Orderbook & Liquidation Engine.

İki görevi var:
1) check_entry_safety(): işlem açmadan ÖNCE, giriş fiyatının tahmini
   likidasyon kümesine veya ters yönde iten büyük orderbook duvarına çok
   yakın olup olmadığını kontrol eder. Yakınsa işlem AÇILMAZ.
2) detect_sweep(): pozisyon AÇIKKEN sürekli çağrılır, ani likidasyon
   patlaması (stop-hunt) tespit ederse Position Manager'a haber verir ki
   hızlı kâr kilitleyip çıkabilsin.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import LiquidationConfig
from ..data_layer import DataLayer
from ..data_layer.models import OrderBookSnapshot
from .liquidation_model import LiquidationCluster, estimate_liquidation_clusters, nearest_cluster_distance_pct

# side eşlemesi: bizim pozisyon yönümüz -> bizi tehdit eden likidasyon kümesi tipi
# (LONG'daysak, LONG kümelerinin süpürülmesi bizi etkiler; SHORT'ta tersi)
_OWN_SIDE_CLUSTER = {"LONG": "LONG", "SHORT": "SHORT"}
# sweep tespitinde izlenecek Binance forceOrder yönü:
# LONG pozisyonu tehdit eden sweep = etraftaki LONG'ların force-SELL edilmesi
_SWEEP_LIQUIDATION_SIDE = {"LONG": "SELL", "SHORT": "BUY"}


@dataclass
class EntrySafetyResult:
    is_safe: bool
    reasons: list[str]
    nearest_liquidation_cluster: LiquidationCluster | None = None
    liquidation_distance_pct: float | None = None
    opposing_wall_price: float | None = None
    opposing_wall_distance_pct: float | None = None


@dataclass
class SweepAlert:
    symbol: str
    position_side: str
    triggered_liquidation_usdt: float
    window_seconds: float
    message: str


class LiquidationEngine:
    def __init__(self, data_layer: DataLayer, cfg: LiquidationConfig | None = None) -> None:
        self._data_layer = data_layer
        self._cfg = cfg or LiquidationConfig()

    # ------------------------------------------------------------------ #
    def check_entry_safety(self, symbol: str, side: str, entry_price: float) -> EntrySafetyResult:
        reasons: list[str] = []

        oi = self._data_layer.get_open_interest(symbol)
        funding = self._data_layer.get_funding_rate(symbol)
        ob = self._data_layer.get_orderbook(symbol)

        nearest_cluster = None
        distance_pct = None
        if oi is not None and funding is not None:
            clusters = estimate_liquidation_clusters(
                current_price=entry_price,
                open_interest_value_usdt=oi.open_interest_value,
                funding_rate=funding.last_funding_rate,
                cfg=self._cfg,
            )
            own_side = _OWN_SIDE_CLUSTER[side]
            nearest_cluster, distance_pct = nearest_cluster_distance_pct(clusters, entry_price, own_side)
            if distance_pct is not None and distance_pct < self._cfg.min_safe_distance_pct:
                reasons.append(
                    f"Tahmini {own_side} likidasyon kümesi çok yakın "
                    f"(%{distance_pct:.2f} mesafe, eşik %{self._cfg.min_safe_distance_pct})"
                )
        else:
            reasons.append("OI/funding verisi henüz yok — likidasyon kümesi tahmini atlandı")

        wall_price = None
        wall_distance_pct = None
        if ob is not None:
            wall_price, wall_distance_pct = self._find_opposing_wall(ob, side, entry_price)
            if wall_distance_pct is not None and wall_distance_pct < self._cfg.min_safe_distance_pct:
                reasons.append(
                    f"Ters yöne itebilecek büyük orderbook duvarı çok yakın "
                    f"(%{wall_distance_pct:.2f} mesafe, eşik %{self._cfg.min_safe_distance_pct})"
                )
        else:
            reasons.append("Orderbook verisi henüz yok")

        is_safe = not reasons or all("henüz yok" in r for r in reasons)
        # Not: veri eksikse temkinli davranıp "güvenli değil" de denebilir; burada
        # veri eksikliğini engelleyici saymıyoruz ama loglamak için reasons'ta tutuyoruz.
        blocking_reasons = [r for r in reasons if "henüz yok" not in r]
        is_safe = len(blocking_reasons) == 0

        return EntrySafetyResult(
            is_safe=is_safe,
            reasons=reasons,
            nearest_liquidation_cluster=nearest_cluster,
            liquidation_distance_pct=distance_pct,
            opposing_wall_price=wall_price,
            opposing_wall_distance_pct=wall_distance_pct,
        )

    def _find_opposing_wall(
        self, ob: OrderBookSnapshot, side: str, entry_price: float
    ) -> tuple[float | None, float | None]:
        """LONG için üstteki (ask) duvarlara, SHORT için alttaki (bid) duvarlara bakar —
        bunlar fiyatı bizim pozisyonumuzun TERSİ yönüne itebilecek likiditedir."""
        levels = ob.asks if side == "LONG" else ob.bids
        if not levels:
            return None, None

        avg_qty = sum(lv.quantity for lv in levels) / len(levels)
        if avg_qty == 0:
            return None, None

        for lv in levels:
            if lv.quantity >= avg_qty * self._cfg.orderbook_wall_multiplier:
                distance_pct = abs(lv.price - entry_price) / entry_price * 100
                return lv.price, distance_pct
        return None, None

    # ------------------------------------------------------------------ #
    def detect_sweep(self, symbol: str, position_side: str) -> SweepAlert | None:
        """Pozisyon açıkken periyodik çağrılır. Son `sweep_window_seconds`
        içinde bizi tehdit eden yönde biriken likidasyon hacmi eşiği aşarsa
        sweep alarmı döner."""
        target_side = _SWEEP_LIQUIDATION_SIDE[position_side]
        recent = self._data_layer.get_recent_liquidations(
            symbol, since_seconds=self._cfg.sweep_window_seconds
        )
        relevant = [ev for ev in recent if ev.side == target_side]
        total_usdt = sum(ev.quote_quantity for ev in relevant)

        if total_usdt >= self._cfg.sweep_liquidation_usdt_threshold:
            return SweepAlert(
                symbol=symbol,
                position_side=position_side,
                triggered_liquidation_usdt=total_usdt,
                window_seconds=self._cfg.sweep_window_seconds,
                message=(
                    f"{symbol}: son {self._cfg.sweep_window_seconds:.0f}sn içinde "
                    f"{total_usdt:,.0f} USDT'lik {target_side} likidasyonu — olası sweep"
                ),
            )
        return None
