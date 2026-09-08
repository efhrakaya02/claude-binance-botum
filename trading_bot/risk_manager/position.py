from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum


class PositionStatus(Enum):
    OPEN = "OPEN"
    TRACKING = "TRACKING"   # sweep sonrası kapatıldı, fırsat devam ederse yeniden girmek üzere izleniyor
    CLOSED = "CLOSED"


@dataclass
class Position:
    symbol: str
    side: str                 # "LONG" / "SHORT"
    entry_price: float
    quantity: float
    margin_usdt: float
    leverage: int

    status: PositionStatus = PositionStatus.OPEN

    stop_price: float | None = None
    tp_price: float | None = None

    breakeven_triggered: bool = False
    trailing_active: bool = False
    last_trail_price: float | None = None   # trailing adımlarının hesaplandığı referans ham fiyat
    peak_favorable_price: float | None = None

    opened_at_ms: int = 0
    closed_at_ms: int | None = None
    realized_pnl_usdt: float | None = None

    # Sweep sonrası TRACKING durumuna geçen bir işlem tekrar açıldığında, orijinal
    # işlemin kaç kez sweep yaşayıp yeniden açıldığını izlemek için.
    reentry_count: int = 0

    def __post_init__(self) -> None:
        if self.opened_at_ms == 0:
            self.opened_at_ms = int(time.time() * 1000)

    @property
    def is_long(self) -> bool:
        return self.side == "LONG"
