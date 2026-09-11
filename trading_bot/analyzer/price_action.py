"""
Saf price-action yardımcı fonksiyonları. Hiçbir dış indikatör kütüphanesi
kullanmıyoruz (RSI/MACD vb. YOK) — istenen "tamamen price action modu" bu.

Terimler:
- Swing High/Low: yerel tepe/dip (fraktal yöntem — solunda ve sağında N mum
  daha düşük/yüksek olan mum).
- BOS (Break of Structure): mevcut trend yönünde, en son swing'i kapanışla
  geçme — trendin DEVAM ettiğinin onayı.
- CHoCH (Change of Character): trendin TERSİ yönünde en son swing'in
  kapanışla kırılması — olası trend dönüşü sinyali.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ..data_layer.models import Candle


class SwingType(Enum):
    HIGH = "HIGH"
    LOW = "LOW"


@dataclass
class SwingPoint:
    index: int
    price: float
    type: SwingType


class Trend(Enum):
    UP = "UP"
    DOWN = "DOWN"
    RANGE = "RANGE"


def find_swing_points(candles: list[Candle], lookback: int = 2) -> list[SwingPoint]:
    """Fraktal yöntemle swing high/low tespiti. `lookback`: bir mumun swing
    sayılması için solunda ve sağında kaç mumdan yüksek/düşük olması gerektiği."""
    points: list[SwingPoint] = []
    n = len(candles)
    for i in range(lookback, n - lookback):
        window = candles[i - lookback : i + lookback + 1]
        c = candles[i]
        if c.high == max(w.high for w in window):
            points.append(SwingPoint(index=i, price=c.high, type=SwingType.HIGH))
        elif c.low == min(w.low for w in window):
            points.append(SwingPoint(index=i, price=c.low, type=SwingType.LOW))
    return points


def determine_trend(swings: list[SwingPoint]) -> Trend:
    """Son swing high'lar ve swing low'lar sırasıyla yükseliyorsa UP,
    düşüyorsa DOWN, karışıksa RANGE."""
    highs = [s for s in swings if s.type == SwingType.HIGH][-3:]
    lows = [s for s in swings if s.type == SwingType.LOW][-3:]

    if len(highs) >= 2 and len(lows) >= 2:
        higher_highs = highs[-1].price > highs[-2].price
        higher_lows = lows[-1].price > lows[-2].price
        lower_highs = highs[-1].price < highs[-2].price
        lower_lows = lows[-1].price < lows[-2].price

        if higher_highs and higher_lows:
            return Trend.UP
        if lower_highs and lower_lows:
            return Trend.DOWN
    return Trend.RANGE


@dataclass
class StructureBreak:
    kind: str          # "BOS" veya "CHoCH"
    direction: Trend    # kırılım sonrası işaret ettiği yön
    broken_level: float
    break_close: float


def detect_structure_break(
    candles: list[Candle], swings: list[SwingPoint], prevailing_trend: Trend
) -> StructureBreak | None:
    """Son kapanışın, trend yönündeki en son swing'i geçip geçmediğine (BOS)
    veya trendin tersi yöndeki swing'i kırıp kırmadığına (CHoCH) bakar."""
    if not candles or not swings:
        return None
    last_close = candles[-1].close

    last_high = next((s for s in reversed(swings) if s.type == SwingType.HIGH), None)
    last_low = next((s for s in reversed(swings) if s.type == SwingType.LOW), None)

    if prevailing_trend == Trend.UP and last_high is not None and last_close > last_high.price:
        return StructureBreak("BOS", Trend.UP, last_high.price, last_close)
    if prevailing_trend == Trend.DOWN and last_low is not None and last_close < last_low.price:
        return StructureBreak("BOS", Trend.DOWN, last_low.price, last_close)

    if prevailing_trend == Trend.UP and last_low is not None and last_close < last_low.price:
        return StructureBreak("CHoCH", Trend.DOWN, last_low.price, last_close)
    if prevailing_trend == Trend.DOWN and last_high is not None and last_close > last_high.price:
        return StructureBreak("CHoCH", Trend.UP, last_high.price, last_close)

    return None


def volume_anomaly_ratio(candles: list[Candle], baseline_window: int = 20) -> float:
    """Son mumun hacmini, önceki `baseline_window` mumun ortalama hacmine oranlar.
    1.0 = normal, 3.0 = son mum ortalamanın 3 katı hacimli (anomali)."""
    if len(candles) < baseline_window + 1:
        return 1.0
    baseline = candles[-(baseline_window + 1) : -1]
    avg_vol = sum(c.volume for c in baseline) / len(baseline)
    if avg_vol == 0:
        return 1.0
    return candles[-1].volume / avg_vol


def momentum_roc(candles: list[Candle], periods: int = 5) -> float:
    """Rate of change: son `periods` mumdaki yüzdesel fiyat değişimi."""
    if len(candles) < periods + 1:
        return 0.0
    past = candles[-(periods + 1)].close
    now = candles[-1].close
    if past == 0:
        return 0.0
    return (now - past) / past * 100


def compute_atr(candles: list[Candle], period: int = 14) -> float | None:
    """Average True Range — basit hareketli ortalama ile (Wilder'ın üstel
    düzeltmesi değil, daha basit ve yeterince sağlam bir varyant).

    True Range = max(bugünkü_high - bugünkü_low, |high - önceki_close|,
    |low - önceki_close|). ATR bunların son `period` tanesinin ortalaması.

    Yetersiz veri varsa None döner — çağıran taraf bunu (ör. sabit bir
    yedek mesafe kullanarak) ele almalı."""
    if len(candles) < period + 1:
        return None
    true_ranges = []
    for i in range(1, len(candles)):
        c = candles[i]
        prev_close = candles[i - 1].close
        tr = max(c.high - c.low, abs(c.high - prev_close), abs(c.low - prev_close))
        true_ranges.append(tr)
    recent = true_ranges[-period:]
    return sum(recent) / len(recent)
