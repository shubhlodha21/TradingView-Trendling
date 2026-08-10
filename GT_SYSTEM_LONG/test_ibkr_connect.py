#!/usr/bin/env python3
"""Simple test to check IBKR connection."""
import asyncio
import sys

sys.path.insert(0, 'src')

async def test_connection():
    from src.execution.broker import Gateway
    
    print("Testing IBKR connection...")
    print("Port 4001 = TWS, Port 4002 = Gateway")
    
    for port in [4002, 4001]:
        print(f"\nTrying port {port}...")
        gw = Gateway(port=port, client_id=999)
        if await gw.connect():
            print(f"  SUCCESS on port {port}!")
            print(f"  Connected: {gw.connected}")
            print(f"  Status: {gw.status}")
            await gw.disconnect()
            return True
        else:
            print(f"  Failed: {gw.status}")
    
    print("\nCould not connect on any port.")
    print("Is IBKR Gateway/TWS running?")
    return False

if __name__ == "__main__":
    result = asyncio.run(test_connection())
    sys.exit(0 if result else 1)
