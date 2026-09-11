"""
ExecutionEngine: RiskAction / açık pozisyon kararlarını gerçek Binance
Futures emirlerine çevirir. LONG/SHORT <-> BUY/SELL eşlemesini, miktar
hassasiyetini (stepSize) ve stop/tp emirlerinin değiştirilmesini (eski
emri iptal edip yenisini açma) burada yönetiyoruz.
"""

from __future__ import annotations

import logging
import math

from ..data_layer import DataLayer
from .binance_client import BinanceFuturesTradingClient

logger = logging.getLogger(__name__)

_ENTRY_SIDE = {"LONG": "BUY", "SHORT": "SELL"}
_EXIT_SIDE = {"LONG": "SELL", "SHORT": "BUY"}


class ExecutionEngine:
    def __init__(self, client: BinanceFuturesTradingClient, data_layer: DataLayer) -> None:
        self._client = client
        self._data_layer = data_layer
        # symbol -> son yerleştirilen stop/tp emir id'si (değiştirirken iptal etmek için)
        self._stop_order_ids: dict[str, int] = {}
        self._tp_order_ids: dict[str, int] = {}

    async def prepare_symbol(self, symbol: str, leverage: int) -> None:
        await self._client.set_margin_type(symbol, "ISOLATED")
        await self._client.set_leverage(symbol, leverage)

    async def open_position(self, symbol: str, side: str, margin_usdt: float, leverage: int) -> tuple[float, float]:
        """Market emriyle pozisyon açar. Döner: (quantity, fill_reference_price)."""
        await self.prepare_symbol(symbol, leverage)

        ob = self._data_layer.get_orderbook(symbol)
        ref_price = ob.mid_price if ob else None
        if ref_price is None:
            candles = self._data_layer.get_klines(symbol, "1m", limit=1)
            ref_price = candles[-1].close if candles else None
        if ref_price is None:
            raise RuntimeError(f"{symbol}: referans fiyat bulunamadı, emir gönderilemiyor")

        notional = margin_usdt * leverage
        raw_quantity = notional / ref_price
        quantity = await self._round_quantity(symbol, raw_quantity)

        order = await self._client.new_market_order(symbol, _ENTRY_SIDE[side], quantity)
        logger.info("%s: pozisyon açıldı side=%s qty=%s order=%s", symbol, side, quantity, order.get("orderId"))
        return quantity, ref_price

    async def close_position_market(self, symbol: str, side: str, quantity: float) -> None:
        await self._cancel_tracked_orders(symbol)
        await self._client.new_market_order(symbol, _EXIT_SIDE[side], quantity, reduce_only=True)
        logger.info("%s: pozisyon market emriyle kapatıldı", symbol)

    async def update_stop(self, symbol: str, side: str, new_stop_price: float) -> None:
        price = await self._round_price(symbol, new_stop_price)
        old_id = self._stop_order_ids.get(symbol)
        if old_id is not None:
            try:
                await self._client.cancel_order(symbol, old_id)
            except Exception:
                logger.exception("%s: eski stop emri iptal edilemedi (order_id=%s)", symbol, old_id)
        order = await self._client.new_stop_market_order(symbol, _EXIT_SIDE[side], price)
        self._stop_order_ids[symbol] = order.get("orderId")
        logger.info("%s: stop güncellendi -> %s", symbol, price)

    async def update_take_profit(self, symbol: str, side: str, new_tp_price: float) -> None:
        price = await self._round_price(symbol, new_tp_price)
        old_id = self._tp_order_ids.get(symbol)
        if old_id is not None:
            try:
                await self._client.cancel_order(symbol, old_id)
            except Exception:
                logger.exception("%s: eski TP emri iptal edilemedi (order_id=%s)", symbol, old_id)
        order = await self._client.new_take_profit_market_order(symbol, _EXIT_SIDE[side], price)
        self._tp_order_ids[symbol] = order.get("orderId")
        logger.info("%s: TP güncellendi -> %s", symbol, price)

    async def cancel_open_orders(self, symbol: str) -> None:
        """Bu sembol için kalan tüm açık emirleri (normal + algo stop/TP)
        temizler. Pozisyonun borsada dış bir sebeple (örn. stop/TP tetiklenmesi)
        zaten kapandığı tespit edildiğinde çağrılır — kalan yetim emirleri
        temizlemek için."""
        await self._cancel_tracked_orders(symbol)

    async def _cancel_tracked_orders(self, symbol: str) -> None:
        try:
            await self._client.cancel_all_open_orders(symbol)
        except Exception:
            logger.exception("%s: açık (normal) emirler iptal edilemedi", symbol)
        try:
            await self._client.cancel_all_algo_open_orders(symbol)
        except Exception:
            logger.exception("%s: açık algo (stop/TP) emirleri iptal edilemedi", symbol)
        self._stop_order_ids.pop(symbol, None)
        self._tp_order_ids.pop(symbol, None)

    # ------------------------------------------------------------------ #
    async def _round_quantity(self, symbol: str, quantity: float) -> float:
        filters = await self._client.get_symbol_filters(symbol)
        step = filters.get("step_size")
        precision = filters.get("quantity_precision", 3)
        if step:
            quantity = math.floor(quantity / step) * step
        return round(quantity, precision)

    async def _round_price(self, symbol: str, price: float) -> float:
        filters = await self._client.get_symbol_filters(symbol)
        tick = filters.get("tick_size")
        precision = filters.get("price_precision", 2)
        if tick:
            price = round(price / tick) * tick
        return round(price, precision)
