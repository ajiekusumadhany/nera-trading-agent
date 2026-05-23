"""
test_trader.py - Test koneksi Binance Testnet & eksekusi order
Jalankan: python3 test_trader.py
"""

import logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

from trader import BinanceTrader
from monte_carlo import SimulationResult


def test_connection():
    print("\n[1/4] Test koneksi Binance Testnet...")
    trader = BinanceTrader()
    balance = trader._get_available_balance()
    print(f"  ✅ Balance USDT: {balance:.2f}")
    assert balance >= 0, "Balance error"
    return balance


def test_symbol_info():
    print("\n[2/4] Test symbol info & max leverage...")
    trader = BinanceTrader()

    test_pairs = ['BTCUSDT', 'SOLUSDT', 'DOGEUSDT']
    for symbol in test_pairs:
        info = trader._get_symbol_info(symbol)
        assert info is not None, f"Gagal ambil info {symbol}"
        print(
            f"  ✅ {symbol:12s} | max_lev={info['max_leverage']:3d}x | "
            f"qty_step={info['qty_step']} | price_tick={info['price_tick']}"
        )
    return True


def test_set_margin_leverage():
    print("\n[3/4] Test set margin type & leverage...")
    trader = BinanceTrader()

    symbol = 'BTCUSDT'
    info   = trader._get_symbol_info(symbol)
    lev    = min(20, info['max_leverage'])

    trader._set_margin_type(symbol, 'ISOLATED')
    print(f"  ✅ {symbol} margin type set to ISOLATED")

    trader._set_leverage(symbol, lev)
    print(f"  ✅ {symbol} leverage set to {lev}x")
    return True


def test_open_position():
    print("\n[4/4] Test open posisi BTCUSDT LONG (kecil)...")
    trader = BinanceTrader()

    # Ambil harga terkini
    from market_data import MarketData
    md    = MarketData()
    price = md.get_ticker_price('BTCUSDT')
    atr   = price * 0.005  # Estimasi ATR ~0.5% dari harga

    dummy_signal = SimulationResult(
        symbol='BTCUSDT',
        direction='LONG',
        confidence=0.75,
        win_probability=0.70,
        expected_return=3.5,
        risk_reward=1.67,
        entry_price=price,
        take_profit=round(price + atr * 2.5, 1),
        stop_loss=round(price - atr * 1.5, 1),
        simulations_run=5000,
        profitable_paths=3500,
        signal_score=0.72
    )

    print(f"  Signal: BTCUSDT LONG @ {price:.2f}")
    print(f"  TP: {dummy_signal.take_profit:.2f} | SL: {dummy_signal.stop_loss:.2f}")

    result = trader.execute(dummy_signal)

    if result.success:
        print(f"\n  ✅ ORDER BERHASIL!")
        print(f"     Leverage    : {result.leverage_used}x (Isolated)")
        print(f"     Quantity    : {result.quantity}")
        print(f"     Entry Price : {result.entry_price:.2f}")
        print(f"     Take Profit : {result.take_profit:.2f}")
        print(f"     Stop Loss   : {result.stop_loss:.2f}")
        print(f"     Margin Used : {result.margin_used:.2f} USDT")
        print(f"     Order ID    : {result.order_id}")
        print(f"     TP Order ID : {result.tp_order_id}")
        print(f"     SL Order ID : {result.sl_order_id}")
    else:
        print(f"\n  ❌ ORDER GAGAL: {result.error_msg}")

    return result.success


if __name__ == '__main__':
    print("=" * 55)
    print("  NERA QUANT - Trader Test (Binance Testnet)")
    print("=" * 55)

    tests = [
        ('Connection & Balance', test_connection),
        ('Symbol Info',          test_symbol_info),
        ('Margin & Leverage',    test_set_margin_leverage),
        ('Open Position',        test_open_position),
    ]

    passed = 0
    for name, fn in tests:
        try:
            r = fn()
            if r is not False and r is not None:
                passed += 1
        except Exception as e:
            print(f"  ❌ {name} FAILED: {e}")
            import traceback
            traceback.print_exc()

    print(f"\n{'='*55}")
    print(f"  Results: {passed}/{len(tests)} tests passed")
    print(f"{'='*55}")
