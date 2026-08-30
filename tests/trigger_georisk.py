import asyncio
import sys
sys.path.insert(0, '/root/moex-app')

from main import TradingBot
from core import db

async def main():
    bot = TradingBot()
    await bot.geo_risk_scan()
    print('geo_risk_scan completed')

asyncio.run(main())
