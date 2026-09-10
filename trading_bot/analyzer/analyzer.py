"""
Çoklu zaman dilimi analiz motoru.

Akış (kullanıcının istediği sırayla):
  4h  -> makro piyasa yönü (Trend.UP / Trend.DOWN / Trend.RANGE) — KATI kapı
  1h  -> işlem açma kararı: 1h yapısı makro yönle uyumlu mu + BOS var mı — KADEMELİ
  15m/5m/1m -> hacim anomalisi + momentum ile giriş zamanlaması — KADEMELİ

Sadece price action kullanılır (RSI/MACD/Bollinger vb. YOK).

KADEMELİ PUANLAMA NEDEN: Eskiden 1h onayı "uyum VE BOS" (ikisi de zorunlu),
zamanlama ise "3 zaman diliminden en az 2'si tam eşiği geçmeli" şeklinde katı
(all-or-nothing) eşiklerdi. Bu, sınırın hemen altında kalan (örn. hacim 1.48x,
eşik 1.5x) ama aslında geçerli olan kurulumları tamamen reddediyordu — özellikle
"hareketin en başında" yakalamak isteyen bir stratejide bu ciddi bir kayıp.
Artık her bileşen 0 ile kendi ağırlığı arasında SÜREKLİ bir skor üretiyor ve
nihai karar (`is_actionable`) toplam `confidence`'ın bir eşiği (varsayılan 60)
geçip geçmediğine bakıyor. Böylece bir bileşendeki sınır-altı zayıflık, diğer
bileşenlerdeki güçle telafi edilebiliyor — ama hâlâ gerçek kanıt (yön uyumu,
hacim, momentum yönü) şart; hiçbir gereksinim tamamen kaldırılmadı.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..config import AnalyzerConfig
from ..data_layer import DataLayer
from . import price_action as pa
from .price_action import Trend

logger = logging.getLogger(__name__)


@dataclass
class Signal:
    symbol: str
    side: str | None                # "LONG" / "SHORT" / None (sinyal yok)
    macro_trend: Trend
    macro_confirmed: bool           # 4h yön net mi (RANGE değil mi) — katı kapı
    entry_confirmed: bool           # 1h uyum + BOS'un İKİSİ de var mı (bilgi amaçlı, tam onay)
    timing_confirmed: bool          # zamanlama skoru ağırlığının çoğunu topladı mı (bilgi amaçlı)
    entry_score: float              # 0..cfg.entry_weight — 1h bileşeninin kademeli puanı
    timing_score: float             # 0..cfg.timing_weight — zamanlama bileşeninin kademeli puanı
    suggested_entry_price: float | None
    confidence: float               # 0-100, tüm bileşenlerin toplamı
    is_actionable: bool             # confidence >= actionable_confidence_threshold (VE side/macro var)
    reasons: list[str] = field(default_factory=list)


class MultiTimeframeAnalyzer:
    def __init__(self, data_layer: DataLayer, cfg: AnalyzerConfig | None = None) -> None:
        self._data_layer = data_layer
        self._cfg = cfg or AnalyzerConfig()

    def analyze(self, symbol: str) -> Signal | None:
        candles_4h = self._data_layer.get_klines(symbol, "4h")
        candles_1h = self._data_layer.get_klines(symbol, "1h")
        candles_15m = self._data_layer.get_klines(symbol, "15m")
        candles_5m = self._data_layer.get_klines(symbol, "5m")
        candles_1m = self._data_layer.get_klines(symbol, "1m")

        if not all([candles_4h, candles_1h, candles_15m, candles_5m, candles_1m]):
            return None  # henüz yeterli veri toplanmadı (sembol yeni eklenmiş olabilir)

        reasons: list[str] = []

        # ---- 4h: makro yön — bu hâlâ KATI bir kapı --------------------------
        # (Yön belirsizken "kısmi" bir LONG/SHORT sinyali üretmenin bir anlamı
        # yok — hangi yöne kısmi güveneceğimizi bile bilemeyiz.)
        swings_4h = pa.find_swing_points(candles_4h, lookback=2)
        macro_trend = pa.determine_trend(swings_4h)
        macro_confirmed = macro_trend != Trend.RANGE
        if not macro_confirmed:
            return Signal(
                symbol=symbol, side=None, macro_trend=macro_trend, macro_confirmed=False,
                entry_confirmed=False, timing_confirmed=False, entry_score=0.0, timing_score=0.0,
                suggested_entry_price=None, confidence=0.0, is_actionable=False,
                reasons=["4h yapı RANGE — net makro yön yok"],
            )
        side = "LONG" if macro_trend == Trend.UP else "SHORT"
        reasons.append(f"4h makro yön: {macro_trend.value}")

        # ---- 1h: işlem açma kararı — KADEMELİ ------------------------------
        swings_1h = pa.find_swing_points(candles_1h, lookback=2)
        trend_1h = pa.determine_trend(swings_1h)
        structure_break_1h = pa.detect_structure_break(candles_1h, swings_1h, macro_trend)

        aligned_1h = trend_1h == macro_trend
        bos_confirms = structure_break_1h is not None and structure_break_1h.kind == "BOS"
        entry_confirmed = aligned_1h and bos_confirms  # bilgi amaçlı: TAM onay var mı

        # İkisi de varsa tam ağırlık, sadece biri varsa yarı yarıya — böylece
        # "henüz tam trend oluşmadı ama taze BOS var" gibi erken-yakalama
        # senaryoları da kısmi puan alıyor (eskiden tamamen reddediliyordu).
        half = self._cfg.entry_weight / 2
        entry_score = (half if aligned_1h else 0.0) + (half if bos_confirms else 0.0)

        if aligned_1h:
            reasons.append("1h yapı makro yönle uyumlu")
        if structure_break_1h is not None:
            reasons.append(f"1h {structure_break_1h.kind} tespit edildi ({structure_break_1h.direction.value})")
        if entry_score < self._cfg.entry_weight:
            reasons.append(f"1h onayı kısmi (skor {entry_score:.0f}/{self._cfg.entry_weight:.0f})")

        # ---- 15m/5m/1m: hacim + momentum ile zamanlama — KADEMELİ ----------
        per_tf_weight = self._cfg.timing_weight / 3
        timing_score = 0.0
        confirmed_tf_count = 0
        for tf_candles, tf_name in ((candles_15m, "15m"), (candles_5m, "5m"), (candles_1m, "1m")):
            vol_ratio = pa.volume_anomaly_ratio(tf_candles)
            roc = pa.momentum_roc(tf_candles, periods=5)
            momentum_aligned = (roc > 0 and side == "LONG") or (roc < 0 and side == "SHORT")

            if not momentum_aligned:
                # Yön ters ise bu zaman diliminden puan yok — momentum yönü
                # hâlâ katı bir alt-kapı (aksi halde ters sinyale de puan
                # vermiş oluruz, bu kaliteyi değil miktarı artırır).
                continue

            # Hacim oranı hedefin ALTINDA kalsa bile ORANTILI kısmi puan
            # veriliyor — eskiden 1.49x bile 0 sayılıyordu, artık 1.49/1.5
            # oranında (neredeyse tam) puan alıyor.
            ratio_score = min(vol_ratio / self._cfg.timing_target_volume_ratio, 1.0)
            tf_score = ratio_score * per_tf_weight
            timing_score += tf_score

            if ratio_score >= 1.0:
                confirmed_tf_count += 1
            reasons.append(
                f"{tf_name}: hacim anomalisi x{vol_ratio:.2f} (hedef x{self._cfg.timing_target_volume_ratio:.1f}), "
                f"momentum {roc:+.2f}% (yönle uyumlu) -> {tf_score:.1f}/{per_tf_weight:.1f} puan"
            )

        timing_confirmed = confirmed_tf_count >= 2  # bilgi amaçlı: eski davranışa denk gelen özet

        suggested_entry_price = candles_1m[-1].close if candles_1m else None

        confidence = round(
            min(self._cfg.macro_weight + entry_score + timing_score, 100.0), 1
        )
        is_actionable = confidence >= self._cfg.actionable_confidence_threshold

        return Signal(
            symbol=symbol,
            side=side,
            macro_trend=macro_trend,
            macro_confirmed=macro_confirmed,
            entry_confirmed=entry_confirmed,
            timing_confirmed=timing_confirmed,
            entry_score=round(entry_score, 1),
            timing_score=round(timing_score, 1),
            suggested_entry_price=suggested_entry_price,
            confidence=confidence,
            is_actionable=is_actionable,
            reasons=reasons,
        )
