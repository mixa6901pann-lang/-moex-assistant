import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import db
from core.analyzer import analyze_ticker
from core.moex import MoexClient
from strategies.indicators import df_from_candles, run_screener, score_stock

TICKERS = ['SBER', 'GAZP', 'LKOH', 'NVTK', 'GMKN']


async def fetch(ticker):
    return await state['moex'].candles_recent(ticker, count=100)


async def main():
    moex = MoexClient()
    state['moex'] = moex
    try:
        macro = None

        try:
            index_candles = await moex.index_candles('IMOEX', interval='1d', count=30)
        except Exception as e:
            print(f'IMOEX fetch failed: {e}')
            index_candles = None

        results = await run_screener(fetch, tickers=TICKERS, index_candles=index_candles)

        print('=' * 70)
        print(f'SCREENER RESULTS ({len(results)} tickers)')
        print('=' * 70)
        for r in results:
            print('%s | score=%3.0f | rec=%-20s | signals=%s' % (
                r['ticker'].ljust(6), r['score'], r.get('recommendation', '?'),
                r.get('signals', [])[:3]))

        top = results[:3]
        print()
        print('=' * 70)
        print('LLM ANALYSIS (yandex) - top %d tickers' % len(top))
        print('=' * 70)

        for r in top:
            ticker = r['ticker']
            print()
            print('--- %s (score=%.0f, rec=%s) ---' % (
                ticker, r['score'], r.get('recommendation')))
            price_data = {
                'direction': r.get('direction'),
                'strength': r.get('strength'),
                'recommendation': r.get('recommendation'),
                'reason': r.get('reason', ''),
                'warnings': r.get('warnings', []),
                'score': r.get('score'),
                'details': r.get('details', {}),
            }
            try:
                analysis, critic = await analyze_ticker(
                    ticker=ticker,
                    price_data=price_data,
                    signals=r.get('signals', []),
                    news='',
                    macro=macro,
                )
                print('ANALYST:')
                print(analysis)
                print()
                print('CRITIC:')
                print(critic)
            except Exception as e:
                import traceback
                traceback.print_exc()
    finally:
        try:
            await asyncio.wait_for(moex.close(), timeout=5)
        except Exception:
            pass


state = {}
asyncio.run(main())
