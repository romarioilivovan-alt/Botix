#!/usr/bin/env python3
"""Test MEXC API to see what's happening with the 0-fee list."""

import asyncio
import sys
from pathlib import Path

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent))

from backend.mexc_trader import MexcTrader
from backend.models import UserAccount

async def main():
    print("Testing MEXC API...")
    print("=" * 80)

    # Create trader with empty credentials (public API only)
    acc = UserAccount(uid="", device_id="", mhash="", proxy="")
    trader = MexcTrader(acc)

    try:
        print("\n1. Fetching 0-fee symbols list...")
        symbols = await trader.list_zero_fee_symbols()
        print(f"   Result: {len(symbols)} symbols")

        if symbols:
            print(f"\n   First 10 symbols:")
            for sym in symbols[:10]:
                print(f"     - {sym}")

            # Check our target symbols
            print(f"\n   Checking target symbols:")
            targets = ['ENA_USDT', 'NVIDIA_USDT', 'MSTRSTOCK_USDT', 'TAO_USDT', 'BCH_USDT']
            for sym in targets:
                found = sym in symbols
                status = 'FOUND' if found else 'NOT FOUND'
                print(f"     {sym:20} {status}")
        else:
            print("   ⚠️ WARNING: API returned 0 symbols!")
            print("\n2. Testing raw API call...")
            data = await trader.api._request_market("api/v1/contract/detail")
            print(f"   Success: {data.get('success')}")
            print(f"   Data keys: {list(data.keys())}")
            if 'data' in data:
                print(f"   Data type: {type(data['data'])}")
                if isinstance(data['data'], dict):
                    print(f"   Data dict keys: {list(data['data'].keys())}")
                elif isinstance(data['data'], list):
                    print(f"   Data list length: {len(data['data'])}")
                    if data['data']:
                        print(f"   First item keys: {list(data['data'][0].keys()) if isinstance(data['data'][0], dict) else 'not a dict'}")

    except Exception as e:
        print(f"   ERROR: {e}")
        import traceback
        traceback.print_exc()

    finally:
        await trader.close()

    print("\n" + "=" * 80)
    print("Test complete")

if __name__ == "__main__":
    asyncio.run(main())
