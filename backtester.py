import os
import sqlite3
import logging
import pandas as pd
from datetime import datetime
from config import (
    MC_CONFIDENCE_THRESHOLD, SMC_MC_CONFIDENCE_THRESHOLD, SMC_MODE
)
from indicators import TechnicalIndicators
from monte_carlo import MonteCarloEngine, SimulationResult
from market_context import get_session

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("Backtester")

DB_PATH = os.path.join(os.path.dirname(__file__), 'backtest_data.db')
PAIR_STATS_DB = os.path.join(os.path.dirname(__file__), 'pair_statistics.db')

def init_stats_db():
    conn = sqlite3.connect(PAIR_STATS_DB)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS pair_stats (
            symbol TEXT PRIMARY KEY,
            total_trades INTEGER,
            wins INTEGER,
            losses INTEGER,
            win_rate REAL,
            profit_factor REAL,
            last_updated TEXT
        )
    ''')
    conn.commit()
    return conn

def load_data(symbol: str, timeframe: str, start_ts: int = None) -> pd.DataFrame:
    conn = sqlite3.connect(DB_PATH)
    params = ()
    query = f"SELECT timestamp, open, high, low, close, volume, quote_volume FROM klines WHERE symbol='{symbol}' AND timeframe='{timeframe}'"
    if start_ts:
        query += " AND timestamp >= ?"
        params = (start_ts,)
    query += " ORDER BY timestamp ASC"
    
    df = pd.read_sql_query(query, conn, params=params)
    conn.close()
    if df.empty:
        return df
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    df.set_index('timestamp', inplace=True)
    return df

def simulate_trade(entry_price, sl, tp, direction, future_df):
    """Scan forward to see if SL or TP is hit first."""
    for idx, row in future_df.iterrows():
        high = row['high']
        low = row['low']
        if direction == 'LONG':
            if low <= sl:
                return 'LOSS'
            if high >= tp:
                return 'WIN'
        else:
            if high >= sl:
                return 'LOSS'
            if low <= tp:
                return 'WIN'
    return 'OPEN' # didn't finish within available data

def run_backtest_for_symbol(symbol: str, indicator: TechnicalIndicators, mc_engine: MonteCarloEngine, start_ts: int):
    logger.info(f"Starting backtest for {symbol}")
    
    df_15m = load_data(symbol, '15m', start_ts)
    if df_15m.empty or len(df_15m) < 200:
        logger.warning(f"Not enough 15m data for {symbol}")
        return None
        
    df_1h = load_data(symbol, '1h', start_ts)
    if df_1h.empty or len(df_1h) < 50:
        logger.warning(f"Not enough 1h data for {symbol}")
        return None
        
    # Precompute indicators on entire dataset
    df_15m = indicator.compute_all(df_15m)
    df_1h = indicator.compute_all(df_1h)
    
    target_conf = SMC_MC_CONFIDENCE_THRESHOLD if SMC_MODE else MC_CONFIDENCE_THRESHOLD
    
    wins = 0
    losses = 0
    total_trades = 0
    
        # We iterate starting from bar 200 to give indicators warmup
    for i in range(200, len(df_15m) - 1):
        if i % 500 == 0: # Log progress every 500 candles
            logger.info(f"  ... processing {symbol} candle {i}/{len(df_15m)}")

        # We need to simulate the features extraction. 
        # Since get_signal_features requires a dataframe ending at `i`, we pass a slice
        slice_15m = df_15m.iloc[max(0, i-60):i+1]  # Give it >= 60 bars for MC engine (needs >= 30)
        features = indicator.get_signal_features(slice_15m)
        if not features:
            continue
            
        # Mock HTF features
        timestamp = df_15m.index[i]
        # Find corresponding 1h bar (closest before or equal to timestamp)
        idx_1h = df_1h.index.get_indexer([timestamp], method='pad')[0]
        if idx_1h >= 0:
            slice_1h = df_1h.iloc[max(0, idx_1h-60):idx_1h+1]
            htf_features = indicator.get_signal_features(slice_1h)
        else:
            htf_features = None
            
        # Skip Funding/OI to save speed, they are secondary in MC
        funding_rate = 0.0
        oi_change = 0.0
            
        # We pass the slice to MC Engine
        res = mc_engine.run(
            symbol=symbol,
            df=slice_15m,
            features=features,
            timeframe='15m',
            funding_rate=funding_rate,
            htf_features=htf_features,
            oi_change=oi_change
        )
        
        if res and res.confidence >= target_conf and res.signal_score >= 0.6:
            # Trade entry!
            future_df = df_15m.iloc[i+1:i+100] # Scan next 100 bars (25 hours) for outcome
            outcome = simulate_trade(res.entry_price, res.stop_loss, res.take_profit, res.direction, future_df)
            if outcome == 'WIN':
                wins += 1
                total_trades += 1
            elif outcome == 'LOSS':
                losses += 1
                total_trades += 1

    win_rate = wins / total_trades if total_trades > 0 else 0.0
    return {
        'symbol': symbol,
        'total_trades': total_trades,
        'wins': wins,
        'losses': losses,
        'win_rate': win_rate,
        'profit_factor': 0.0 # simplified
    }

from datetime import datetime, timedelta

def run_all():
    indicator = TechnicalIndicators()
    mc_engine = MonteCarloEngine()
    stats_conn = init_stats_db()
    
    # Get pairs from the downloaded database
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT symbol FROM klines")
    pairs = [r[0] for r in cursor.fetchall()]
    conn.close()
    
    logger.info(f"Found {len(pairs)} pairs in local database to backtest.")

    # Calculate start timestamp (3 months ago)
    three_months_ago = datetime.now() - timedelta(days=90)
    start_ts = int(three_months_ago.timestamp() * 1000)
    logger.info(f"Running backtest for the last 3 months (since {three_months_ago.strftime('%Y-%m-%d')}).")
    
    for symbol in pairs:
        try:
            stats = run_backtest_for_symbol(symbol, indicator, mc_engine, start_ts)
            if stats:
                now = datetime.utcnow().isoformat()
                stats_conn.execute('''
                    INSERT OR REPLACE INTO pair_stats (symbol, total_trades, wins, losses, win_rate, profit_factor, last_updated)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', (stats['symbol'], stats['total_trades'], stats['wins'], stats['losses'], stats['win_rate'], stats['profit_factor'], now))
                stats_conn.commit()
                logger.info(f"[{symbol}] WR: {stats['win_rate']*100:.2f}% | Trades: {stats['total_trades']}")
        except Exception as e:
            logger.error(f"Error backtesting {symbol}: {e}")
            
    stats_conn.close()
    logger.info("Backtesting completed.")

if __name__ == '__main__':
    run_all()
