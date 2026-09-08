"""
Binance Futures ham WebSocket bağlantısını yönetir.

Tasarım kararları:
- Tek bir bağlantı üzerinden Binance'in SUBSCRIBE/UNSUBSCRIBE mesajlarıyla
  dinamik stream yönetimi yapılır (her sembol için ayrı socket açmıyoruz —
  scanner sembol listesini her 15sn'de güncelleyebilir, bu yüzden dinamik
  abonelik şart).
- Bağlantı koparsa exponential backoff ile yeniden bağlanır ve önceki
  aktif stream setine otomatik yeniden abone olur (resubscribe).
- Bu katman SADECE ham mesajı parse edip ilgili callback'e yönlendirir;
  iş mantığı (analiz, risk vb.) burada YOK — o DataLayer ve üstündeki
  modüllerin işi.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
from typing import Awaitable, Callable

import websockets
from websockets.exceptions import ConnectionClosed

from .. import config

logger = logging.getLogger(__name__)

MessageHandler = Callable[[dict], Awaitable[None]]


class BinanceFuturesWebSocket:
    def __init__(
        self,
        on_kline: MessageHandler | None = None,
        on_depth: MessageHandler | None = None,
        on_liquidation: MessageHandler | None = None,
        testnet: bool = False,
    ) -> None:
        self._url = (
            config.BINANCE_FUTURES_WS_BASE_TESTNET
            if testnet
            else config.BINANCE_FUTURES_WS_BASE
        )
        self._on_kline = on_kline
        self._on_depth = on_depth
        self._on_liquidation = on_liquidation

        self._ws: websockets.WebSocketClientProtocol | None = None
        self._active_streams: set[str] = set()
        self._id_counter = itertools.count(1)
        self._stop_event = asyncio.Event()
        self._connected_event = asyncio.Event()

    # ------------------------------------------------------------------ #
    # Genel yaşam döngüsü
    # ------------------------------------------------------------------ #
    async def run(self) -> None:
        """Ana döngü: bağlan, dinle, kopunca yeniden bağlan. Task olarak çalıştırılmalı."""
        backoff = config.WS_RECONNECT_MIN_BACKOFF_SECONDS
        while not self._stop_event.is_set():
            try:
                async with websockets.connect(
                    self._url,
                    ping_interval=20,
                    ping_timeout=config.WS_PING_TIMEOUT_SECONDS,
                ) as ws:
                    self._ws = ws
                    self._connected_event.set()
                    backoff = config.WS_RECONNECT_MIN_BACKOFF_SECONDS
                    logger.info("Binance Futures WS bağlantısı kuruldu: %s", self._url)

                    if self._active_streams:
                        await self._send_subscribe_message(
                            list(self._active_streams), method="SUBSCRIBE"
                        )
                        logger.info(
                            "Yeniden bağlanma sonrası %d stream'e resubscribe edildi",
                            len(self._active_streams),
                        )

                    await self._listen_loop(ws)

            except (ConnectionClosed, OSError) as exc:
                self._connected_event.clear()
                logger.warning("WS bağlantısı koptu (%s), %ss sonra yeniden denenecek", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, config.WS_RECONNECT_MAX_BACKOFF_SECONDS)
            except asyncio.CancelledError:
                raise
            except Exception:
                self._connected_event.clear()
                logger.exception("WS döngüsünde beklenmeyen hata, %ss sonra yeniden denenecek", backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, config.WS_RECONNECT_MAX_BACKOFF_SECONDS)

    async def stop(self) -> None:
        self._stop_event.set()
        if self._ws is not None:
            await self._ws.close()

    async def wait_until_connected(self, timeout: float | None = None) -> None:
        await asyncio.wait_for(self._connected_event.wait(), timeout=timeout)

    # ------------------------------------------------------------------ #
    # Abonelik yönetimi
    # ------------------------------------------------------------------ #
    async def subscribe_kline(self, symbol: str, interval: str) -> None:
        await self._subscribe([f"{symbol.lower()}@kline_{interval}"])

    async def unsubscribe_kline(self, symbol: str, interval: str) -> None:
        await self._unsubscribe([f"{symbol.lower()}@kline_{interval}"])

    async def subscribe_depth(self, symbol: str) -> None:
        stream = (
            f"{symbol.lower()}@depth{config.ORDERBOOK_DEPTH_LEVELS}"
            f"@{config.ORDERBOOK_UPDATE_SPEED}"
        )
        await self._subscribe([stream])

    async def unsubscribe_depth(self, symbol: str) -> None:
        stream = (
            f"{symbol.lower()}@depth{config.ORDERBOOK_DEPTH_LEVELS}"
            f"@{config.ORDERBOOK_UPDATE_SPEED}"
        )
        await self._unsubscribe([stream])

    async def subscribe_liquidations(self) -> None:
        """Global likidasyon akışı — tüm semboller için tek seferlik abonelik yeterli."""
        await self._subscribe([config.LIQUIDATION_STREAM])

    async def subscribe_symbol_full(self, symbol: str, intervals: list[str]) -> None:
        """Bir sembol için tüm kline interval'ları + depth'e tek seferde abone olur."""
        streams = [f"{symbol.lower()}@kline_{iv}" for iv in intervals]
        streams.append(
            f"{symbol.lower()}@depth{config.ORDERBOOK_DEPTH_LEVELS}@{config.ORDERBOOK_UPDATE_SPEED}"
        )
        await self._subscribe(streams)

    async def unsubscribe_symbol_full(self, symbol: str, intervals: list[str]) -> None:
        streams = [f"{symbol.lower()}@kline_{iv}" for iv in intervals]
        streams.append(
            f"{symbol.lower()}@depth{config.ORDERBOOK_DEPTH_LEVELS}@{config.ORDERBOOK_UPDATE_SPEED}"
        )
        await self._unsubscribe(streams)

    async def _subscribe(self, streams: list[str]) -> None:
        new_streams = [s for s in streams if s not in self._active_streams]
        self._active_streams.update(streams)
        if new_streams and self._ws is not None:
            await self._send_subscribe_message(new_streams, method="SUBSCRIBE")

    async def _unsubscribe(self, streams: list[str]) -> None:
        existing = [s for s in streams if s in self._active_streams]
        self._active_streams.difference_update(streams)
        if existing and self._ws is not None:
            await self._send_subscribe_message(existing, method="UNSUBSCRIBE")

    async def _send_subscribe_message(self, streams: list[str], method: str) -> None:
        payload = {"method": method, "params": streams, "id": next(self._id_counter)}
        assert self._ws is not None
        await self._ws.send(json.dumps(payload))

    # ------------------------------------------------------------------ #
    # Mesaj işleme
    # ------------------------------------------------------------------ #
    async def _listen_loop(self, ws: "websockets.WebSocketClientProtocol") -> None:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("JSON parse edilemedi: %r", raw[:200])
                continue

            # SUBSCRIBE/UNSUBSCRIBE cevapları {"result": null, "id": ...} şeklinde gelir
            if "result" in msg and "id" in msg:
                continue

            await self._dispatch(msg)

    async def _dispatch(self, msg: dict) -> None:
        event_type = msg.get("e")
        try:
            if event_type == "kline" and self._on_kline is not None:
                await self._on_kline(msg)
            elif event_type == "depthUpdate" and self._on_depth is not None:
                await self._on_depth(msg)
            elif event_type == "forceOrder" and self._on_liquidation is not None:
                await self._on_liquidation(msg)
        except Exception:
            logger.exception("Callback işlenirken hata (event_type=%s)", event_type)
