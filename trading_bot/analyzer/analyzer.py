"""
Çoklu zaman dilimi analiz motoru.

Akış (kullanıcının istediği sırayla):
  4h  -> makro piyasa yönü (Trend.UP / Trend.DOWN / Trend.RANGE)
  1h  -> işlem açma kararı: 1h yapısı da makro yönle uyumlu mu + BOS var mı
  15m/5m/1m -> hacim anomalisi + momentum ile giriş zamanlaması

Sadece price action kullanılır (RSI/MACD/Bollinger vb. YOK).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..data_layer import DataLayer
from . import price_action as pa
from .price_action import Trend

logger = logging.getLogger(__name__)


@dataclass
class Signal:
    symbol: str
    side: str | None                # "LONG" / "SHORT" / None (sinyal yok)
    macro_trend: Trend
    macro_confirmed: bool           # 4h yön net mi (RANGE değil mi)
    entry_confirmed: bool           # 1h yapı makro yönle uyumlu + BOS var
    timing_confirmed: bool          # 15m/5m/1m hacim+momentum onayı
    suggested_entry_price: float | None
    confidence: float               # 0-100
    reasons: list[str] = field(default_factory=list)

    @property
    def is_actionable(self) -> bool:
        return (
            self.side is not None
            and self.macro_confirmed
            and self.entry_confirmed
            and self.timing_confirmed
        )


class MultiTimeframeAnalyzer:
    def __init__(self, data_layer: DataLayer) -> None:
        self._data_layer = data_layer

    def analyze(self, symbol: str) -> Signal | None:
        candles_4h = self._data_layer.get_klines(symbol, "4h")
        candles_1h = self._data_layer.get_klines(symbol, "1h")
        candles_15m = self._data_layer.get_klines(symbol, "15m")
        candles_5m = self._data_layer.get_klines(symbol, "5m")
        candles_1m = self._data_layer.get_klines(symbol, "1m")

        if not all([candles_4h, candles_1h, candles_15m, candles_5m, candles_1m]):
            return None  # henüz yeterli veri toplanmadı (sembol yeni eklenmiş olabilir)

        reasons: list[str] = []

        # ---- 4h: makro yön -------------------------------------------------
        swings_4h = pa.find_swing_points(candles_4h, lookback=2)
        macro_trend = pa.determine_trend(swings_4h)
        macro_confirmed = macro_trend != Trend.RANGE
        if not macro_confirmed:
            return Signal(
                symbol=symbol, side=None, macro_trend=macro_trend, macro_confirmed=False,
                entry_confirmed=False, timing_confirmed=False, suggested_entry_price=None,
                confidence=0.0, reasons=["4h yapı RANGE — net makro yön yok"],
            )
        side = "LONG" if macro_trend == Trend.UP else "SHORT"
        reasons.append(f"4h makro yön: {macro_trend.value}")

        # ---- 1h: işlem açma kararı -----------------------------------------
        swings_1h = pa.find_swing_points(candles_1h, lookback=2)
        trend_1h = pa.determine_trend(swings_1h)
        structure_break_1h = pa.detect_structure_break(candles_1h, swings_1h, macro_trend)

        aligned_1h = trend_1h == macro_trend
        bos_confirms = structure_break_1h is not None and structure_break_1h.kind == "BOS"
        entry_confirmed = aligned_1h and bos_confirms
        if aligned_1h:
            reasons.append("1h yapı makro yönle uyumlu")
        if structure_break_1h is not None:
            reasons.append(f"1h {structure_break_1h.kind} tespit edildi ({structure_break_1h.direction.value})")

        # ---- 15m/5m/1m: hacim + momentum ile zamanlama ----------------------
        timing_votes = 0
        for tf_candles, tf_name in ((candles_15m, "15m"), (candles_5m, "5m"), (candles_1m, "1m")):
            vol_ratio = pa.volume_anomaly_ratio(tf_candles)
            roc = pa.momentum_roc(tf_candles, periods=5)
            momentum_aligned = (roc > 0 and side == "LONG") or (roc < 0 and side == "SHORT")
            if vol_ratio >= 1.5 and momentum_aligned:
                timing_votes += 1
                reasons.append(
                    f"{tf_name}: hacim anomalisi x{vol_ratio:.1f}, momentum {roc:+.2f}% (yönle uyumlu)"
                )
        timing_confirmed = timing_votes >= 2  # 3 zaman diliminden en az 2'si onaylamalı

        suggested_entry_price = candles_1m[-1].close if candles_1m else None

        confidence = self._compute_confidence(
            macro_confirmed=macro_confirmed,
            entry_confirmed=entry_confirmed,
            timing_votes=timing_votes,
        )

        return Signal(
            symbol=symbol,
            side=side,
            macro_trend=macro_trend,
            macro_confirmed=macro_confirmed,
            entry_confirmed=entry_confirmed,
            timing_confirmed=timing_confirmed,
            suggested_entry_price=suggested_entry_price,
            confidence=confidence,
            reasons=reasons,
        )

    @staticmethod
    def _compute_confidence(macro_confirmed: bool, entry_confirmed: bool, timing_votes: int) -> float:
        score = 0.0
        if macro_confirmed:
            score += 30
        if entry_confirmed:
            score += 35
        score += timing_votes * (35 / 3)
        return round(min(score, 100.0), 1)
