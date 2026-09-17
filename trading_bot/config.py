"""
Proje geneli konfigürasyon.
Tüm modüller ayarları buradan okuyacak; ileride .env / config dosyasına
taşınabilir ama şimdilik tek yerden yönetiyoruz.
"""

from dataclasses import dataclass, field


# ---- Binance Futures uç noktaları -----------------------------------------
BINANCE_FUTURES_WS_BASE = "wss://fstream.binance.com/ws"
BINANCE_FUTURES_REST_BASE = "https://fapi.binance.com"

# Testnet (gerçek para riskine girmeden test etmek için)
BINANCE_FUTURES_WS_BASE_TESTNET = "wss://stream.binancefuture.com/ws"
BINANCE_FUTURES_REST_BASE_TESTNET = "https://testnet.binancefuture.com"


# ---- Zaman dilimleri --------------------------------------------------------
# 4h: makro yön, 1h: işlem açma kararı, 15m/5m/1m: hacim+momentum ile
# giriş zamanlaması.
KLINE_INTERVALS = ["4h", "1h", "15m", "5m", "1m"]

# Her (sembol, interval) çifti için bellekte tutulacak maksimum mum sayısı.
# Analyzer modülü indikatör hesaplarken buradan geriye dönük veri çekecek.
MAX_KLINE_HISTORY = {
    "4h": 300,
    "1h": 500,
    "15m": 500,
    "5m": 500,
    "1m": 500,
}

# ---- Orderbook ---------------------------------------------------------
ORDERBOOK_DEPTH_LEVELS = "20"   # 5, 10 veya 20
ORDERBOOK_UPDATE_SPEED = "100ms"  # 100ms veya 250ms/500ms

# ---- Likidasyon akışı --------------------------------------------------
# Binance'in global likidasyon stream'i (!forceOrder@arr) TÜM semboller için
# gerçekleşmiş likidasyonları verir; sembol bazlı abonelik gerekmez.
LIQUIDATION_STREAM = "!forceOrder@arr"
LIQUIDATION_BUFFER_SIZE = 5000  # bellekte tutulacak son likidasyon olayı sayısı

# ---- REST polling (OI + funding) ---------------------------------------
# Kendi likidasyon tahmin modelimiz için open interest ve funding rate'i
# periyodik olarak REST üzerinden çekiyoruz (bu veriler websocket'te yok).
OI_FUNDING_POLL_INTERVAL_SECONDS = 60

# ---- Bağlantı dayanıklılığı ---------------------------------------------
WS_RECONNECT_MIN_BACKOFF_SECONDS = 1
WS_RECONNECT_MAX_BACKOFF_SECONDS = 30
WS_PING_TIMEOUT_SECONDS = 10
# Binance, bir WS bağlantısı üzerinden saniyede ~5 SUBSCRIBE/UNSUBSCRIBE
# yönetim mesajına izin veriyor; aşılırsa 1008 (policy violation) ile
# bağlantıyı kapatıyor. 4 (5'in altında) kullanarak güvenli mesafe bırakıyoruz.
WS_SUBSCRIBE_RATE_LIMIT_PER_SECOND = 4


@dataclass
class RiskConfig:
    """Pozisyon yönetimi parametreleri (Risk & Position Manager modülünde kullanılacak,
    şimdiden burada tutuyoruz çünkü Data Layer'daki bazı buffer boyutları buna bağlı).

    Üç fazlı stop mantığı:
      Faz A (0% -> trailing_activate_pct): ATR bazlı SABİT başlangıç stop'u
        (entry_price ± ATR*atr_multiplier). Coin'in kendi volatilitesine göre
        ayarlanır — dar mumlu (düşük volatilite) bir coin'de dar, geniş mumlu
        bir coin'de geniş bir stop demektir; sabit bir yüzdeden daha isabetli.
      Faz B (trailing_activate_pct -> breakeven_trigger_pct): ATR bazlı
        TRAILING stop — stop, anlık fiyatı ATR*atr_multiplier mesafeden takip
        eder, sadece lehte yönde güncellenir.
      Faz C (breakeven_trigger_pct'ten itibaren): stop en az breakeven'a
        zorlanır, ardından "stop = zirvenin trailing_lock_ratio kadarı"
        formülüyle kâr kademe kademe kilitlenir (zirve yükseldikçe stop da
        yükselir, asla geri gitmez).
    """
    # Sermaye küçükken (şu an ~3.82 USDT) 2 pozisyonu aynı anda ince ince
    # bölmek yerine TEK, en kaliteli fırsata odaklanmak hem komisyon yükünü
    # yarıya indiriyor hem de sermayeyi daha güçlü kullanıyor.
    max_concurrent_positions: int = 1
    # 3 USDT marj (5x ile 15 USDT'lik pozisyon), 3.82 USDT bakiyede ~0.8 USDT
    # komisyon/dalgalanma payı bırakıyor.
    margin_per_position_usdt: float = 3.0
    max_leverage: int = 5
    margin_mode: str = "ISOLATED"

    # --- ATR (Average True Range) ayarları ---
    atr_period: int = 14
    atr_timeframe: str = "1h"
    # Hem Faz A'daki (başlangıç) hem Faz B'deki (trailing) ATR mesafesi için
    # kullanılır: stop_mesafesi = ATR * atr_multiplier.
    # 1.5'ten 2.5'e yükseltildi: LSKUSDT raporunda görülen "gerçek bir trendin
    # ortasındaki sıradan geri çekilmelerde bile stop'a takılıp hemen tekrar
    # girme" paterni, büyük trendleri sonuna kadar taşıyabilmek için erken
    # fazların gürültüye karşı daha toleranslı olmasını gerektiriyor.
    atr_multiplier: float = 2.5

    trailing_activate_pct: float = 1.5      # Faz A -> Faz B geçişi (ham fiyat hareketi)
    breakeven_trigger_pct: float = 2.5      # Faz B -> Faz C geçişi: stop breakeven'a zorlanır
    # Faz C'de stop her zaman "zirvenin (girişten itibaren kat edilen en
    # yüksek ham fiyat mesafesinin) bu oranı" seviyesinde tutulur.
    #
    # ÖNEMLİ FELSEFE: Bu stop artık "asıl çıkış tetikleyicisi" DEĞİL, geniş bir
    # FELAKET GÜVENLİK AĞI — "momentum devam ettiği sürece pozisyonu açık tut"
    # kararını asıl veren şey reversal_signal (15m CHoCH, gerçek zamanlı
    # momentum kontrolü). 0.6 iken (zirvenin %60'ı) bu stop çok sık, sıradan
    # bir geri çekilmede bile reversal_signal'dan ÖNCE tetikleniyordu (örn.
    # CAPUSDT: momentum hâlâ sürerken salt bu oran yüzünden kapandı). 0.35'e
    # düşürüldü — artık sadece gerçekten büyük bir geri çekilmede devreye
    # giriyor, normal şartlarda kararı 15m CHoCH veriyor.
    trailing_lock_ratio: float = 0.35


