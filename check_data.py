
import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), 'backtest_data.db')

try:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Check 15m data
    cursor.execute("SELECT COUNT(*) FROM klines WHERE symbol='ADAUSDT' AND timeframe='15m'")
    count_15m = cursor.fetchone()[0]
    
    # Check 1h data
    cursor.execute("SELECT COUNT(*) FROM klines WHERE symbol='ADAUSDT' AND timeframe='1h'")
    count_1h = cursor.fetchone()[0]
    
    print(f"ADAUSDT 15m candle count: {count_15m}")
    print(f"ADAUSDT 1h candle count: {count_1h}")
    
    if count_15m < 200:
        print("Penyebab: Jumlah data 15m tidak cukup (kurang dari 200).")
    elif count_1h < 50:
        print("Penyebab: Jumlah data 1h tidak cukup (kurang dari 50).")
    else:
        print("Data cukup. Penyebabnya kemungkinan ada pada logika sinyal di backtester.")

except Exception as e:
    print(f"Error: {e}")
finally:
    if 'conn' in locals() and conn:
        conn.close()
