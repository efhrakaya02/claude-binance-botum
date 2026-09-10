# Trading Bot — Binance Futures Price Action Botu

Çoklu zaman dilimi price-action stratejisiyle çalışan, orderbook/likidasyon
farkındalıklı bir Binance Futures (USDT-M) işlem botu. Railway üzerinde
Docker ile 7/24 arka planda çalışacak şekilde paketlenmiştir.

## Repo yapısı

```
repo-kökü/
├── Dockerfile              # Railway bu dosyayla imaj build eder
├── railway.toml             # Railway build/deploy ayarları
├── .dockerignore
├── .gitignore
├── .env.example              # yerel geliştirme için örnek; Railway'de KULLANILMAZ (env var'lar panelden girilir)
├── requirements.txt
├── README.md
└── trading_bot/               # asıl Python paketi
    ├── __init__.py
    ├── config.py               # tüm ayarlar (risk, scanner, likidasyon parametreleri)
    ├── main.py                  # giriş noktası
    ├── orchestrator.py          # tüm modülleri birbirine bağlayan ana döngü
    ├── data_layer/               # Binance WS/REST veri toplama (kline, orderbook, likidasyon, OI, funding)
    ├── scanner/                   # gainers/losers/volume top-50 taraması + erken hareket tespiti
    ├── analyzer/                   # 4h makro yön, 1h giriş onayı, 15m/5m/1m zamanlama (saf price action)
    ├── liquidation_engine/          # giriş güvenliği kontrolü + gerçek zamanlı sweep tespiti
    ├── risk_manager/                 # breakeven/trailing mantığı, sweep sonrası çıkış/yeniden giriş
    ├── execution/                     # Binance Futures'a imzalı emir gönderimi
    └── examples/                       # modülleri tek tek test etmek için scriptler
```

**Önemli:** `trading_bot/` klasörü bir Python paketidir ve içindeki dosyalar
birbirini `from . import ...` / `from ..data_layer import ...` şeklinde
göreli olarak import eder. Bu yüzden bu klasör kesinlikle bir alt klasör
olarak kalmalı — içeriğini repo köküne "düzleştirip" taşımayın, yoksa
`python -m trading_bot.main` çalışmaz.

## Strateji özeti

- **4h**: makro yön (price-action swing yapısı — RSI/MACD gibi indikatör kullanılmıyor)
- **1h**: işlem açma kararı (1h yapısı makro yönle uyumlu mu + BOS var mı)
- **15m/5m/1m**: hacim anomalisi + momentum ile giriş zamanlaması
- **Scanner**: tüm USDT-M evrenini tarar, gainers/losers/volume top-50 listelerinde
  hızla tırmanan (henüz tepeye varmamış) sembolleri erken yakalamaya çalışır
- **Liquidation Engine**: kendi modelimizle (OI + funding rate'ten) tahmini likidasyon
  kümeleri hesaplar; giriş bu kümelere veya büyük orderbook duvarlarına çok yakınsa
  işlem açılmaz; pozisyon açıkken de sürekli sweep (ani likidasyon patlaması) izler
- **Risk Manager**: +%1'de breakeven, +%1.5'te trailing (her +%0.3'te kazancın
  yarısını kilitleme), sweep tespitinde hızlı çıkış + fırsatı izlemeye alıp
  hareket devam ederse yeniden giriş
- **Limitler**: aynı anda en fazla 3 pozisyon, pozisyon başına 10 USDT margin,
  en fazla 5x kaldıraç, isolated margin

## Yerel kurulum (geliştirme/test için)

```bash
git clone <bu-repo>
cd <repo-klasörü>
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
# .env içine kendi Binance API key/secret'ını yaz
python -m trading_bot.main
```

`.env` içindeki `BINANCE_TESTNET=true` ile önce testnet'te (gerçek para riski
olmadan) test etmen şiddetle önerilir.

## Railway üzerinde deploy

