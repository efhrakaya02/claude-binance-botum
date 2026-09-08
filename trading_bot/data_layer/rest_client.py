"""
Binance Futures REST uç noktaları.

WebSocket'te bulunmayan ama likidasyon tahmin modeli ve scanner için
gerekli veriler burada: open interest, funding rate, 24h ticker (gainers/
losers/volume taraması Scanner modülünde bu client kullanılarak yapılacak).
"""

from __future__ import annotations

import logging
import time

import aiohttp

from .. import config
from .models import FundingRateSnapshot, OpenInterestSnapshot

logger = logging.getLogger(__name__)


class BinanceFuturesREST:
    def __init__(self, testnet: bool = False, session: aiohttp.ClientSession | None = None) -> None:
        self._base_url = (
            config.BINANCE_FUTURES_REST_BASE_TESTNET if testnet else config.BINANCE_FUTURES_REST_BASE
        )
        self._session = session
        self._owns_session = session is None

    async def __aenter__(self) -> "BinanceFuturesREST":
        if self._session is None:
            self._session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()

    async def _get(self, path: str, params: dict | None = None) -> dict | list:
        assert self._session is not None, "Önce __aenter__ ile veya session vererek başlat"
        url = f"{self._base_url}{path}"
        async with self._session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            resp.raise_for_status()
            return await resp.json()

    # ------------------------------------------------------------------ #
    async def get_exchange_info(self) -> dict:
        return await self._get("/fapi/v1/exchangeInfo")

    async def get_all_symbols(self, quote_asset: str = "USDT") -> list[str]:
        """USDT-M perpetual sembollerinin listesi (Scanner'ın tarayacağı evren)."""
        info = await self.get_exchange_info()
        symbols = []
        for s in info.get("symbols", []):
            if (
                s.get("quoteAsset") == quote_asset
                and s.get("contractType") == "PERPETUAL"
                and s.get("status") == "TRADING"
            ):
                symbols.append(s["symbol"])
        return symbols

    async def get_24hr_tickers(self) -> list[dict]:
        """Tüm semboller için 24h değişim/hacim — Scanner'ın gainers/losers/volume
        top-50 listelerini çıkarmak için kullanacağı ham veri."""
        data = await self._get("/fapi/v1/ticker/24hr")
        return data if isinstance(data, list) else [data]

    async def get_open_interest(self, symbol: str) -> OpenInterestSnapshot:
        data = await self._get("/fapi/v1/openInterest", params={"symbol": symbol})
        mark = await self.get_mark_price(symbol)
        oi = float(data["openInterest"])
        return OpenInterestSnapshot(
            symbol=symbol,
            open_interest=oi,
            open_interest_value=oi * mark.mark_price,
            timestamp_ms=int(time.time() * 1000),
        )

    async def get_mark_price(self, symbol: str) -> FundingRateSnapshot:
        data = await self._get("/fapi/v1/premiumIndex", params={"symbol": symbol})
        return FundingRateSnapshot(
            symbol=symbol,
            last_funding_rate=float(data["lastFundingRate"]),
            next_funding_time_ms=int(data["nextFundingTime"]),
            mark_price=float(data["markPrice"]),
            timestamp_ms=int(data["time"]),
        )

    async def get_klines_rest(self, symbol: str, interval: str, limit: int = 500) -> list[list]:
        """Bot ilk açıldığında geçmiş mum verisiyle buffer'ları doldurmak için
        (websocket sadece o andan itibaren canlı veri verir, geçmiş vermez)."""
        return await self._get(
            "/fapi/v1/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
        )
