"""
Orchestrator: Scanner -> Analyzer -> Liquidation Engine -> Position Manager
-> Execution Engine zincirini birbirine bağlayan ana döngü.

Bu dosya "iş mantığının birleştiği yer" — her modül kendi başına test
edilebilir kalsın diye mantığı olabildiğince ilgili modülde tuttuk;
burada sadece akışı yönetiyoruz.

BİLİNEN SADELEŞTİRMELER (üretime almadan önce gözden geçir):
- reversal_signal / resumed_signal şu an 1m ve 5m'de pozisyon yönünün
  TERSİNE/AYNI yönde CHoCH/BOS olup olmadığına bakan basit bir kural;
  gerçek "zirve tespiti" hiçbir zaman kesin olamaz, bu sezgisel bir
  yaklaşımdır ve gerçek verilerle backtest edilip ayarlanmalıdır.
- _monitor_loop her POLL_INTERVAL_SECONDS'da bir çalışır; gerçek "anlık"
  takip için bu süre kısaltılabilir ama Binance rate limit'lerine dikkat
  edilmeli (özellikle stop/tp güncellemede REST çağrısı var).
"""

from __future__ import annotations

import asyncio
import logging
import time

from .analyzer import MultiTimeframeAnalyzer
from .analyzer.price_action import Trend, detect_structure_break, find_swing_points
from .config import RiskConfig
from .data_layer import DataLayer
from .execution import BinanceFuturesTradingClient, DryRunExecutionEngine, ExecutionEngine
from .execution.binance_client import BinanceAPIError
from .liquidation_engine import LiquidationEngine
from .risk_manager import PositionManager
from .scanner import Scanner, ScanResult

logger = logging.getLogger(__name__)

MONITOR_POLL_INTERVAL_SECONDS = 3.0
SYMBOL_WARMUP_SECONDS = 5.0  # add_symbol sonrası buffer'ların dolması için kısa bekleme
# Risk yönetimi (sweep/breakeven/trailing) HIZLI kalmalı — bu yüzden ayrı,
# sadece bilgilendirme amaçlı bir log döngüsü kullanıyoruz; onu yavaşlatmak
# sweep tepkisini geciktirir.
POSITION_STATUS_LOG_INTERVAL_SECONDS = 120.0

# Bir sembol pozisyonu/izlenen fırsatı yoksa VE son bu kadar saniyedir
# scanner tarafından aday olarak seçilmiyorsa izlemeden çıkarılır. add_symbol()
# hiç geri alınmazsa watched_symbols sadece büyür ve sonunda Binance'in
# TEK BAĞLANTI BAŞINA STREAM LİMİTİNİ (1024) aşıp sürekli kopma/yeniden
# bağlanma döngüsüne sokar — bu yüzden budama zorunlu.
SYMBOL_WATCH_TTL_SECONDS = 1800.0  # 30 dakika (~6 tarama döngüsü)
# Ek güvenlik: budama gecikse bile stream sayısı asla 1024 limitine
# yaklaşmasın diye sert bir tavan (60 sembol * 6 stream = 360 stream).
MAX_WATCHED_SYMBOLS = 60