1. Bu repoyu GitHub'a push et (aşağıdaki "GitHub'a push" bölümüne bak).
2. [railway.app](https://railway.app) → **New Project** → **Deploy from GitHub repo** →
   bu repoyu seç. Railway `Dockerfile`'ı otomatik algılayıp onunla build eder
   (`railway.toml` build ayarlarını zaten `DOCKERFILE` olarak sabitliyor).
3. Proje oluşunca **Variables** sekmesine gir ve şunları tek tek ekle
   (`.env` dosyası Railway'e YÜKLENMEZ, image'a da kopyalanmaz — her şeyi
   panelden environment variable olarak girmen gerekiyor):
   - `BINANCE_API_KEY`
   - `BINANCE_API_SECRET`
   - `BINANCE_TESTNET` → önce `true` ile test et, sonra `false` yap
   - `DRY_RUN` → `true` ise hiçbir gerçek emir göndermez, sadece ne yapacağını
     loglar (gerçek piyasa verisiyle, sıfır finansal risk). API key/secret
     bile gerekmez. Botun mantığını canlı veriyle güvenle gözlemlemenin en
     hızlı yolu budur — testnet'in aksine gerçek fiyat/hacim/likidasyon
     davranışını görürsün.
4. **Deployments** sekmesinden build/deploy loglarını izle. Başarılı olunca
   `Bot çalışıyor. Durdurmak için Ctrl+C.` benzeri bir log satırı görürsün
   (Railway'de Ctrl+C senin elinde değil ama log mesajı orchestrator'ın
   başladığını doğrular).
5. Bu bir arka plan işlemi (worker) olduğu için **hiçbir domain/port
   açman gerekmiyor** — Railway'in "Generate Domain" adımını atlayabilirsin.
6. Kod her değiştiğinde `git push` yeterli — Railway GitHub entegrasyonuyla
   otomatik yeniden build/deploy eder.

### Railway'de dikkat edilmesi gerekenler

- **Sürekli çalışma**: Railway container'ı çökerse (örn. yakalanmamış bir
  exception) `railway.toml`'daki `restartPolicyType = "ON_FAILURE"` sayesinde
  otomatik yeniden başlar. Yine de Deployments loglarını düzenli kontrol et.
- **Sırlar**: API key/secret'ı asla koda veya `.env.example`'a yazma — sadece
  Railway'in Variables panelinden gir. Repo public ise bu daha da kritik.
- **Bölge/gecikme (latency)**: Railway'in sunucu bölgesi Binance'in API
  sunucularına (genelde Tokyo/Singapur bölgeleri en düşük gecikmeyi verir)
  uzaksa emir gecikmesi artar; Railway'in proje ayarlarından bölge
  seçebiliyorsan (plan'a göre değişir) Asya'ya yakın bir bölge tercih et.
- **Maliyet**: Bot 7/24 açık kalan bir worker olduğu için Railway'in
  kullanım-bazlı ücretlendirmesinde sürekli CPU/RAM tüketir — free tier
  kotasını hızlı tüketebilir, bir ücretli plana geçmen gerekebilir.
- **Tek instance**: Aynı API key ile birden fazla instance/replica ÇALIŞTIRMA
  — iki bot aynı anda aynı hesapta emir açmaya çalışırsa pozisyon limiti ve
  risk mantığı (3 slot vb.) çakışır. Railway'de replica sayısını 1'de tut.

## GitHub'a push

```bash
cd <repo-klasörü>
git init
git add .
git commit -m "İlk commit: bot mimarisi + Railway deploy dosyaları"
git branch -M main
git remote add origin https://github.com/<kullanici-adin>/<repo-adi>.git
git push -u origin main
```

Push'tan sonra GitHub'daki repo sayfasında `.env` dosyasının **görünmediğini**
doğrula (`.gitignore` içinde listeli). Görünüyorsa hemen repoyu private yap,
Binance'te o API key'i iptal edip yenisini oluştur.

## ÖNEMLİ UYARILAR

- Bu bot **gerçek para ile market emri açar**. Kodun tamamını anlamadan,
  testnet'te uzun süre çalıştırıp davranışını gözlemlemeden canlıya almayın.
- `analyzer` ve `risk_manager` içindeki "zirve tespiti" / "reversal signal"
  sezgisel kurallardır — kesin bir tahmin motoru değildir. Gerçek verilerle
  backtest edip parametreleri (config.py) ayarlamak gerekir.
- Likidasyon kümeleri **tahminidir** (kendi modelimiz), Binance'in resmi bir
  likidasyon haritası API'si değildir.
- API key'lerine sadece **Futures trading** izni verin, **withdrawal (para
  çekme) izni KESİNLİKLE vermeyin**.

## Modülleri tek tek test etme

Her modül bağımsız import edilip test edilebilir, örn.:

```bash
python -m trading_bot.examples.test_data_layer
```
