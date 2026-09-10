"""
DryRunExecutionEngine: ExecutionEngine ile AYNI arayüze sahip ama Binance'e
HİÇBİR imzalı (trading) çağrı yapmaz. Orchestrator'ın geri kalanı (Scanner,
Analyzer, LiquidationEngine, PositionManager) tamamen normal çalışır ve
GERÇEK piyasa verisiyle (DataLayer her zaman gerçek Binance market data'sına
bağlanır — bunun için API key gerekmez) karar üretir; sadece "emri gönder"
adımı burada simülasyona dönüşür.

Kullanım: main.py'de DRY_RUN=true ortam değişkeni ayarlanınca Orchestrator
bu motoru kullanır. Bu modda BINANCE_API_KEY/SECRET bile gerekmez (hiç
imzalı çağrı yapılmadığı için) — sadece testnet/mainnet market data
tarafını etkiler (BINANCE_TESTNET ile ayrı bir ayar).
"""

from __future__ import annotations

import logging

from ..data_layer import DataLayer

logger = logging.getLogger(__name__)


class DryRunExecutionEngine:
    def __init__(self, data_layer: DataLayer) -> None:
        self._data_layer = data_layer

    async def prepare_symbol(self, symbol: str, leverage: int) -> None:
        logger.info("[DRY RUN] %s: margin=ISOLATED, leverage=%dx ayarlandı (simüle)", symbol, leverage)

    async def open_position(self, symbol: str, side: str, margin_usdt: float, leverage: int) -> tuple[float, float]:
        ref_price = self._reference_price(symbol)
        if ref_price is None:
            raise RuntimeError(f"{symbol}: referans fiyat bulunamadı, simülasyon açılamıyor")

        notional = margin_usdt * leverage
        quantity = round(notional / ref_price, 6)  # gerçek stepSize bilinmediği için basit yuvarlama

        logger.info(
            "[DRY RUN] %s: %s pozisyon AÇILDI (simüle) qty=%s fiyat=%s (margin=%s USDT, %sx)",
            symbol, side, quantity, ref_price, margin_usdt, leverage,
        )
        return quantity, ref_price

    async def close_position_market(self, symbol: str, side: str, quantity: float) -> None:
        price = self._reference_price(symbol)
        logger.info("[DRY RUN] %s: pozisyon KAPATILDI (simüle) qty=%s fiyat=%s", symbol, quantity, price)

    async def update_stop(self, symbol: str, side: str, new_stop_price: float) -> None:
        logger.info("[DRY RUN] %s: stop güncellendi (simüle) -> %s", symbol, new_stop_price)

    async def update_take_profit(self, symbol: str, side: str, new_tp_price: float) -> None:
        logger.info("[DRY RUN] %s: TP güncellendi (simüle) -> %s", symbol, new_tp_price)

    def _reference_price(self, symbol: str) -> float | None:
        ob = self._data_layer.get_orderbook(symbol)
        if ob is not None and ob.mid_price is not None:
            return ob.mid_price
        candles = self._data_layer.get_klines(symbol, "1m", limit=1)
        return candles[-1].close if candles else None
