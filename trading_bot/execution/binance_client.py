"""
Binance Futures'ın imza gerektiren (trading) uç noktaları.

API key/secret ASLA koda gömülmez — ortam değişkenlerinden okunur
(BINANCE_API_KEY / BINANCE_API_SECRET). Bu client sadece emir gönderimi,
kaldıraç/margin modu ayarı ve pozisyon sorgulamasından sorumludur; piyasa
verisi (kline/orderbook/OI) DataLayer'ın işi — burada tekrar edilmiyor.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
import urllib.parse

import aiohttp

from .. import config

logger = logging.getLogger(__name__)


class BinanceAPIError(Exception):
    def __init__(self, status: int, payload: dict) -> None:
        self.status = status
        self.payload = payload
        super().__init__(f"Binance API hatası ({status}): {payload}")


class BinanceFuturesTradingClient:
    def __init__(self, api_key: str, api_secret: str, testnet: bool = False) -> None:
        self._api_key = api_key
        self._api_secret = api_secret.encode()
        self._base_url = (
            config.BINANCE_FUTURES_REST_BASE_TESTNET if testnet else config.BINANCE_FUTURES_REST_BASE
        )
        self._session: aiohttp.ClientSession | None = None
        self._symbol_filters_cache: dict[str, dict] = {}

    async def __aenter__(self) -> "BinanceFuturesTradingClient":
        self._session = aiohttp.ClientSession(headers={"X-MBX-APIKEY": self._api_key})
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()

    # ------------------------------------------------------------------ #
    def _sign(self, params: dict) -> dict:
        params = dict(params)
        params["timestamp"] = int(time.time() * 1000)
        params["recvWindow"] = 5000
        query = urllib.parse.urlencode(params, doseq=True)
        signature = hmac.new(self._api_secret, query.encode(), hashlib.sha256).hexdigest()
        params["signature"] = signature
        return params

    async def _request(self, method: str, path: str, params: dict | None = None, signed: bool = True) -> dict:
        assert self._session is not None, "Client __aenter__ ile başlatılmalı"
        params = params or {}
        if signed:
            params = self._sign(params)
        url = f"{self._base_url}{path}"

        async with self._session.request(method, url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            data = await resp.json()
            if resp.status >= 400:
                raise BinanceAPIError(resp.status, data)
            return data

    # ------------------------------------------------------------------ #
    # Hesap / sembol ayarları
    # ------------------------------------------------------------------ #
    async def set_margin_type(self, symbol: str, margin_type: str = "ISOLATED") -> None:
        try:
            await self._request("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": margin_type})
        except BinanceAPIError as e:
            # -4046: "No need to change margin type" -> zaten isolated ise hata verir, yok sayılabilir
            if e.payload.get("code") != -4046:
                raise

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        await self._request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": leverage})

    async def get_symbol_filters(self, symbol: str) -> dict:
        if symbol in self._symbol_filters_cache:
            return self._symbol_filters_cache[symbol]
        data = await self._request("GET", "/fapi/v1/exchangeInfo", signed=False)
        for s in data.get("symbols", []):
            if s["symbol"] == symbol:
                filters = {f["filterType"]: f for f in s["filters"]}
                info = {
                    "quantity_precision": s["quantityPrecision"],
                    "price_precision": s["pricePrecision"],
                    "step_size": float(filters.get("LOT_SIZE", {}).get("stepSize", 0.0)) or None,
                    "tick_size": float(filters.get("PRICE_FILTER", {}).get("tickSize", 0.0)) or None,
                }
                self._symbol_filters_cache[symbol] = info
                return info
        raise ValueError(f"Sembol bulunamadı: {symbol}")

    # ------------------------------------------------------------------ #
    # Emirler
    # ------------------------------------------------------------------ #
    async def new_market_order(self, symbol: str, side: str, quantity: float, reduce_only: bool = False) -> dict:
        params = {
            "symbol": symbol,
            "side": side,           # "BUY" / "SELL"
            "type": "MARKET",
            "quantity": quantity,
        }
        if reduce_only:
            params["reduceOnly"] = "true"
            
        return await self._request("POST", "/fapi/v1/order", params)

    async def new_stop_market_order(
        self, symbol: str, side: str, stop_price: float, close_position: bool = True
    ) -> dict:
        params = {
            "symbol": symbol,
            "side": side,
            "type": "STOP_MARKET",
            "stopPrice": stop_price,
            "workingType": "MARK_PRICE",
        }
        if close_position:
            params["closePosition"] = "true"
            
        return await self._request("POST", "/fapi/v1/order", params)

    async def new_take_profit_market_order(
        self, symbol: str, side: str, stop_price: float, close_position: bool = True
    ) -> dict:
        params = {
            "symbol": symbol,
            "side": side,
            "type": "TAKE_PROFIT_MARKET",
            "stopPrice": stop_price,
            "workingType": "MARK_PRICE",
        }
        if close_position:
            params["closePosition"] = "true"
            
        return await self._request("POST", "/fapi/v1/order", params)

    async def cancel_order(self, symbol: str, order_id: int) -> dict:
        return await self._request("DELETE", "/fapi/v1/order", {"symbol": symbol, "orderId": order_id})

    async def cancel_all_open_orders(self, symbol: str) -> dict:
        return await self._request("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol})

    async def get_position_risk(self, symbol: str) -> list[dict]:
        return await self._request("GET", "/fapi/v2/positionRisk", {"symbol": symbol})
