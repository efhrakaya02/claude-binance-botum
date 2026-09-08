"""
Data Layer'ın ürettiği/tükettiği veri yapıları.
Diğer tüm modüller (Scanner, Analyzer, Liquidation Engine, Risk Manager)
bu tiplerle konuşacak — böylece Binance'in ham JSON formatına bağımlı
kalmazlar.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class Candle:
    symbol: str
    interval: str
    open_time_ms: int
    close_time_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    quote_volume: float
    n_trades: int
    taker_buy_base_volume: float
    is_closed: bool  # False ise mum henüz kapanmadı, canlı güncelleniyor

    @classmethod
    def from_binance_kline_payload(cls, symbol: str, k: dict) -> "Candle":
        return cls(
            symbol=symbol,
            interval=k["i"],
            open_time_ms=k["t"],
            close_time_ms=k["T"],
            open=float(k["o"]),
            high=float(k["h"]),
            low=float(k["l"]),
            close=float(k["c"]),
            volume=float(k["v"]),
            quote_volume=float(k["q"]),
            n_trades=int(k["n"]),
            taker_buy_base_volume=float(k["V"]),
            is_closed=bool(k["x"]),
        )


@dataclass(slots=True)
class OrderBookLevel:
    price: float
    quantity: float


@dataclass(slots=True)
class OrderBookSnapshot:
    symbol: str
    last_update_id: int
    timestamp_ms: int
    bids: list[OrderBookLevel]  # fiyata göre azalan sıralı (en iyi bid ilk sırada)
    asks: list[OrderBookLevel]  # fiyata göre artan sıralı (en iyi ask ilk sırada)

    @property
    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None

    @property
    def mid_price(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / 2


@dataclass(slots=True)
class LiquidationEvent:
    symbol: str
    side: str  # "BUY" (short pozisyonlar likide oluyor) / "SELL" (long pozisyonlar likide oluyor)
    price: float
    quantity: float
    quote_quantity: float
    timestamp_ms: int

    @classmethod
    def from_binance_force_order_payload(cls, payload: dict) -> "LiquidationEvent":
        o = payload["o"]
        price = float(o["ap"]) or float(o["p"])
        qty = float(o["q"])
        return cls(
            symbol=o["s"],
            side=o["S"],
            price=price,
            quantity=qty,
            quote_quantity=price * qty,
            timestamp_ms=o["T"],
        )


@dataclass(slots=True)
class OpenInterestSnapshot:
    symbol: str
    open_interest: float       # kontrat/coin cinsinden
    open_interest_value: float  # USDT cinsinden (open_interest * mark_price)
    timestamp_ms: int


@dataclass(slots=True)
class FundingRateSnapshot:
    symbol: str
    last_funding_rate: float
    next_funding_time_ms: int
    mark_price: float
    timestamp_ms: int
