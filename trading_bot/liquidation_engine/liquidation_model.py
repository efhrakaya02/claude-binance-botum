"""
Kendi likidasyon tahmin modelimiz.

Binance'in gerçek zamanlı verdiği tek şey GERÇEKLEŞMİŞ likidasyonlar
(forceOrder stream). Henüz gerçekleşmemiş, "şu fiyata gelirse burada
yığınla likidasyon tetiklenir" tahminini KENDİMİZ hesaplıyoruz:

    tahmini_küme_fiyatı = mevcut_fiyat * (1 ± bakım_marjı_katsayısı / kaldıraç)

Varsayılan bir kaldıraç dağılımı (retail trader'ların yoğunlaştığı 5x-100x
aralığı) üzerinden, her kaldıraç seviyesi için bir fiyat kümesi ve o kümenin
tahmini büyüklüğünü (toplam OI'nin o kaldıraca düşen payı) hesaplıyoruz.

Funding rate'i "skew" (long/short OI dengesizliği) tahmini için kullanıyoruz:
funding pozitif ve yüksekse piyasa long ağırlıklı demektir -> long likidasyon
kümesi (fiyatın ALTINDA) daha büyük tahmin edilir, ve tersi.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import LiquidationConfig


@dataclass
class LiquidationCluster:
    price: float
    side: str              # "LONG" (bu fiyata düşerse long'lar likide olur) / "SHORT"
    estimated_size_usdt: float
    leverage: int


def _funding_skew_to_long(funding_rate: float) -> float:
    """Funding rate'i [0, 1] aralığında bir 'long payı' tahminine çevirir.
    0.5 = dengeli. Basit doğrusal ölçekleme; ekstrem funding'lerde clip edilir."""
    # Binance funding genelde -0.75% ile +0.75% (8 saatlik) aralığında dolaşır;
    # bunun dışına taşarsa da clip ile sınırlıyoruz.
    scaled = 0.5 + (funding_rate / 0.0075) * 0.3
    return min(max(scaled, 0.15), 0.85)


def estimate_liquidation_clusters(
    current_price: float,
    open_interest_value_usdt: float,
    funding_rate: float,
    cfg: LiquidationConfig | None = None,
) -> list[LiquidationCluster]:
    cfg = cfg or LiquidationConfig()
    if current_price <= 0 or open_interest_value_usdt <= 0:
        return []

    long_share = _funding_skew_to_long(funding_rate)
    short_share = 1.0 - long_share

    total_weight = sum(cfg.assumed_leverage_weights.values()) or 1.0

    clusters: list[LiquidationCluster] = []
    for leverage, weight in cfg.assumed_leverage_weights.items():
        norm_weight = weight / total_weight
        move_fraction = cfg.maintenance_margin_factor / leverage

        long_liq_price = current_price * (1 - move_fraction)
        short_liq_price = current_price * (1 + move_fraction)

        long_size = open_interest_value_usdt * norm_weight * long_share
        short_size = open_interest_value_usdt * norm_weight * short_share

        clusters.append(LiquidationCluster(long_liq_price, "LONG", long_size, leverage))
        clusters.append(LiquidationCluster(short_liq_price, "SHORT", short_size, leverage))

    return clusters


def nearest_cluster_distance_pct(
    clusters: list[LiquidationCluster], reference_price: float, side: str
) -> tuple[LiquidationCluster | None, float | None]:
    """`side`: giriş yönümüz karşısında tehlike oluşturan küme tipini seçmek için
    kullanılır. LONG girişte asıl risk, fiyatın aşağı sweep edilip mevcut
    LONG kümelerini süpürmesi; SHORT girişte ise yukarı sweep ile SHORT kümelerinin
    süpürülmesidir — bu yüzden kendi yönümüzdeki kümelere bakıyoruz."""
    relevant = [c for c in clusters if c.side == side]
    if not relevant or reference_price <= 0:
        return None, None
    nearest = min(relevant, key=lambda c: abs(c.price - reference_price))
    distance_pct = abs(nearest.price - reference_price) / reference_price * 100
    return nearest, distance_pct
