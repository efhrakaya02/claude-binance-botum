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

    @property
    def cfg(self) -> AnalyzerConfig:
        return self._cfg

    def timing_score_for(self, symbol: str, side: str) -> float:
        """15m/5m/1m hacim+momentum skorunu TEK BAŞINA hesaplar (0..timing_weight).
        Tam bir analiz (4h/1h) gerektirmez — pozisyon açıkken "momentum hâlâ
        canlı mı" diye sürekli kontrol etmek veya izlenen bir fırsata yeniden
        girip girmeyeceğine karar vermek için kullanılır."""
        candles_15m = self._data_layer.get_klines(symbol, "15m")
        candles_5m = self._data_layer.get_klines(symbol, "5m")
        candles_1m = self._data_layer.get_klines(symbol, "1m")
        if not all([candles_15m, candles_5m, candles_1m]):
            return 0.0
        score, _ = _compute_timing_score(
            [(candles_15m, "15m"), (candles_5m, "5m"), (candles_1m, "1m")], side, self._cfg
        )
        return score

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
        timing_score, confirmed_tf_count = _compute_timing_score(
            [(candles_15m, "15m"), (candles_5m, "5m"), (candles_1m, "1m")], side, self._cfg, reasons
        )
        timing_confirmed = confirmed_tf_count >= 2  # bilgi amaçlı: eski davranışa denk gelen özet

        # ---- Genişleme (extension) kontrolü — MOMENTUM'A BAĞLI kapı ----------
        # Eskiden bu kontrol TEK BAŞINA (momentumdan bağımsız) reddediyordu —
        # bu, "%2-3 pompalayıp tükenen" kurulumla "%90-130 devam eden gerçek
        # mega-trend"i AYNI KEFEYE koyuyordu; ikincisi tam da botun yakalamak
        # için var olduğu şey. Artık genişleme TEK BAŞINA yeterli değil: sadece
        # hem UZAK hem de momentum ZAYIFLAMIŞSA (timing_score düşükse) "geç
        # kalınmış/tükenmiş" sayılıp reddediliyor. Momentum hâlâ güçlüyse
        # (hacim/momentum kanıtı sürüyorsa) uzaklık tek başına engel değil —
        # hareket hâlâ devam ediyor demektir, tam da binmek istediğimiz şey.
        last_low_1h = next((s for s in reversed(swings_1h) if s.type == pa.SwingType.LOW), None)
        last_high_1h = next((s for s in reversed(swings_1h) if s.type == pa.SwingType.HIGH), None)
        current_close = candles_1h[-1].close

        if side == "LONG" and last_low_1h is not None and last_low_1h.price > 0:
            extension_pct = (current_close - last_low_1h.price) / last_low_1h.price * 100
        elif side == "SHORT" and last_high_1h is not None and last_high_1h.price > 0:
            extension_pct = (last_high_1h.price - current_close) / last_high_1h.price * 100
        else:
            extension_pct = 0.0  # referans swing yoksa genişleme ölçülemiyor, engellemiyoruz

        momentum_still_strong = timing_score >= self._cfg.extension_momentum_override_ratio * self._cfg.timing_weight

        # "Son 4-5 mum hep aynı yönde + dirence/desteğe yakın" — genişleme
        # yüzdesinden bağımsız, ayrı bir tükenme sezgisi. 4-5 ardışık aynı
        # renkli mum, kâr satışının yakın olabileceğinin somut bir işareti;
        # genişleme %'si henüz eşiği geçmemiş olsa bile bu tek başına riskli.
        streak_1h = pa.same_direction_streak(candles_1h, side, max_lookback=5)
        near_resistance = extension_pct >= self._cfg.max_extension_pct * 0.5
        streak_into_resistance = streak_1h >= 4 and near_resistance

        if (extension_pct > self._cfg.max_extension_pct or streak_into_resistance) and not momentum_still_strong:
            reason = (
                f"Genişleme çok fazla VE momentum zayıf: son 1h taban/tepeden %{extension_pct:.1f} "
                f"uzaklaşmış (eşik %{self._cfg.max_extension_pct:.0f})"
                if extension_pct > self._cfg.max_extension_pct
                else (
                    f"Son {streak_1h} mum (1h) art arda {side} yönünde VE dirence/desteğe yakın "
                    f"(%{extension_pct:.1f} uzaklık) — kâr satışı/düzeltme riski yüksek"
                )
            )
            return Signal(
                symbol=symbol, side=side, macro_trend=macro_trend, macro_confirmed=True,
                entry_confirmed=entry_confirmed, timing_confirmed=timing_confirmed,
                entry_score=round(entry_score, 1), timing_score=round(timing_score, 1),
                suggested_entry_price=None, confidence=0.0, is_actionable=False,
                reasons=reasons + [f"{reason}, timing_score={timing_score:.1f}/{self._cfg.timing_weight:.0f} "
                                    "— hareket tükenmiş görünüyor, geç kalınmış"],
            )
        if (extension_pct > self._cfg.max_extension_pct or streak_into_resistance) and momentum_still_strong:
            reasons.append(
                f"Uzak/dirence yakın (%{extension_pct:.1f}, streak={streak_1h}) ama momentum hâlâ güçlü "
                f"(timing_score={timing_score:.1f}) — hareket devam ediyor sayılıp REDDEDİLMEDİ"
            )

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


def _compute_timing_score(
    timeframes: list[tuple[list, str]], side: str, cfg: AnalyzerConfig, reasons: list[str] | None = None
) -> tuple[float, int]:
    """15m/5m/1m hacim+momentum puanlamasının ortak çekirdeği. `analyze()` ve
    `timing_score_for()` (pozisyon-içi momentum takibi, yeniden giriş kararı)
    aynı bu fonksiyonu kullanır — tek bir yerde tanımlı tutmak için."""
    per_tf_weight = cfg.timing_weight / 3
    timing_score = 0.0
    confirmed_tf_count = 0
    for tf_candles, tf_name in timeframes:
        vol_ratio = pa.volume_anomaly_ratio(tf_candles)
        roc = pa.momentum_roc(tf_candles, periods=5)
        momentum_aligned = (roc > 0 and side == "LONG") or (roc < 0 and side == "SHORT")

        if not momentum_aligned:
            continue

        ratio_score = min(vol_ratio / cfg.timing_target_volume_ratio, 1.0)
        tf_score = ratio_score * per_tf_weight
        timing_score += tf_score

        if ratio_score >= 1.0:
            confirmed_tf_count += 1
        if reasons is not None:
            reasons.append(
                f"{tf_name}: hacim anomalisi x{vol_ratio:.2f} (hedef x{cfg.timing_target_volume_ratio:.1f}), "
                f"momentum {roc:+.2f}% (yönle uyumlu) -> {tf_score:.1f}/{per_tf_weight:.1f} puan"
            )
    return timing_score, confirmed_tf_count
