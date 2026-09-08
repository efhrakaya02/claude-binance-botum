"""
DataLayer: Scanner, Analyzer, Liquidation Engine gibi üst modüllerin
konuşacağı TEK arayüz. Binance'in WS/REST detaylarını burada saklıyoruz;
üst modüller sadece get_klines(), get_orderbook() gibi temiz metodlar görür.

Kullanım akışı (üst modüller için):
    layer = DataLayer()
    await layer.start()
    await layer.add_symbol("BTCUSDT")   # scanner yeni aday bulduğunda çağırır
    ...
    candles = layer.get_klines("BTCUSDT", "1h", limit=100)
    ob = layer.get_orderbook("BTCUSDT")
    recent_liqs = layer.get_recent_liquidations("BTCUSDT", since_seconds=60)
    await layer.remove_symbol("BTCUSDT")  # sembol artık izlenmiyorsa
    ...
    await layer.stop()
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict, deque

import aiohttp

from .. import config
from .models import (
    Candle,
    FundingRateSnapshot,
    LiquidationEvent,
    OpenInterestSnapshot,
    OrderBookLevel,
    OrderBookSnapshot,
)
from .rest_client import BinanceFuturesREST
from .websocket_manager import BinanceFuturesWebSocket

logger = logging.getLogger(__name__)


class DataLayer:
    def __init__(self, testnet: bool = False) -> None:
        self._testnet = testnet

        self._ws = BinanceFuturesWebSocket(
            on_kline=self._handle_kline_message,
            on_depth=self._handle_depth_message,
            on_liquidation=self._handle_liquidation_message,
            testnet=testnet,
        )
        self._rest_session: aiohttp.ClientSession | None = None
        self._rest: BinanceFuturesREST | None = None

        # (symbol, interval) -> son N mum
        self._klines: dict[tuple[str, str], deque[Candle]] = defaultdict(deque)
        # symbol -> son orderbook snapshot
        self._orderbooks: dict[str, OrderBookSnapshot] = {}
        # global likidasyon geçmişi (tüm semboller, zaman sıralı)
        self._liquidations: deque[LiquidationEvent] = deque(maxlen=config.LIQUIDATION_BUFFER_SIZE)
        # symbol -> son open interest / funding rate
        self._open_interest: dict[str, OpenInterestSnapshot] = {}
        self._funding_rates: dict[str, FundingRateSnapshot] = {}

        self._watched_symbols: set[str] = set()
        self._tasks: list[asyncio.Task] = []
        self._stop_event = asyncio.Event()

    # ------------------------------------------------------------------ #
    # Yaşam döngüsü
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        self._rest_session = aiohttp.ClientSession()
        self._rest = BinanceFuturesREST(testnet=self._testnet, session=self._rest_session)

        self._tasks.append(asyncio.create_task(self._ws.run(), name="ws_run"))
        await self._ws.wait_until_connected(timeout=15)

        # Likidasyon akışı sembolden bağımsız, global — bir kere abone oluyoruz.
        await self._ws.subscribe_liquidations()

        self._tasks.append(asyncio.create_task(self._oi_funding_poll_loop(), name="oi_funding_poll"))
        logger.info("DataLayer başlatıldı")

    async def stop(self) -> None:
        self._stop_event.set()
        await self._ws.stop()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._rest is not None:
            await self._rest.close()
        if self._rest_session is not None:
            await self._rest_session.close()
        logger.info("DataLayer durduruldu")

    # ------------------------------------------------------------------ #
    # Sembol izleme yönetimi (Scanner tarafından çağrılır)
    # ------------------------------------------------------------------ #
    async def add_symbol(self, symbol: str) -> None:
        symbol = symbol.upper()
        if symbol in self._watched_symbols:
            return
        self._watched_symbols.add(symbol)

        # Buffer'ları geçmiş veriyle doldur (WS sadece o andan sonrasını verir).
        assert self._rest is not None
        for interval in config.KLINE_INTERVALS:
            key = (symbol, interval)
            try:
                raw = await self._rest.get_klines_rest(
                    symbol, interval, limit=config.MAX_KLINE_HISTORY[interval]
                )
                self._klines[key] = deque(
                    (self._candle_from_rest_row(symbol, interval, row) for row in raw),
                    maxlen=config.MAX_KLINE_HISTORY[interval],
                )
            except Exception:
                logger.exception("Geçmiş kline verisi çekilemedi: %s %s", symbol, interval)

        await self._ws.subscribe_symbol_full(symbol, config.KLINE_INTERVALS)
        logger.info("Sembol izlemeye alındı: %s", symbol)

    async def remove_symbol(self, symbol: str) -> None:
        symbol = symbol.upper()
        if symbol not in self._watched_symbols:
            return
        self._watched_symbols.discard(symbol)
        await self._ws.unsubscribe_symbol_full(symbol, config.KLINE_INTERVALS)
        for interval in config.KLINE_INTERVALS:
            self._klines.pop((symbol, interval), None)
        self._orderbooks.pop(symbol, None)
        self._open_interest.pop(symbol, None)
        self._funding_rates.pop(symbol, None)
        logger.info("Sembol izlemeden çıkarıldı: %s", symbol)

    @property
    def watched_symbols(self) -> set[str]:
        return set(self._watched_symbols)

    # ------------------------------------------------------------------ #
    # Okuma arayüzü (Analyzer, Liquidation Engine, Risk Manager kullanır)
    # ------------------------------------------------------------------ #
    def get_klines(self, symbol: str, interval: str, limit: int | None = None) -> list[Candle]:
        buf = self._klines.get((symbol.upper(), interval))
        if buf is None:
            return []
        data = list(buf)
        return data[-limit:] if limit else data

    def get_orderbook(self, symbol: str) -> OrderBookSnapshot | None:
        return self._orderbooks.get(symbol.upper())

    def get_recent_liquidations(
        self, symbol: str | None = None, since_seconds: float | None = None
    ) -> list[LiquidationEvent]:
        now_ms = time.time() * 1000
        result = []
        for ev in self._liquidations:
            if symbol is not None and ev.symbol != symbol.upper():
                continue
            if since_seconds is not None and (now_ms - ev.timestamp_ms) > since_seconds * 1000:
                continue
            result.append(ev)
        return result

    def get_open_interest(self, symbol: str) -> OpenInterestSnapshot | None:
        return self._open_interest.get(symbol.upper())

    def get_funding_rate(self, symbol: str) -> FundingRateSnapshot | None:
        return self._funding_rates.get(symbol.upper())

    async def get_all_futures_symbols(self) -> list[str]:
        """Scanner'ın tarayacağı tüm USDT-M perpetual evrenini döner (REST, önbelleksiz)."""
        assert self._rest is not None
        return await self._rest.get_all_symbols()

    async def get_24hr_tickers(self) -> list[dict]:
        """Scanner'ın gainers/losers/volume top-50 hesaplaması için ham 24h veri."""
        assert self._rest is not None
        return await self._rest.get_24hr_tickers()

    # ------------------------------------------------------------------ #
    # WebSocket mesaj işleyicileri (private)
    # ------------------------------------------------------------------ #
    async def _handle_kline_message(self, msg: dict) -> None:
        symbol = msg["s"]
        k = msg["k"]
        interval = k["i"]
        candle = Candle.from_binance_kline_payload(symbol, k)
        key = (symbol, interval)
        buf = self._klines[key]
        if buf.maxlen is None:
            # defaultdict ilk erişimde sınırsız deque() üretir; add_symbol()
            # zaten geçmiş veriyle sınırlı bir deque set ediyor ama WS mesajı
            # add_symbol tamamlanmadan önce gelirse bu fallback devreye girer.
            buf = deque(buf, maxlen=config.MAX_KLINE_HISTORY.get(interval, 500))
            self._klines[key] = buf

        # Son eleman aynı open_time'a sahipse (mum henüz kapanmadı) güncelle,
        # değilse yeni mum olarak ekle.
        if buf and buf[-1].open_time_ms == candle.open_time_ms:
            buf[-1] = candle
        else:
            buf.append(candle)

    async def _handle_depth_message(self, msg: dict) -> None:
        # Partial book depth stream (depth20@100ms) her seferinde TAM snapshot
        # gönderir — diff birleştirme gerekmez, doğrudan değiştiriyoruz.
        symbol = msg["s"]
        bids = [OrderBookLevel(price=float(p), quantity=float(q)) for p, q in msg["b"]]
        asks = [OrderBookLevel(price=float(p), quantity=float(q)) for p, q in msg["a"]]
        self._orderbooks[symbol] = OrderBookSnapshot(
            symbol=symbol,
            last_update_id=msg.get("u", 0),
            timestamp_ms=msg.get("E", int(time.time() * 1000)),
            bids=bids,
            asks=asks,
        )

    async def _handle_liquidation_message(self, msg: dict) -> None:
        event = LiquidationEvent.from_binance_force_order_payload(msg)
        self._liquidations.append(event)

    # ------------------------------------------------------------------ #
    # REST polling döngüsü (OI + funding rate)
    # ------------------------------------------------------------------ #
    async def _oi_funding_poll_loop(self) -> None:
        assert self._rest is not None
        while not self._stop_event.is_set():
            symbols = list(self._watched_symbols)
            for symbol in symbols:
                try:
                    self._funding_rates[symbol] = await self._rest.get_mark_price(symbol)
                    self._open_interest[symbol] = await self._rest.get_open_interest(symbol)
                except Exception:
                    logger.exception("OI/funding çekilemedi: %s", symbol)
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=config.OI_FUNDING_POLL_INTERVAL_SECONDS
                )
            except asyncio.TimeoutError:
                pass

    @staticmethod
    def _candle_from_rest_row(symbol: str, interval: str, row: list) -> Candle:
        # REST /fapi/v1/klines satır formatı:
        # [openTime, open, high, low, close, volume, closeTime, quoteVolume,
        #  numTrades, takerBuyBaseVolume, takerBuyQuoteVolume, ignore]
        return Candle(
            symbol=symbol,
            interval=interval,
            open_time_ms=row[0],
            close_time_ms=row[6],
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            volume=float(row[5]),
            quote_volume=float(row[7]),
            n_trades=int(row[8]),
            taker_buy_base_volume=float(row[9]),
            is_closed=True,
        )
