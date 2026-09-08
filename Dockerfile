# Hafif, resmi Python imajı — bot bir arka plan işlemi (worker), HTTP sunucusu değil.
FROM python:3.11-slim

# Loglar anında görünsün (Railway log akışı için önemli), .pyc dosyaları yazılmasın.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Önce sadece requirements.txt kopyalanır ki kod değiştikçe bağımlılıklar
# yeniden kurulmasın (Docker layer cache'ten faydalanmak için).
COPY requirements.txt .
RUN pip install -r requirements.txt

# Paketin tamamını kopyala
COPY trading_bot/ trading_bot/

# Railway ortam değişkenlerini (BINANCE_API_KEY vb.) proje ayarlarından
# enjekte eder; .env dosyası image içine KOPYALANMAZ (.dockerignore'a bak).
CMD ["python", "-m", "trading_bot.main"]
