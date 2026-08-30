import asyncio
import json
import sys
sys.path.insert(0, '/root/moex-assistant/Учебный/moex-assistant')

from core.georisk_agent import GeoRiskAgent

async def main():
    agent = GeoRiskAgent()
    result = await agent.scan(max_items=5)
    if result is None:
        print('result: None')
        return
    print('score:', result.score)
    print('severity:', result.severity)
    print('summary:', result.summary)
    print('affected_sectors:', result.affected_sectors)
    print('trigger_keywords:', result.trigger_keywords)
    print('news_items:')
    print(json.dumps(result.news_items, ensure_ascii=False, indent=2))

asyncio.run(main())
