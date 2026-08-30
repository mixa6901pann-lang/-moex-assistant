import asyncio
import sys
sys.path.insert(0, '/root/moex-assistant/Учебный/moex-assistant')

from core.llm import _call_ollama

async def main():
    try:
        r = await _call_ollama("Ты — помощник. Ответь JSON.", "Скажи привет в JSON: {\"hello\": true}", max_tokens=64)
        print('raw response:', repr(r))
    except Exception as e:
        print('exception:', type(e).__name__, e)

asyncio.run(main())
