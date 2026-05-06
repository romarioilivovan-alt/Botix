"""Verify Binance provides stock futures data."""
import asyncio
import aiohttp

async def check_binance_stocks():
    print("=== Checking Binance Futures Stock Symbols ===\n")
    
    stocks = ['NVDAUSDT', 'MSTRUSDT', 'TSLAUSDT', 'INTCUSDT']
    
    async with aiohttp.ClientSession() as session:
        url = 'https://fapi.binance.com/fapi/v1/exchangeInfo'
        async with session.get(url, timeout=10) as r:
            data = await r.json()
            
            for stock_sym in stocks:
                matching = [s for s in data['symbols'] if s['symbol'] == stock_sym]
                if matching:
                    s = matching[0]
                    print(f"{stock_sym}:")
                    print(f"  Status: {s['status']}")
                    print(f"  Contract Type: {s['contractType']}")
                    print(f"  Quote Asset: {s['quoteAsset']}")
                    print(f"  Price Precision: {s['pricePrecision']}")
                    print()
        
        # Check current prices
        print("=== Current Prices ===\n")
        url = 'https://fapi.binance.com/fapi/v1/ticker/price'
        async with session.get(url, timeout=10) as r:
            prices = await r.json()
            price_map = {p['symbol']: p['price'] for p in prices}
            
            for stock_sym in stocks:
                if stock_sym in price_map:
                    print(f"{stock_sym}: ${price_map[stock_sym]}")

asyncio.run(check_binance_stocks())