class Orchestrator:
    def __init__(
        self, api_key: str, api_secret: str, testnet: bool = False, dry_run: bool = False
    ) -> None:
        self._testnet = testnet
        self._dry_run = dry_run
        self._data_layer = DataLayer(testnet=testnet)
        self._scanner = Scanner(self._data_layer)
        self._analyzer = MultiTimeframeAnalyzer(self._data_layer)
        self._liquidation_engine = LiquidationEngine(self._data_layer)
        self._risk_cfg = RiskConfig()
        self._position_manager = PositionManager(self._risk_cfg)

        self._trading_client = BinanceFuturesTradingClient(api_key, api_secret, testnet=testnet)
        self._execution_engine: ExecutionEngine | DryRunExecutionEngine | None = None  # start() içinde kurulur

        self._tasks: list[asyncio.Task] = []
        self._stop_event = asyncio.Event()
        # symbol -> en son ne zaman scanner adayı olarak seçildiği (budama için)
        self._symbol_last_candidate_ts: dict[str, float] = {}

    async def start(self) -> None:
        await self._data_layer.start()

        if self._dry_run:
            self._execution_engine = DryRunExecutionEngine(self._data_layer)
            logger.warning(
                "DRY RUN modu AKTİF — hiçbir gerçek emir gönderilmeyecek, sadece simülasyon yapılacak"
            )
        else:
            await self._trading_client.__aenter__()
            self._execution_engine = ExecutionEngine(self._trading_client, self._data_layer)

        self._tasks.append(asyncio.create_task(self._scanner.run(self._on_candidates), name="scanner"))
        self._tasks.append(asyncio.create_task(self._monitor_loop(), name="monitor"))
        self._tasks.append(asyncio.create_task(self._position_status_log_loop(), name="position_status_log"))
        logger.info("Orchestrator başlatıldı (testnet=%s, dry_run=%s)", self._testnet, self._dry_run)

    async def stop(self) -> None:
        self._stop_event.set()
        await self._scanner.stop()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self._data_layer.stop()
        if not self._dry_run:
            await self._trading_client.close()
        logger.info("Orchestrator durduruldu")

    # ------------------------------------------------------------------ #
    # Scanner'dan gelen adaylar
    # ------------------------------------------------------------------ #
    async def _on_candidates(self, candidates: list[ScanResult]) -> None:
        # Yeni adaylar eklenmeden ÖNCE budama yapılıyor ki uzun süredir
        # aday olmayan/pozisyonu olmayan semboller için yer açılsın.
        await self._prune_stale_symbols()

        now = time.time()
        for c in candidates:
            self._symbol_last_candidate_ts[c.symbol] = now

            if c.symbol in self._data_layer.watched_symbols:
                await self._try_enter(c.symbol)
                continue

            if len(self._data_layer.watched_symbols) >= MAX_WATCHED_SYMBOLS:
                logger.info(
                    "%s: izleme kapasitesi dolu (%d/%d), bu tarama döngüsünde atlanıyor",
                    c.symbol, len(self._data_layer.watched_symbols), MAX_WATCHED_SYMBOLS,
                )
                continue

            await self._data_layer.add_symbol(c.symbol)
            # Buffer'ların (kline geçmişi REST'ten yüklendiği için genelde
            # anında hazır olur, ama WS'in ilk mesajları için) kısa bir
            # ısınma payı bırakıyoruz.
            asyncio.create_task(self._try_enter_after_warmup(c.symbol))

    async def _prune_stale_symbols(self) -> None:
        """Pozisyonu/izlenen fırsatı olmayan ve uzun süredir aday olarak
        seçilmeyen sembolleri izlemeden çıkarır. Bu yapılmazsa watched_symbols
        sadece büyür ve Binance'in tek bağlantı başına 1024 stream limitini
        aşıp sürekli kopma/yeniden bağlanma döngüsüne yol açar."""
        now = time.time()
        for symbol in list(self._data_layer.watched_symbols):
            if symbol in self._position_manager.open_positions:
                continue
            if symbol in self._position_manager.tracked_opportunities:
                continue
            last_seen = self._symbol_last_candidate_ts.get(symbol, 0.0)
            if now - last_seen > SYMBOL_WATCH_TTL_SECONDS:
                await self._data_layer.remove_symbol(symbol)
                self._symbol_last_candidate_ts.pop(symbol, None)
                logger.info("%s: uzun süredir aday değil, izlemeden çıkarıldı", symbol)

    async def _try_enter_after_warmup(self, symbol: str) -> None:
        await asyncio.sleep(SYMBOL_WARMUP_SECONDS)
        await self._try_enter(symbol)

    async def _try_enter(self, symbol: str) -> None:
        if symbol in self._position_manager.open_positions:
            return  # zaten açık

        signal = self._analyzer.analyze(symbol)
        if signal is None or not signal.is_actionable:
            return

        if not self._position_manager.has_free_slot():
            # Slot doluysa: daha büyük fırsat mı, yoksa vazgeç mi kararı basitçe
            # confidence karşılaştırmasıyla veriliyor — gerçek kullanımda bu eşik
            # test edilerek ayarlanmalı.
            riskiest = self._position_manager.find_riskiest_position()
            if riskiest is None or signal.confidence < 70:
                return
            await self._close_position(riskiest.symbol, reason="daha_büyük_fırsat_için_slot_boşaltıldı")

        entry_price = signal.suggested_entry_price
        if entry_price is None:
            return

        safety = self._liquidation_engine.check_entry_safety(symbol, signal.side, entry_price)
        if not safety.is_safe:
            logger.info("%s: giriş güvensiz, atlanıyor -> %s", symbol, safety.reasons)
            return

        assert self._execution_engine is not None
        try:
            quantity, fill_price = await self._execution_engine.open_position(
                symbol, signal.side, self._risk_cfg.margin_per_position_usdt, self._risk_cfg.max_leverage
            )
        except Exception:
            logger.exception("%s: pozisyon açma emri başarısız", symbol)
            return

        position = self._position_manager.open_position(symbol, signal.side, fill_price, quantity)
        if position.stop_price is not None:
            try:
                await self._execution_engine.update_stop(symbol, position.side, position.stop_price)
            except Exception:
                logger.exception("%s: başlangıç stop emri gönderilemedi", symbol)

    async def _sync_closed_externally(self, symbol: str, position, last_known_price: float) -> None:
        """Binance -2022/-4509 gibi bir hatayla 'artık pozisyon yok' derse,
        bu neredeyse her zaman Binance'teki GERÇEK stop/TP emrinin bizden
        önce tetiklenip pozisyonu zaten kapattığı anlamına gelir. Bu durumda
        tekrar tekrar aynı hatayı almak yerine, botun kendi kayıtlarını
        borsanın gerçek durumuyla senkronize ediyoruz."""
        logger.warning(
            "%s: pozisyon borsada zaten kapanmış görünüyor (muhtemelen stop/TP tetiklendi), "
            "dahili durum senkronize ediliyor",
            symbol,
        )
        try:
            await self._execution_engine.cancel_open_orders(symbol)
        except Exception:
            logger.exception("%s: kalan emirler temizlenirken hata (önemli değil, devam ediliyor)", symbol)
        self._position_manager.close_position(symbol, last_known_price, reason="borsada_zaten_kapanmis")

    async def _close_position(self, symbol: str, reason: str) -> None:
        position = self._position_manager.open_positions.get(symbol)
        if position is None:
            return
        assert self._execution_engine is not None
        try:
            await self._execution_engine.close_position_market(symbol, position.side, position.quantity)
        except Exception:
            logger.exception("%s: kapatma emri başarısız", symbol)
            return
        current_price = self._current_price(symbol) or position.entry_price
        self._position_manager.close_position(symbol, current_price, reason)

    # ------------------------------------------------------------------ #
    # Açık pozisyon izleme döngüsü
    # ------------------------------------------------------------------ #
    async def _monitor_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self._monitor_once()
            except Exception:
                logger.exception("Monitor döngüsünde hata")
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=MONITOR_POLL_INTERVAL_SECONDS)
            except asyncio.TimeoutError:
                pass

    async def _monitor_once(self) -> None:
        assert self._execution_engine is not None

        for symbol, position in list(self._position_manager.open_positions.items()):
            current_price = self._current_price(symbol)
            if current_price is None:
                continue

            # 1) Sweep kontrolü — önce bu, çünkü sweep tespit edilirse diğer
            #    trailing hesaplarını atlayıp hemen çıkıyoruz.
            sweep = self._liquidation_engine.detect_sweep(symbol, position.side)
            if sweep is not None:
                logger.warning(sweep.message)
                try:
                    await self._execution_engine.close_position_market(symbol, position.side, position.quantity)
                except BinanceAPIError as e:
                    if e.indicates_no_open_position:
                        await self._sync_closed_externally(symbol, position, current_price)
                        continue
                    logger.exception("%s: sweep sonrası kapatma emri başarısız", symbol)
                    continue
                except Exception:
                    logger.exception("%s: sweep sonrası kapatma emri başarısız", symbol)
                    continue
                self._position_manager.handle_sweep_exit(position, current_price)
                continue

            # 2) Ters yönde yapı kırılımı (zirve/tersine dönüş sinyali)
            reversal_signal = self._detect_reversal(symbol, position.side)

            action = self._position_manager.update_position_risk(position, current_price, reversal_signal)

            if action.close_position:
                try:
                    await self._execution_engine.close_position_market(symbol, position.side, position.quantity)
                except BinanceAPIError as e:
                    if e.indicates_no_open_position:
                        await self._sync_closed_externally(symbol, position, current_price)
                        continue
                    logger.exception("%s: hedef kapatma emri başarısız", symbol)
                    continue
                except Exception:
                    logger.exception("%s: hedef kapatma emri başarısız", symbol)
                    continue
                self._position_manager.close_position(symbol, current_price, action.close_reason or "target")
                continue

            if action.update_stop is not None:
                try:
                    await self._execution_engine.update_stop(symbol, position.side, action.update_stop)
                except BinanceAPIError as e:
                    if e.indicates_no_open_position:
                        await self._sync_closed_externally(symbol, position, current_price)
                        continue
                    logger.exception("%s: stop güncellenemedi", symbol)
                except Exception:
                    logger.exception("%s: stop güncellenemedi", symbol)
            if action.update_tp is not None:
                try:
                    await self._execution_engine.update_take_profit(symbol, position.side, action.update_tp)
                except BinanceAPIError as e:
                    if e.indicates_no_open_position:
                        await self._sync_closed_externally(symbol, position, current_price)
                        continue
                    logger.exception("%s: TP güncellenemedi", symbol)
                except Exception:
                    logger.exception("%s: TP güncellenemedi", symbol)

        # 3) Sweep sonrası TRACKING'deki fırsatlar: hareket devam ediyorsa yeniden gir
        for symbol in list(self._position_manager.tracked_opportunities.keys()):
            if not self._position_manager.has_free_slot():
                continue
            tracked = self._position_manager.tracked_opportunities[symbol]
            resumed = self._detect_resumption(symbol, tracked.side)
            if self._position_manager.should_reenter(symbol, self._current_price(symbol) or 0.0, resumed):
                entry_price = self._current_price(symbol)
                if entry_price is None:
                    continue
                safety = self._liquidation_engine.check_entry_safety(symbol, tracked.side, entry_price)
                if not safety.is_safe:
                    continue
                try:
                    quantity, fill_price = await self._execution_engine.open_position(
                        symbol, tracked.side, self._risk_cfg.margin_per_position_usdt, self._risk_cfg.max_leverage
                    )
                except Exception:
                    logger.exception("%s: yeniden giriş emri başarısız", symbol)
                    continue
                reentered_position = self._position_manager.reenter(symbol, fill_price, quantity)
                if reentered_position.stop_price is not None:
                    try:
                        await self._execution_engine.update_stop(
                            symbol, reentered_position.side, reentered_position.stop_price
                        )
                    except Exception:
                        logger.exception("%s: yeniden giriş sonrası başlangıç stop emri gönderilemedi", symbol)

    # ------------------------------------------------------------------ #
    # Bilgilendirme amaçlı işlem takip logu (2 dakikada bir)
    # ------------------------------------------------------------------ #
    async def _position_status_log_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._log_position_statuses()
            except Exception:
                logger.exception("Pozisyon durum logu yazılırken hata")
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=POSITION_STATUS_LOG_INTERVAL_SECONDS
                )
            except asyncio.TimeoutError:
                pass

    def _log_position_statuses(self) -> None:
        positions = self._position_manager.open_positions
        if not positions:
            logger.info("İşlem takibi: şu anda açık pozisyon yok")
            return

        lines = []
        for symbol, position in positions.items():
            current_price = self._current_price(symbol)
            if current_price is None:
                lines.append(f"  {symbol}: anlık fiyat henüz alınamadı")
                continue

            pnl_pct = self._position_manager.raw_move_pct(position, current_price)
            peak_price = position.peak_favorable_price or position.entry_price
            peak_pnl_pct = self._position_manager.raw_move_pct(position, peak_price)

            tp_str = f"{position.tp_price:.6g}" if position.tp_price is not None else "—"
            stop_str = f"{position.stop_price:.6g}" if position.stop_price is not None else "—"

            lines.append(
                f"  {symbol} {position.side} | giriş={position.entry_price:.6g} "
                f"anlık={current_price:.6g} | TP={tp_str} SL={stop_str} | "
                f"PNL(ham fiyat)=%{pnl_pct:+.2f} en_yüksek=%{peak_pnl_pct:+.2f}"
            )

        logger.info("İşlem takibi (%d açık pozisyon):\n%s", len(positions), "\n".join(lines))

    # ------------------------------------------------------------------ #
    def _current_price(self, symbol: str) -> float | None:
        ob = self._data_layer.get_orderbook(symbol)
        if ob is not None and ob.mid_price is not None:
            return ob.mid_price
        candles = self._data_layer.get_klines(symbol, "1m", limit=1)
        return candles[-1].close if candles else None

    def _detect_reversal(self, symbol: str, position_side: str) -> bool:
        """1m ve 5m'de pozisyonun TERSİ yönde CHoCH var mı — zirve/tükeniş sezgisi."""
        opposite_trend = Trend.DOWN if position_side == "LONG" else Trend.UP
        prevailing = Trend.UP if position_side == "LONG" else Trend.DOWN
        for interval in ("1m", "5m"):
            candles = self._data_layer.get_klines(symbol, interval)
            if not candles:
                continue
            swings = find_swing_points(candles, lookback=2)
            brk = detect_structure_break(candles, swings, prevailing)
            if brk is not None and brk.kind == "CHoCH" and brk.direction == opposite_trend:
                return True
        return False

    def _detect_resumption(self, symbol: str, side: str) -> bool:
        """Düzeltme sonrası hareketin kaldığı yerden (orijinal yönde) devam
        ettiğinin sezgisi: 1m'de o yönde tekrar BOS."""
        prevailing = Trend.UP if side == "LONG" else Trend.DOWN
        candles = self._data_layer.get_klines(symbol, "1m")
        if not candles:
            return False
        swings = find_swing_points(candles, lookback=2)
        brk = detect_structure_break(candles, swings, prevailing)
        return brk is not None and brk.kind == "BOS" and brk.direction == prevailing
