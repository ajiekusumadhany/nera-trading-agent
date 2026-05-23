"""
market_data.py - Fetch top 50 pairs & OHLCV data dari Binance
"""

import json
import os
import requests
import pandas as pd
import numpy as np
import logging
from typing import List, Dict, Optional, Set
from config import (
    API_KEY, BINANCE_BASE_URL, TOP_PAIRS_COUNT,
    MIN_VOLUME_USDT, TIMEFRAME, MC_LOOKBACK_CANDLES
)

logger = logging.getLogger(__name__)

# File untuk persist blacklist antar restart
_BLACKLIST_FILE = os.path.join(os.path.dirname(__file__), 'blacklist.json')


class MarketData:
    """Handles semua data fetching dari Binance Futures."""

    BASE_URL = 'https://fapi.binance.com'  # Public endpoint (no testnet needed for market data)

    def __init__(self):
        # Cache status symbol dari exchange info (refresh tiap 10 menit)
        self._tradeable_cache: Set[str] = set()
        self._cache_ts: float = 0
        self._CACHE_TTL = 600  # 10 menit

        # Blacklist permanen (survive restart) — pair closed/delisted
        self._blacklist: Set[str] = self._load_blacklist()

    # ── Blacklist persistence ─────────────────────────────────────────

    def _load_blacklist(self) -> Set[str]:
        try:
            if os.path.exists(_BLACKLIST_FILE):
                with open(_BLACKLIST_FILE) as f:
                    data = json.load(f)
                bl = set(data.get('blacklist', []))
                if bl:
                    logger.info(f"[MarketData] Loaded blacklist: {bl}")
                return bl
        except Exception:
            pass
        return set()

    def _save_blacklist(self):
        try:
            with open(_BLACKLIST_FILE, 'w') as f:
                json.dump({'blacklist': list(self._blacklist)}, f)
        except Exception as e:
            logger.warning(f"[MarketData] Gagal save blacklist: {e}")

    def add_to_blacklist(self, symbol: str):
        """Tambah symbol ke blacklist permanen."""
        if symbol not in self._blacklist:
            self._blacklist.add(symbol)
            self._save_blacklist()
            logger.warning(f"[MarketData] 🚫 {symbol} ditambah ke blacklist permanen")

    def get_blacklist(self) -> Set[str]:
        return self._blacklist.copy()

    # ── Tradeable symbols dari exchange info ──────────────────────────

    def _refresh_tradeable_cache(self):
        """
        Fetch exchange info dari Binance Testnet dan cache symbol
        yang statusnya TRADING. Di-refresh setiap 10 menit.
        """
        import time
        if time.time() - self._cache_ts < self._CACHE_TTL and self._tradeable_cache:
            return

        try:
            # Gunakan testnet exchange info untuk validasi trading status
            url = f"{BINANCE_BASE_URL}/fapi/v1/exchangeInfo"
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            data = resp.json()

            tradeable = set()
            for s in data.get('symbols', []):
                if (s.get('status') == 'TRADING'
                        and s.get('symbol', '').endswith('USDT')
                        and s.get('contractType') == 'PERPETUAL'):
                    tradeable.add(s['symbol'])

            self._tradeable_cache = tradeable
            self._cache_ts = time.time()
            logger.debug(f"[MarketData] Exchange info refreshed: {len(tradeable)} tradeable pairs")

        except Exception as e:
            logger.warning(f"[MarketData] Gagal refresh exchange info: {e}")

    def is_tradeable(self, symbol: str) -> bool:
        """Cek apakah symbol bisa di-trade (status TRADING dan tidak di blacklist)."""
        if symbol in self._blacklist:
            return False
        self._refresh_tradeable_cache()
        if self._tradeable_cache:
            return symbol in self._tradeable_cache
        return True  # fallback: anggap tradeable kalau cache kosong

    def get_top_pairs(self) -> List[str]:
        """
        Ambil top N USDT perpetual futures pairs berdasarkan 24h volume.
        Hanya return pair yang statusnya TRADING di exchange dan tidak di blacklist.
        """
        try:
            # Refresh cache exchange info dulu
            self._refresh_tradeable_cache()

            url = f"{self.BASE_URL}/fapi/v1/ticker/24hr"
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            tickers = resp.json()

            # Filter: USDT pairs, volume cukup, tidak ada underscore,
            # statusnya TRADING di testnet, dan tidak di blacklist
            usdt_pairs = [
                t for t in tickers
                if t['symbol'].endswith('USDT')
                and float(t['quoteVolume']) >= MIN_VOLUME_USDT
                and '_' not in t['symbol']
                and self.is_tradeable(t['symbol'])
            ]

            # Sort by 24h quote volume descending
            usdt_pairs.sort(key=lambda x: float(x['quoteVolume']), reverse=True)

            symbols = [t['symbol'] for t in usdt_pairs[:TOP_PAIRS_COUNT]]
            logger.info(f"Top {len(symbols)} pairs fetched. #1: {symbols[0] if symbols else 'N/A'}")
            return symbols

        except Exception as e:
            logger.error(f"Error fetching top pairs: {e}")
            return [
                'BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'SOLUSDT', 'XRPUSDT',
                'ADAUSDT', 'DOGEUSDT', 'AVAXUSDT', 'DOTUSDT', 'LINKUSDT'
            ]

    def get_klines(
        self,
        symbol: str,
        interval: str = TIMEFRAME,
        limit: int = MC_LOOKBACK_CANDLES + 50
    ) -> Optional[pd.DataFrame]:
        """
        Fetch OHLCV candlestick data untuk satu symbol.
        Returns DataFrame dengan kolom: open, high, low, close, volume
        """
        try:
            url = f"{self.BASE_URL}/fapi/v1/klines"
            params = {
                'symbol': symbol,
                'interval': interval,
                'limit': limit
            }
            resp = requests.get(url, params=params, timeout=10)
            resp.raise_for_status()
            raw = resp.json()

            df = pd.DataFrame(raw, columns=[
                'timestamp', 'open', 'high', 'low', 'close', 'volume',
                'close_time', 'quote_volume', 'trades', 'taker_buy_base',
                'taker_buy_quote', 'ignore'
            ])

            # Convert types
            for col in ['open', 'high', 'low', 'close', 'volume', 'quote_volume']:
                df[col] = pd.to_numeric(df[col])

            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            df.set_index('timestamp', inplace=True)

            return df[['open', 'high', 'low', 'close', 'volume', 'quote_volume']]

        except Exception as e:
            logger.error(f"Error fetching klines for {symbol}: {e}")
            return None

    def get_ticker_price(self, symbol: str) -> Optional[float]:
        """Ambil harga terkini untuk satu symbol."""
        try:
            url = f"{self.BASE_URL}/fapi/v1/ticker/price"
            resp = requests.get(url, params={'symbol': symbol}, timeout=5)
            resp.raise_for_status()
            return float(resp.json()['price'])
        except Exception as e:
            logger.error(f"Error fetching price for {symbol}: {e}")
            return None

    def get_funding_rate(self, symbol: str) -> Optional[float]:
        """Ambil current funding rate."""
        try:
            url = f"{self.BASE_URL}/fapi/v1/premiumIndex"
            resp = requests.get(url, params={'symbol': symbol}, timeout=5)
            resp.raise_for_status()
            data = resp.json()
            return float(data.get('lastFundingRate', 0))
        except Exception as e:
            logger.error(f"Error fetching funding rate for {symbol}: {e}")
            return 0.0

    def get_open_interest(self, symbol: str) -> Optional[float]:
        """Ambil open interest untuk sentiment analysis."""
        try:
            url = f"{self.BASE_URL}/fapi/v1/openInterest"
            resp = requests.get(url, params={'symbol': symbol}, timeout=5)
            resp.raise_for_status()
            return float(resp.json().get('openInterest', 0))
        except Exception as e:
            logger.error(f"Error fetching OI for {symbol}: {e}")
            return 0.0

    def get_oi_change(self, symbol: str, period: str = '15m') -> float:
        """
        Hitung perubahan Open Interest (%) dalam period terakhir menggunakan /futures/data/openInterestHist.
        """
        try:
            url = f"{self.BASE_URL}/futures/data/openInterestHist"

            params = {
                'symbol': symbol,
                'period': period,
                'limit': 5
            }
            resp = requests.get(url, params=params, timeout=5)
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, list) and len(data) >= 2:
                # data diurutkan dari timestamp lama ke baru, data terakhir adalah yang terbaru
                oi_latest = float(data[-1].get('sumOpenInterest', 0))
                oi_prev = float(data[-2].get('sumOpenInterest', 0))
                if oi_prev > 0:
                    return (oi_latest - oi_prev) / oi_prev
            return 0.0
        except Exception as e:
            logger.error(f"Error fetching OI change for {symbol}: {e}")
            return 0.0

