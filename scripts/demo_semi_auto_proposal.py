"""Demo: print a semi-auto trading proposal to the terminal.

This script does not call any broker APIs or place orders. It only
simulates the proposal output so the user can see the terminal format.
"""

from __future__ import annotations

from strategies.fees import estimate_trade_costs


def main():
    ticker = "SBER"
    action = "buy"
    market_price = 249.34
    stop_px = 245.00
    take_px = 255.00
    qty = 10

    fee_est = estimate_trade_costs(market_price, take_px, qty)

    proposal = (
        f"[ROBOT] Предлагаю сделку\n"
        f"{action.upper()} {ticker}\n"
        f"Цена: {market_price} RUB | Кол-во: {qty}\n"
        f"Стоп: {stop_px} RUB | Цель: {take_px} RUB\n"
        f"Риск: ~{market_price - stop_px:.2f} RUB на акцию\n"
        f"Комиссия (~): {fee_est.total_commission_rub} RUB\n"
        f"Чистая прибыль при цели: {fee_est.net_profit_pct}%\n"
        f"Причина: отскок от дневного low, RSI в зоне перепроданности\n\n"
        f"Для подтверждения напиши: /confirm_trade {ticker} {action} {qty}"
    )

    print("\n" + "=" * 50)
    print(proposal)
    print("=" * 50 + "\n")


if __name__ == "__main__":
    main()
