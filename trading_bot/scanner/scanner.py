"""
Scanner modülü.

Amaç: TÜM USDT-M perpetual evrenini (yüzlerce sembol) hafif bir REST
çağrısıyla (24hr ticker) periyodik olarak tarayıp gainers/losers/volume
top-50 listelerini çıkarmak VE — asıl kritik kısım — bir sembol bu
listelerin en tepesine varmadan, sıralamada HIZLA yükselirken tespit
etmek (rank velocity). Böylece %100-150 pump yapan bir coin, liste
tepesine oturmadan aday olarak işaretlenir.

Scanner TÜM evren için WebSocket açmaz (yüzlerce stream = gereksiz yük).
Sadece bir sembol güçlü aday haline geldiğinde DataLayer.add_symbol()
çağrılarak o sembol için kline+depth stream'leri açılır; Analyzer ve
Liquidation Engine ancak o zaman devreye girer.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from ..config import ScannerConfig
from ..data_layer import DataLayer

logger = logging.getLogger(__name__)

NEUTRAL_RANK = 10_000  # bir sembol ilgili listede yoksa kullanılan "sonsuz" rank


@dataclass
class ScanResult:
    symbol: str
    price_change_pct: float
    quote_volume: float
    rank_gainer: int | None
    rank_loser: int | None
    rank_volume: int | None
    best_rank: int              # üç listeden en iyisi (en küçük sayı)
    prev_best_rank: int | None  # bir önceki taramadaki best_rank (yoksa None -> yeni giriş)
    rank_velocity: int          # prev_best_rank - best_rank (pozitif = hızla yükseliyor)
    is_new_entrant: bool        # önceki taramada hiçbir listede yoktu, şimdi var
    composite_score: float      # sıralama önceliği için birleşik skor


@dataclass
class ScannerState:
    # symbol -> bir önceki taramadaki best_rank
    previous_best_rank: dict[str, int] = field(default_factory=dict)


class Scanner:
    def __init__(self, data_layer: DataLayer, cfg: ScannerConfig | None = None) -> None:
        self._data_layer = data_layer
        self._cfg = cfg or ScannerConfig()
        self._state = ScannerState()
        self._stop_event = asyncio.Event()

    async def stop(self) -> None:
        self._stop_event.set()

    async def run(self, on_candidates) -> None:
        """Sürekli tarama döngüsü. `on_candidates` async callback: (list[ScanResult]) -> None
        çağrılır ve öncelik sırasına göre (composite_score azalan) sıralanmış adayları alır."""
        while not self._stop_event.is_set():
            try:
                results = await self.scan_once()
                candidates = self.select_priority_candidates(results)
                if candidates:
                    await on_candidates(candidates)
            except Exception:
                logger.exception("Scanner taramasında hata")

            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self._cfg.rescan_interval_seconds
                )
            except asyncio.TimeoutError:
                pass

    async def scan_once(self) -> list[ScanResult]:
        tickers = await self._data_layer.get_24hr_tickers()
        # Sadece USDT-M perpetual'lar; garip/az likit sembolleri elemek için
        # minimum bir hacim eşiği koymuyoruz çünkü top-50 zaten bunu doğal
        # olarak filtreliyor.
        usable = [t for t in tickers if t.get("symbol", "").endswith("USDT")]

        by_gain = sorted(usable, key=lambda t: float(t["priceChangePercent"]), reverse=True)
        by_loss = sorted(usable, key=lambda t: float(t["priceChangePercent"]))
        by_vol = sorted(usable, key=lambda t: float(t["quoteVolume"]), reverse=True)

        gainer_rank = {t["symbol"]: i + 1 for i, t in enumerate(by_gain[: self._cfg.top_n_gainers])}
        loser_rank = {t["symbol"]: i + 1 for i, t in enumerate(by_loss[: self._cfg.top_n_losers])}
        volume_rank = {t["symbol"]: i + 1 for i, t in enumerate(by_vol[: self._cfg.top_n_volume])}

        candidate_symbols = set(gainer_rank) | set(loser_rank) | set(volume_rank)
        ticker_by_symbol = {t["symbol"]: t for t in usable}

        results: list[ScanResult] = []
        current_best_rank: dict[str, int] = {}

        for symbol in candidate_symbols:
            t = ticker_by_symbol[symbol]
            rg = gainer_rank.get(symbol)
            rl = loser_rank.get(symbol)
            rv = volume_rank.get(symbol)
            best_rank = min(x for x in (rg, rl, rv, NEUTRAL_RANK) if x is not None)
            current_best_rank[symbol] = best_rank

            prev_rank = self._state.previous_best_rank.get(symbol)
            is_new = prev_rank is None
            velocity = (prev_rank - best_rank) if prev_rank is not None else 0

            results.append(
                ScanResult(
                    symbol=symbol,
                    price_change_pct=float(t["priceChangePercent"]),
                    quote_volume=float(t["quoteVolume"]),
                    rank_gainer=rg,
                    rank_loser=rl,
                    rank_volume=rv,
                    best_rank=best_rank,
                    prev_best_rank=prev_rank,
                    rank_velocity=velocity,
                    is_new_entrant=is_new,
                    composite_score=self._composite_score(
                        price_change_pct=float(t["priceChangePercent"]),
                        best_rank=best_rank,
                        velocity=velocity,
                        is_new=is_new,
                    ),
                )
            )

        self._state.previous_best_rank = current_best_rank
        results.sort(key=lambda r: r.composite_score, reverse=True)
        return results

    def select_priority_candidates(self, results: list[ScanResult]) -> list[ScanResult]:
        """Analyzer'a öncelikli olarak gönderilecek adaylar: hızla tırmananlar
        ve yeni girenler. Listenin tamamı değil, gerçekten sinyal taşıyanlar."""
        out = []
        for r in results:
            if r.is_new_entrant and r.best_rank <= 40:
                out.append(r)
            elif r.rank_velocity >= 10:  # 10+ basamak yükseliş = anlamlı ivme
                out.append(r)
            elif abs(r.price_change_pct) >= 15:  # zaten sert hareket etmiş, geç kalmış olsak da izlemeye değer
                out.append(r)
        return out

    @staticmethod
    def _composite_score(price_change_pct: float, best_rank: int, velocity: int, is_new: bool) -> float:
        # Küçük best_rank (listenin tepesine yakın) ve yüksek velocity (hızlı tırmanış)
        # skoru artırır. is_new bonus: henüz kimsenin fark etmediği erken sinyal.
        rank_component = max(0.0, (NEUTRAL_RANK - best_rank) / NEUTRAL_RANK) * 40
        velocity_component = min(max(velocity, 0), 100) * 0.6
        momentum_component = min(abs(price_change_pct), 100) * 0.3
        new_bonus = 15.0 if is_new else 0.0
        return rank_component + velocity_component + momentum_component + new_bonus
