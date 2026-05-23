"""
test_system.py - Quick test semua komponen sebelum run production
Jalankan: python test_system.py
"""

import sys
import logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)


def test_market_data():
    print("\n[1/4] Testing Market Data...")
    from market_data import MarketData
    md = MarketData()

    pairs = md.get_top_pairs()
    assert len(pairs) > 0, "Gagal fetch top pairs"
    print(f"  ✅ Top pairs: {pairs[:5]}...")

    df = md.get_klines('BTCUSDT')
    assert df is not None and len(df) > 50, "Gagal fetch klines"
    print(f"  ✅ BTCUSDT klines: {len(df)} candles, last close: {df['close'].iloc[-1]:.2f}")

    price = md.get_ticker_price('BTCUSDT')
    assert price is not None and price > 0
    print(f"  ✅ BTCUSDT price: ${price:,.2f}")

    funding = md.get_funding_rate('BTCUSDT')
    print(f"  ✅ BTCUSDT funding rate: {funding:.4%}")
    return True


def test_indicators():
    print("\n[2/4] Testing Indicators...")
    from market_data import MarketData
    from indicators import TechnicalIndicators

    md = MarketData()
    ind = TechnicalIndicators()

    df = md.get_klines('ETHUSDT')
    df_ind = ind.compute_all(df)
    assert df_ind is not None, "Gagal compute indicators"
    print(f"  ✅ Indicators computed: {list(df_ind.columns)}")

    features = ind.get_signal_features(df_ind)
    assert features is not None
    print(f"  ✅ Features: RSI={features['rsi']:.1f}, EMA trend={features['ema_trend']}")
    print(f"     BB%={features['bb_pct']:.2f}, Vol ratio={features['vol_ratio']:.2f}")
    return True


def test_monte_carlo():
    print("\n[3/4] Testing Monte Carlo Engine...")
    from market_data import MarketData
    from indicators import TechnicalIndicators
    from monte_carlo import MonteCarloEngine

    md = MarketData()
    ind = TechnicalIndicators()
    mc = MonteCarloEngine(n_simulations=1000)  # Pakai 1000 untuk test cepat

    df = md.get_klines('SOLUSDT')
    df_ind = ind.compute_all(df)
    features = ind.get_signal_features(df_ind)
    funding = md.get_funding_rate('SOLUSDT')

    result = mc.run('SOLUSDT', df_ind, features, funding)
    assert result is not None, "Monte Carlo gagal"

    print(f"  ✅ SOLUSDT Monte Carlo result:")
    print(f"     Direction:    {result.direction}")
    print(f"     Confidence:   {result.confidence*100:.1f}%")
    print(f"     Win Prob:     {result.win_probability*100:.1f}%")
    print(f"     Signal Score: {result.signal_score*100:.1f}/100")
    print(f"     Entry:        {result.entry_price:.4f}")
    print(f"     TP:           {result.take_profit:.4f}")
    print(f"     SL:           {result.stop_loss:.4f}")
    print(f"     R/R:          1:{result.risk_reward:.2f}")
    return True


def test_telegram():
    print("\n[4/4] Testing Telegram Notifier...")
    from notifier import TelegramNotifier
    from monte_carlo import SimulationResult

    notifier = TelegramNotifier()

    # Test dengan dummy signal
    dummy = SimulationResult(
        symbol='BTCUSDT',
        direction='LONG',
        confidence=0.72,
        win_probability=0.68,
        expected_return=3.45,
        risk_reward=1.67,
        entry_price=67500.0,
        take_profit=69187.5,
        stop_loss=66487.5,
        simulations_run=5000,
        profitable_paths=3400,
        signal_score=0.75
    )

    ok = notifier.send_signal(dummy)
    if ok:
        print("  ✅ Telegram notifikasi terkirim! Cek chat Telegram kamu.")
    else:
        print("  ❌ Telegram gagal. Cek BOT_TOKEN dan CHAT_ID di config.py")
    return ok


if __name__ == '__main__':
    print("=" * 50)
    print("  NERA QUANT - System Test")
    print("=" * 50)

    tests = [
        ('Market Data', test_market_data),
        ('Indicators',  test_indicators),
        ('Monte Carlo', test_monte_carlo),
        ('Telegram',    test_telegram),
    ]

    passed = 0
    for name, test_fn in tests:
        try:
            result = test_fn()
            if result:
                passed += 1
        except Exception as e:
            print(f"  ❌ {name} FAILED: {e}")
            import traceback
            traceback.print_exc()

    print(f"\n{'='*50}")
    print(f"  Results: {passed}/{len(tests)} tests passed")
    print(f"{'='*50}")

    if passed == len(tests):
        print("\n✅ Semua test passed! Jalankan: python main.py")
    else:
        print("\n⚠️  Ada test yang gagal. Cek error di atas.")
        sys.exit(1)
