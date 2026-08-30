import asyncio
import sys
sys.path.insert(0, '/root/moex-assistant/Учебный/moex-assistant')

from core.georisk_agent import GeoRiskAgent
from core import db

async def main():
    agent = GeoRiskAgent()
    result = await agent.scan(max_items=5)
    if result is None:
        print('result: None')
        return
    print('score:', result.score)
    print('severity:', result.severity)
    print('news_items count:', len(result.news_items or []))
    await db.save_georisk(
        score=result.score,
        severity=result.severity,
        summary=result.summary,
        affected_sectors=result.affected_sectors,
        trigger_keywords=result.trigger_keywords,
        news_items=result.news_items,
    )
    latest = await db.get_latest_georisk()
    print('saved latest:', latest)

asyncio.run(main())