@dataclass
class AnalyzerConfig:
    """MultiTimeframeAnalyzer ağırlıkları ve eşiği.

    Eskiden 1h onayı (uyum + BOS) ve 15m/5m/1m zamanlaması ("3'ten en az 2'si")
    KATI (all-or-nothing) eşiklerdi — sınırın hemen altında kalan geçerli
    kurulumlar tamamen reddediliyordu. Artık her bileşen SÜREKLİ bir skora
    (0 ile bileşenin ağırlığı arasında) dönüşüyor ve nihai karar toplam
    confidence skorunun `actionable_confidence_threshold`'u geçip geçmediğine
    bakılarak veriliyor — eksik kanıt diğer bileşenlerin gücüyle telafi
    edilebiliyor, ama hâlâ gerçek yapısal/hacimsel kanıt şart.
    """
    macro_weight: float = 30.0     # 4h yönü net mi (bu hâlâ katı bir kapı — RANGE ise sinyal yok)
    entry_weight: float = 35.0     # 1h uyum + BOS bileşeninin toplam ağırlığı
    timing_weight: float = 35.0    # 15m/5m/1m hacim+momentum bileşeninin toplam ağırlığı
    timing_target_volume_ratio: float = 1.8  # bu orana ULAŞMASA bile kısmi puan verilir (önceki: 1.5)
    # Sermaye çok küçükken (şu an ~3.82 USDT) her işlem komisyon açısından
    # nispeten pahalı — az ama isabetli işlem stratejisi için 65'ten 72'ye
    # yükseltildi.
    actionable_confidence_threshold: float = 72.0
    # Fiyat, son 1h taban/tepesinden bu yüzdeden fazla uzaklaşmışsa "artık geç
    # kalınmış" sayılıp KATI bir kapıyla reddedilir — çoklu zaman dilimi onay
    # zincirinin (4h+1h) doğal gecikmesi yüzünden, onay geldiğinde hareket
    # genelde zaten ilerlemiş olur; bu kontrol "momentum bitip düzeltmeye
    # geçtiğinde giriş yapma" sorununu doğrudan hedefliyor.
    max_extension_pct: float = 15.0


@dataclass
class ScannerConfig:
    """Scanner modülü ayarları (şimdilik sabit, ileride scanner.py'ye taşınacak)."""
    top_n_gainers: int = 35   # önceki: 50 — daha az ama daha ekstrem hareketlere odaklan
    top_n_losers: int = 35    # önceki: 50
    top_n_volume: int = 35    # önceki: 50
    rescan_interval_seconds: int = 300  # 5 dakika


@dataclass
class LiquidationConfig:
    """Liquidation & Orderbook Engine ayarları — kendi tahmin modelimiz için."""
    # Bir işlem seviyesinin (giriş fiyatının) tahmini likidasyon kümesine veya
    # büyük orderbook duvarına ne kadar yakın olması "tehlikeli" sayılır.
    min_safe_distance_pct: float = 0.5
    # Orderbook'ta "duvar" sayılması için bir seviyenin, ortalama seviye
    # büyüklüğünün kaç katı olması gerektiği.
    orderbook_wall_multiplier: float = 4.0
    # Varsayılan kaldıraç dağılımı (retail trader'ların yoğunlaştığı seviyeler)
    # ve göreli ağırlıkları — toplamı 1.0 olacak şekilde normalize edilir.
    assumed_leverage_weights: dict = field(
        default_factory=lambda: {5: 0.15, 10: 0.25, 20: 0.25, 25: 0.15, 50: 0.12, 75: 0.05, 100: 0.03}
    )
    maintenance_margin_factor: float = 0.9  # basitleştirilmiş bakım marjı katsayısı
    # Sweep tespiti: son N saniyede tek yönde biriken likidasyon hacmi bu
    # eşiği (USDT) aşarsa "sweep" sayılır.
    sweep_window_seconds: float = 20.0
    sweep_liquidation_usdt_threshold: float = 50_000.0
    # Orderbook derinliğinin bu oranın altına ani düşüşü de sweep sinyali sayılır.
    sweep_depth_drop_ratio: float = 0.4
