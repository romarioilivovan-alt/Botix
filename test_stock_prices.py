"""Test script to investigate how stocks get fair prices on MEXC."""
import asyncio
import aiohttp
import json

STOCK_SYMBOLS = ['NVDA_USDT', 'MSTR_USDT', 'TSLA_USDT', 'INTC_USDT']

async def check_mexc_contract_details():
    """Check MEXC contract detail API for stock symbols."""
    print("=== Checking MEXC Contract Details ===\n")
    
    async with aiohttp.ClientSession() as session:
        url = 'https://contract.mexc.com/api/v1/contract/detail'
        try:
            async with session.get(url, timeout=10) as r:
                data = await r.json()
                if data.get('success'):
                    contracts = data.get('data', [])
                    print(f"Total contracts: {len(contracts)}\n")
                    
                    for sym in STOCK_SYMBOLS:
                        matching = [c for c in contracts if c.get('symbol') == sym]
                        if matching:
                            c = matching[0]
                            print(f"{sym}:")
                            print(f"  indexPrice: {c.get('indexPrice')}")
                            print(f"  fairPrice: {c.get('fairPrice')}")
                            print(f"  lastPrice: {c.get('lastPrice')}")
                            print(f"  riseFallRate: {c.get('riseFallRate')}")
                            print()
                        else:
                            print(f"{sym}: NOT FOUND\n")
                else:
                    print(f"API returned success=false: {data}")
        except Exception as e:
            print(f"Error fetching MEXC data: {e}")

async def check_binance_futures():
    """Check if stocks exist on Binance Futures."""
    print("\n=== Checking Binance Futures ===\n")
    
    async with aiohttp.ClientSession() as session:
        url = 'https://fapi.binance.com/fapi/v1/exchangeInfo'
        try:
            async with session.get(url, timeout=10) as r:
                data = await r.json()
                symbols = {s['symbol'] for s in data['symbols']}
                
                for sym in STOCK_SYMBOLS:
                    binance_sym = sym.replace('_', '')
                    exists = binance_sym in symbols
                    print(f"{sym} ({binance_sym}): {'EXISTS' if exists else 'NOT FOUND'}")
        except Exception as e:
            print(f"Error fetching Binance data: {e}")

async def main():
    await check_mexc_contract_details()
    await check_binance_futures()
    
    print("\n=== Conclusion ===")
    print("If MEXC provides indexPrice/fairPrice for stocks:")
    print("  -> Use MEXC index price as fair (Option A)")
    print("If MEXC doesn't provide index price:")
    print("  -> Need NYSE WebSocket (Option B)")

if __name__ == '__main__':
    asyncio.run(main())
