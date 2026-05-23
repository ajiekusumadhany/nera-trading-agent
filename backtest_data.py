import os
import time
import logging
import sqlite3
import requests
import pandas as pd
from typing import List, Optional
from datetime import datetime, timedelta

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("BacktestData")

DB_PATH = os.path.join(os.path.dirname(__file__), 'backtest_data.db')
BASE_URL = 'https://fapi.binance.com'

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS klines (
            symbol TEXT,
            timeframe TEXT,
            timestamp INTEGER,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            volume REAL,
            quote_volume REAL,
            PRIMARY KEY (symbol, timeframe, timestamp)
        )
    ''')
    cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_symbol_timeframe_timestamp
        ON klines(symbol, timeframe, timestamp)
    ''')
    conn.commit()
    return conn

def get_active_usdt_pairs() -> List[str]:
    """Fetch all active USDT perpetual pairs."""
    try:
        url = f"{BASE_URL}/fapi/v1/exchangeInfo"
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        tradeable = []
        for s in data.get('symbols', []):
            if (s.get('status') == 'TRADING'
                    and s.get('symbol', '').endswith('USDT')
                    and s.get('contractType') == 'PERPETUAL'
                    and '_' not in s.get('symbol', '')):
                tradeable.append(s['symbol'])
        return tradeable
    except Exception as e:
        logger.error(f"Error fetching active pairs: {e}")
        return []

def fetch_klines_chunk(symbol: str, interval: str, start_time: int, end_time: int) -> list:
    """Fetch a chunk of klines from Binance."""
    url = f"{BASE_URL}/fapi/v1/klines"
    params = {
        'symbol': symbol,
        'interval': interval,
        'startTime': start_time,
        'endTime': end_time,
        'limit': 1500
    }
    try:
        resp = requests.get(url, params=params, timeout=10)
        if resp.status_code == 429:
            logger.warning("Rate limit hit! Sleeping for 30 seconds...")
            time.sleep(30)
            return fetch_klines_chunk(symbol, interval, start_time, end_time)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.error(f"Error fetching {symbol} {interval}: {e}")
        time.sleep(2) # Backoff
        return []

def download_historical_data(symbol: str, interval: str, days: int = 730, conn: sqlite3.Connection = None):
    """Download historical data for a specific symbol and interval."""
    if not conn:
        conn = init_db()
    
    cursor = conn.cursor()
    # Check latest timestamp in DB to resume
    cursor.execute('SELECT MAX(timestamp) FROM klines WHERE symbol=? AND timeframe=?', (symbol, interval))
    res = cursor.fetchone()[0]
    
    end_time_ms = int(time.time() * 1000)
    
    if res:
        # Resume from the latest stored timestamp + 1 interval
        start_time_ms = res + 1
        logger.info(f"Resuming {symbol} {interval} from {datetime.fromtimestamp(start_time_ms/1000)}")
    else:
        # Start from `days` ago
        start_time_ms = int((datetime.now() - timedelta(days=days)).timestamp() * 1000)
        logger.info(f"Starting {symbol} {interval} from {days} days ago")

    if start_time_ms >= end_time_ms:
        logger.info(f"{symbol} {interval} is already up to date.")
        return

    current_start = start_time_ms
    total_inserted = 0
    
    while current_start < end_time_ms:
        chunk = fetch_klines_chunk(symbol, interval, current_start, end_time_ms)
        if not chunk:
            break
            
        records = []
        last_ts = current_start
        for k in chunk:
            ts = int(k[0])
            records.append((
                symbol, interval, ts,
                float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5]), float(k[7])
            ))
            last_ts = ts
        
        cursor.executemany('''
            INSERT OR IGNORE INTO klines (symbol, timeframe, timestamp, open, high, low, close, volume, quote_volume)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', records)
        conn.commit()
        
        total_inserted += len(records)
        logger.info(f"[{symbol} {interval}] Downloaded {len(records)} candles (up to {datetime.fromtimestamp(last_ts/1000)}). Total: {total_inserted}")
        
        # Advance current_start to the next timestamp
        if len(chunk) < 1500:
            # Reached the end
            break
        current_start = last_ts + 1
        time.sleep(0.1) # Small delay to respect rate limits (weight is usually 5-10 per request)

def run_mass_download():
    """Main function to orchestrate the mass download."""
    logger.info("Starting mass download of historical data...")
    conn = init_db()
    pairs = get_active_usdt_pairs()
    logger.info(f"Found {len(pairs)} active USDT pairs.")
    
    # Define timeframes we need: main (15m) and HTF (1h, 4h)
    timeframes = ['15m', '1h', '4h']
    
    for symbol in pairs:
        for tf in timeframes:
            try:
                download_historical_data(symbol, tf, days=90, conn=conn)
            except Exception as e:
                logger.error(f"Failed completely on {symbol} {tf}: {e}")
                
    logger.info("Mass download complete!")
    conn.close()

if __name__ == "__main__":
    run_mass_download()
