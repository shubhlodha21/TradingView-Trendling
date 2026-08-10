import sys
sys.path.insert(0, 'src')

try:
    import nest_asyncio
    nest_asyncio.apply()
except:
    pass

from ib_async import IB

ib = IB()
try:
    ib.connect('127.0.0.1', 7497, clientId=99)
    print('Connected to TWS on port 7497!')
    print('Waiting 2 seconds...')
    import time
    time.sleep(2)
    print('Open orders:', ib.openOrders())
    ib.disconnect()
    print('Disconnected')
except Exception as e:
    print(f'Connection failed: {e}')
    print('Make sure TWS is running with API enabled on port 7497')
