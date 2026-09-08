"""
Data Layer'ı tek başına test etmek için örnek script.

Çalıştırmak için (proje kök dizininden):
    pip install -r requirements.txt
    python -m examples.test_data_layer

Not: Binance'e gerçek ağ erişimi gerektirir; bu sandbox ortamında
çalıştırılamaz, kendi makinende/sunucunda test etmelisin.
"""

import asyncio
import logging

from trading_bot.data_layer import DataLayer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


async def main() -> None:
    layer = DataLayer(testnet=False)
    await layer.start()

    # Scanner henüz yazılmadığı için sembolleri elle ekliyoruz.
    for symbol in ["BTCUSDT", "ETHUSDT"]:
        await layer.add_symbol(symbol)

    try:
        for _ in range(6):  # ~30 saniye boyunca periyodik durum yazdır
            await asyncio.sleep(5)
            for symbol in layer.watched_symbols:
                candles_1h = layer.get_klines(symbol, "1h", limit=3)
                ob = layer.get_orderbook(symbol)
                recent_liqs = layer.get_recent_liquidations(symbol, since_seconds=60)
                oi = layer.get_open_interest(symbol)
                funding = layer.get_funding_rate(symbol)

                print(f"\n--- {symbol} ---")
                print(f"Son 1h mumlar: {len(candles_1h)} adet, son kapanış: "
                      f"{candles_1h[-1].close if candles_1h else 'yok'}")
                print(f"Orderbook mid price: {ob.mid_price if ob else 'yok'}")
                print(f"Son 60sn'deki likidasyon sayısı: {len(recent_liqs)}")
                print(f"Open interest: {oi.open_interest_value if oi else 'yok'}")
                print(f"Funding rate: {funding.last_funding_rate if funding else 'yok'}")
    finally:
        await layer.stop()


if __name__ == "__main__":
    asyncio.run(main())
