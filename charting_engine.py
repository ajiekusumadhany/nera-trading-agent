import pandas as pd
import mplfinance as mpf
import os
import logging
from market_data import MarketData

logger = logging.getLogger(__name__)

def generate_chart(symbol: str, timeframe: str, entry_price: float, tp: float, sl: float, ob_top: float=None, ob_bot: float=None) -> str:
    """
    Generate candlestick chart with annotations for entry, TP, SL, and OB.
    Returns path to the saved image.
    """
    try:
        md = MarketData()
        df = md.get_klines(symbol, interval=timeframe, limit=100)
        if df is None or df.empty:
            return ""
            
        apdicts = []
        apdicts.append(mpf.make_addplot([entry_price]*len(df), color='blue', linestyle='--', label='Entry'))
        apdicts.append(mpf.make_addplot([tp]*len(df), color='green', linestyle='-', label='TP'))
        apdicts.append(mpf.make_addplot([sl]*len(df), color='red', linestyle='-', label='SL'))
        
        filename = f"/tmp/{symbol}_{timeframe}_chart.png"
        
        fill_between = None
        if ob_top and ob_bot:
            fill_between = dict(y1=ob_bot, y2=ob_top, color='purple', alpha=0.2)
            
        mpf.plot(df, type='candle', style='charles', addplot=apdicts, 
                 volume=True, savefig=filename,
                 title=f"{symbol} - {timeframe} Setup",
                 fill_between=fill_between)
                 
        return filename
    except Exception as e:
        logger.error(f"Error generating chart for {symbol}: {e}")
        return ""
