"""
indicators.py - Technical indicators untuk signal generation
"""

import numpy as np
import pandas as pd
import logging
from typing import Dict, Optional

logger = logging.getLogger(__name__)


class TechnicalIndicators:
    """Hitung semua technical indicators dari OHLCV data."""

    def compute_all(self, df: pd.DataFrame) -> Optional[pd.DataFrame]:
        """
        Hitung semua indicators sekaligus.
        Returns DataFrame dengan kolom indicators tambahan.
        """
        if df is None or len(df) < 50:
            return None

        try:
            df = df.copy()

            # ── Trend Indicators ──────────────────────────────────────
            df['ema_9']  = self._ema(df['close'], 9)
            df['ema_21'] = self._ema(df['close'], 21)
            df['ema_50'] = self._ema(df['close'], 50)
            df['sma_20'] = df['close'].rolling(20).mean()

            # MACD
            ema12 = self._ema(df['close'], 12)
            ema26 = self._ema(df['close'], 26)
            df['macd']        = ema12 - ema26
            df['macd_signal'] = self._ema(df['macd'], 9)
            df['macd_hist']   = df['macd'] - df['macd_signal']

            # ── Momentum Indicators ───────────────────────────────────
            df['rsi'] = self._rsi(df['close'], 14)

            # Stochastic RSI
            df['stoch_k'], df['stoch_d'] = self._stoch_rsi(df['close'])

            # ── Volatility Indicators ─────────────────────────────────
            # Bollinger Bands
            df['bb_mid']   = df['close'].rolling(20).mean()
            bb_std         = df['close'].rolling(20).std()
            df['bb_upper'] = df['bb_mid'] + (2 * bb_std)
            df['bb_lower'] = df['bb_mid'] - (2 * bb_std)
            df['bb_width'] = (df['bb_upper'] - df['bb_lower']) / df['bb_mid']
            df['bb_pct']   = (df['close'] - df['bb_lower']) / (df['bb_upper'] - df['bb_lower'])

            # ATR
            df['atr'] = self._atr(df, 14)
            df['atr_pct'] = df['atr'] / df['close']  # ATR sebagai % harga

            # ── Volume Indicators ─────────────────────────────────────
            df['vol_sma'] = df['volume'].rolling(20).mean()
            df['vol_ratio'] = df['volume'] / df['vol_sma']  # Volume spike ratio

            # OBV (On Balance Volume)
            df['obv'] = self._obv(df)

            # ── Price Action ──────────────────────────────────────────
            df['returns']    = df['close'].pct_change()
            df['volatility'] = df['returns'].rolling(20).std() * np.sqrt(252 * 96)  # Annualized

            # Candle body & wick analysis
            df['body']       = abs(df['close'] - df['open'])
            df['upper_wick'] = df['high'] - df[['close', 'open']].max(axis=1)
            df['lower_wick'] = df[['close', 'open']].min(axis=1) - df['low']
            df['is_bullish'] = (df['close'] > df['open']).astype(int)

            # SMC Concepts
            from config import SMC_MODE, SMC_SWING_WINDOW
            if SMC_MODE:
                df = self._detect_smc(df, swing_window=SMC_SWING_WINDOW)

            return df.dropna()

        except Exception as e:
            logger.error(f"Error computing indicators: {e}")
            return None

    def _detect_smc(self, df: pd.DataFrame, swing_window: int = 5) -> pd.DataFrame:
        """
        Detect Smart Money Concepts (SMC): Swing Highs/Lows, BOS, CHoCH, Order Blocks, and FVGs.
        """
        df = df.copy()
        n = len(df)
        
        # Initialize columns
        df['swing_high'] = 0.0
        df['swing_low'] = 0.0
        df['bos'] = 0          # 1 = Bullish BOS, -1 = Bearish BOS, 0 = None
        df['choch'] = 0        # 1 = Bullish CHoCH, -1 = Bearish CHoCH, 0 = None
        
        # Order Blocks: bull_ob_top, bull_ob_bot, bear_ob_top, bear_ob_bot
        df['bull_ob_top'] = 0.0
        df['bull_ob_bot'] = 0.0
        df['bear_ob_top'] = 0.0
        df['bear_ob_bot'] = 0.0
        
        # FVGs: fvg_top, fvg_bot, fvg_dir
        df['fvg_top'] = 0.0
        df['fvg_bot'] = 0.0
        df['fvg_dir'] = 0      # 1 = Bullish FVG, -1 = Bearish FVG, 0 = None

        # 1. Deteksi Swing High / Low (Local peaks/troughs)
        highs = df['high'].values
        lows = df['low'].values
        closes = df['close'].values
        opens = df['open'].values
        
        current_trend = 0  # 1 = Bullish, -1 = Bearish
        
        # Track active order blocks & FVGs
        active_bull_obs = []
        active_bear_obs = []
        active_fvgs = []

        for i in range(swing_window, n - swing_window):
            is_sh = True
            is_sl = True
            
            # Check swing window
            for w in range(1, swing_window + 1):
                if highs[i] < highs[i-w] or highs[i] < highs[i+w]:
                    is_sh = False
                if lows[i] > lows[i-w] or lows[i] > lows[i+w]:
                    is_sl = False
                    
            if is_sh:
                df.at[df.index[i], 'swing_high'] = highs[i]
            if is_sl:
                df.at[df.index[i], 'swing_low'] = lows[i]

            # 2. Deteksi BOS / CHoCH & Pembuatan Order Blocks
            prev_highs = df['swing_high'].iloc[:i].replace(0.0, np.nan).dropna()
            prev_lows = df['swing_low'].iloc[:i].replace(0.0, np.nan).dropna()
            
            if len(prev_highs) > 0 and closes[i] > prev_highs.iloc[-1]:
                # Break of last swing high
                if current_trend == -1:
                    df.at[df.index[i], 'choch'] = 1
                    current_trend = 1
                else:
                    df.at[df.index[i], 'bos'] = 1
                    current_trend = 1
                
                # Identifikasi Bullish Order Block (bearish candle terakhir sebelum pergerakan impulsif naik)
                for k in range(i, max(0, i - 15), -1):
                    if closes[k] < opens[k]:
                        active_bull_obs.append({
                            'top': highs[k],
                            'bot': lows[k],
                            'index': k,
                            'mitigated': False
                        })
                        break
                        
            elif len(prev_lows) > 0 and closes[i] < prev_lows.iloc[-1]:
                # Break of last swing low
                if current_trend == 1:
                    df.at[df.index[i], 'choch'] = -1
                    current_trend = -1
                else:
                    df.at[df.index[i], 'bos'] = -1
                    current_trend = -1
                
                # Identifikasi Bearish Order Block (bullish candle terakhir sebelum pergerakan impulsif turun)
                for k in range(i, max(0, i - 15), -1):
                    if closes[k] > opens[k]:
                        active_bear_obs.append({
                            'top': highs[k],
                            'bot': lows[k],
                            'index': k,
                            'mitigated': False
                        })
                        break

            # 3. Deteksi Fair Value Gaps (FVG)
            if i >= 2 and lows[i] > highs[i-2] and closes[i-1] > opens[i-1]:
                active_fvgs.append({
                    'top': lows[i],
                    'bot': highs[i-2],
                    'dir': 1,
                    'index': i-1,
                    'mitigated': False
                })
            elif i >= 2 and highs[i] < lows[i-2] and closes[i-1] < opens[i-1]:
                active_fvgs.append({
                    'top': lows[i-2],
                    'bot': highs[i],
                    'dir': -1,
                    'index': i-1,
                    'mitigated': False
                })

            # 4. Mitigasi (Update status OB & FVG)
            for ob in active_bull_obs:
                if not ob['mitigated']:
                    if closes[i] < ob['bot']:
                        ob['mitigated'] = True
            
            for ob in active_bear_obs:
                if not ob['mitigated']:
                    if closes[i] > ob['top']:
                        ob['mitigated'] = True

            for fvg in active_fvgs:
                if not fvg['mitigated']:
                    if fvg['dir'] == 1 and lows[i] <= fvg['bot']:
                        fvg['mitigated'] = True
                    elif fvg['dir'] == -1 and highs[i] >= fvg['top']:
                        fvg['mitigated'] = True

        # Ambil OB & FVG terupdate/aktif untuk candle terakhir
        unmitigated_bull_obs = [ob for ob in active_bull_obs if not ob['mitigated']]
        unmitigated_bear_obs = [ob for ob in active_bear_obs if not ob['mitigated']]
        unmitigated_fvgs = [fvg for fvg in active_fvgs if not fvg['mitigated']]
        
        if unmitigated_bull_obs:
            latest_bull = unmitigated_bull_obs[-1]
            df.at[df.index[-1], 'bull_ob_top'] = latest_bull['top']
            df.at[df.index[-1], 'bull_ob_bot'] = latest_bull['bot']
            
        if unmitigated_bear_obs:
            latest_bear = unmitigated_bear_obs[-1]
            df.at[df.index[-1], 'bear_ob_top'] = latest_bear['top']
            df.at[df.index[-1], 'bear_ob_bot'] = latest_bear['bot']

        if unmitigated_fvgs:
            latest_fvg = unmitigated_fvgs[-1]
            df.at[df.index[-1], 'fvg_top'] = latest_fvg['top']
            df.at[df.index[-1], 'fvg_bot'] = latest_fvg['bot']
            df.at[df.index[-1], 'fvg_dir'] = latest_fvg['dir']

        return df


    def get_signal_features(self, df: pd.DataFrame) -> Optional[Dict]:
        """
        Extract fitur dari candle terakhir untuk signal scoring.
        Returns dict dengan semua nilai indicator terkini.
        """
        if df is None or len(df) < 2:
            return None

        from config import RSI_OVERSOLD, RSI_OVERBOUGHT

        last = df.iloc[-1]
        prev = df.iloc[-2]

        # Deteksi divergence
        rsi_div = self._detect_divergence(df, 'rsi')
        macd_div = self._detect_divergence(df, 'macd')

        # Recent BOS and CHoCH detection in last 5 candles
        recent_bos = 0
        recent_choch = 0
        for offset in range(1, min(6, len(df) + 1)):
            val = df.iloc[-offset]
            if 'bos' in val and val['bos'] != 0:
                recent_bos = int(val['bos'])
                break
        for offset in range(1, min(6, len(df) + 1)):
            val = df.iloc[-offset]
            if 'choch' in val and val['choch'] != 0:
                recent_choch = int(val['choch'])
                break

        features = {
            # Price
            'price':        last['close'],
            'price_change': last['returns'],

            # Trend
            'ema_trend':    1 if last['ema_9'] > last['ema_21'] > last['ema_50'] else
                           -1 if last['ema_9'] < last['ema_21'] < last['ema_50'] else 0,
            'above_ema50':  1 if last['close'] > last['ema_50'] else -1,

            # MACD
            'macd_cross':   1 if (last['macd'] > last['macd_signal'] and
                                  prev['macd'] <= prev['macd_signal']) else
                           -1 if (last['macd'] < last['macd_signal'] and
                                  prev['macd'] >= prev['macd_signal']) else 0,
            'macd_hist':    last['macd_hist'],
            'macd_positive': 1 if last['macd'] > 0 else -1,
            'macd_div':      macd_div,

            # RSI
            'rsi':          last['rsi'],
            'rsi_signal':   1 if last['rsi'] < RSI_OVERSOLD else -1 if last['rsi'] > RSI_OVERBOUGHT else 0,
            'rsi_div':      rsi_div,

            # Stochastic
            'stoch_k':      last['stoch_k'],
            'stoch_d':      last['stoch_d'],
            'stoch_cross':  1 if (last['stoch_k'] > last['stoch_d'] and
                                  prev['stoch_k'] <= prev['stoch_d']) else
                           -1 if (last['stoch_k'] < last['stoch_d'] and
                                  prev['stoch_k'] >= prev['stoch_d']) else 0,

            # Bollinger Bands
            'bb_pct':       last['bb_pct'],
            'bb_signal':    1 if last['bb_pct'] < 0.2 else -1 if last['bb_pct'] > 0.8 else 0,
            'bb_width':     last['bb_width'],

            # Volatility
            'atr_pct':      last['atr_pct'],
            'volatility':   last['volatility'],

            # Volume
            'vol_ratio':    last['vol_ratio'],
            'vol_spike':    1 if last['vol_ratio'] > 2.0 else 0,

            # Candle
            'is_bullish':   last['is_bullish'],
            
            # SMC features
            'swing_high':   last.get('swing_high', 0.0),
            'swing_low':    last.get('swing_low', 0.0),
            'bos':          recent_bos,
            'choch':        recent_choch,
            'bull_ob_top':  last.get('bull_ob_top', 0.0),
            'bull_ob_bot':  last.get('bull_ob_bot', 0.0),
            'bear_ob_top':  last.get('bear_ob_top', 0.0),
            'bear_ob_bot':  last.get('bear_ob_bot', 0.0),
            'fvg_top':      last.get('fvg_top', 0.0),
            'fvg_bot':      last.get('fvg_bot', 0.0),
            'fvg_dir':      last.get('fvg_dir', 0),
        }

        return features


    def _detect_divergence(self, df: pd.DataFrame, indicator_col: str, lookback: int = 30) -> int:
        """
        Deteksi divergence antara close price dan indicator_col (RSI/MACD)
        Returns:
            1 if Bullish Divergence (Price Lower Low, Indicator Higher Low)
            -1 if Bearish Divergence (Price Higher High, Indicator Lower High)
            0 if No Divergence
        """
        if len(df) < lookback:
            return 0

        # Kita ambil lookback window terakhir
        window = df.iloc[-lookback:]

        # Swing Low terjadi jika low_i < low_{i-1} and low_i < low_{i+1}
        # Swing High terjadi jika high_i > high_{i-1} and high_i > high_{i+1}
        
        # Bullish Divergence
        lows = []
        for i in range(1, lookback - 1):
            if window['low'].iloc[i] < window['low'].iloc[i-1] and window['low'].iloc[i] < window['low'].iloc[i+1]:
                lows.append((window['low'].iloc[i], window[indicator_col].iloc[i]))
        
        if len(lows) >= 2:
            last_low = lows[-1]
            prev_low = lows[-2]
            # Price Lower Low, Indicator Higher Low
            if last_low[0] < prev_low[0] and last_low[1] > prev_low[1]:
                if df['close'].iloc[-1] > last_low[0]:
                    return 1

        # Bearish Divergence
        highs = []
        for i in range(1, lookback - 1):
            if window['high'].iloc[i] > window['high'].iloc[i-1] and window['high'].iloc[i] > window['high'].iloc[i+1]:
                highs.append((window['high'].iloc[i], window[indicator_col].iloc[i]))
                
        if len(highs) >= 2:
            last_high = highs[-1]
            prev_high = highs[-2]
            # Price Higher High, Indicator Lower High
            if last_high[0] > prev_high[0] and last_high[1] < prev_high[1]:
                if df['close'].iloc[-1] < last_high[0]:
                    return -1
                    
        return 0

    # ── Private helper methods ────────────────────────────────────────


    def _ema(self, series: pd.Series, period: int) -> pd.Series:
        return series.ewm(span=period, adjust=False).mean()

    def _rsi(self, series: pd.Series, period: int = 14) -> pd.Series:
        delta = series.diff()
        gain  = delta.clip(lower=0).rolling(period).mean()
        loss  = (-delta.clip(upper=0)).rolling(period).mean()
        rs    = gain / loss.replace(0, np.nan)
        return 100 - (100 / (1 + rs))

    def _stoch_rsi(self, series: pd.Series, period: int = 14) -> tuple:
        rsi    = self._rsi(series, period)
        rsi_min = rsi.rolling(period).min()
        rsi_max = rsi.rolling(period).max()
        stoch_k = 100 * (rsi - rsi_min) / (rsi_max - rsi_min).replace(0, np.nan)
        stoch_d = stoch_k.rolling(3).mean()
        return stoch_k, stoch_d

    def _atr(self, df: pd.DataFrame, period: int = 14) -> pd.Series:
        high_low   = df['high'] - df['low']
        high_close = (df['high'] - df['close'].shift()).abs()
        low_close  = (df['low']  - df['close'].shift()).abs()
        tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        return tr.ewm(span=period, adjust=False).mean()

    def _obv(self, df: pd.DataFrame) -> pd.Series:
        direction = np.sign(df['close'].diff()).fillna(0)
        return (direction * df['volume']).cumsum()
