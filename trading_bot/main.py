"""
Botun ana giriş noktası.

Çalıştırmadan önce:
    cp .env.example .env
    # .env içine kendi API key/secret'ını yaz
    pip install -r requirements.txt
    python -m trading_bot.main

UYARI: Bu bot gerçek para ile işlem açar. Önce mutlaka testnet ile
(BINANCE_TESTNET=true) ve/veya küçük miktarlarla test et.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal

from .orchestrator import Orchestrator

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def _load_env_file(path: str = ".env") -> None:
    """Basit .env yükleyici — python-dotenv'e bağımlılık eklememek için."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


async def main() -> None:
    _load_env_file()

    api_key = os.environ.get("BINANCE_API_KEY")
    api_secret = os.environ.get("BINANCE_API_SECRET")
    testnet = os.environ.get("BINANCE_TESTNET", "false").lower() == "true"
    dry_run = os.environ.get("DRY_RUN", "false").lower() == "true"

    if not dry_run and (not api_key or not api_secret):
        raise RuntimeError(
            "BINANCE_API_KEY / BINANCE_API_SECRET tanımlı değil. "
            ".env dosyasını oluşturup doldurduğundan emin ol (.env.example'a bak). "
            "Gerçek emir göndermeden test etmek istiyorsan DRY_RUN=true ayarlayabilirsin "
            "— o modda API key gerekmez."
        )
    # Dry run'da imzalı hiçbir çağrı yapılmıyor; client yine de örneklenir
    # (kod basitliği için) ama boş anahtarlarla asla kullanılmaz.
    api_key = api_key or "dry-run-placeholder"
    api_secret = api_secret or "dry-run-placeholder"

    orchestrator = Orchestrator(api_key=api_key, api_secret=api_secret, testnet=testnet, dry_run=dry_run)
    await orchestrator.start()

    stop_event = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass  # Windows'ta bazı sinyaller desteklenmeyebilir

    logger.info("Bot çalışıyor. Durdurmak için Ctrl+C.")
    await stop_event.wait()

    logger.info("Kapatma sinyali alındı, temizleniyor...")
    await orchestrator.stop()


if __name__ == "__main__":
    asyncio.run(main())
