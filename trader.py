"""
trader.py - Eksekusi order ke Binance Testnet Futures
Handles: set leverage, isolated margin, open posisi, pasang TP/SL
"""

import hashlib
import hmac
import time
import math
import logging
import requests
from urllib.parse import urlencode
from typing import Optional, Dict, Tuple
from dataclasses import dataclass

from config import (
    API_KEY, API_SECRET,
    BINANCE_BASE_URL,
    LEVERAGE, MARGIN_TYPE,
    RISK_PER_TRADE,
    MIN_NOTIONAL_USDT,
    PARTIAL_TP_ATR_MULTIPLIER,
    ENABLE_PARTIAL_TP,
    SPREAD_PROTECTION_ENABLED,
    MAX_SPREAD_PCT,
)
from monte_carlo import SimulationResult

logger = logging.getLogger(__name__)


@dataclass
class TradeResult:
    """Hasil eksekusi satu trade."""
    symbol:          str
    direction:       str
    success:         bool
    leverage_used:   int
    quantity:        float
    entry_price:     float
    take_profit:     float
    stop_loss:       float
    order_id:        Optional[int]
    tp_order_id:     Optional[int]
    sl_order_id:     Optional[int]
    margin_used:     float        # USDT
    error_msg:       Optional[str] = None
    tp1_order_id:    Optional[int] = None
    tp2_order_id:    Optional[int] = None
    is_partial:      bool = False
    tp1_price:       float = 0.0


class BinanceTrader:
    """
    Eksekusi order ke Binance Futures Testnet.

    Flow per sinyal:
    1. Cek posisi terbuka (jangan double entry)
    2. Ambil exchange info → max leverage & tick/step size
    3. Set margin type ke ISOLATED
    4. Set leverage (min dari target vs max pair)
    5. Hitung quantity dari balance × risk
    6. Open market order
    7. Pasang TP (TAKE_PROFIT_MARKET) dan SL (STOP_MARKET)
    """

    BASE_URL = BINANCE_BASE_URL  # https://testnet.binancefuture.com

    def __init__(self):
        self._exchange_info_cache: Dict = {}   # cache per symbol
        self._open_positions: set = set()      # track posisi aktif

    # ─────────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────────

    def execute(self, signal: SimulationResult, risk_pct: Optional[float] = None) -> TradeResult:
        """
        Eksekusi sinyal trading lengkap.
        Returns TradeResult dengan detail order.
        """
        symbol = signal.symbol

        # Cek ke Binance langsung — jangan duplikat posisi yang sudah ada
        if self._has_open_position(symbol):
            logger.info(f"[{symbol}] Sudah ada posisi terbuka di Binance, skip.")
            return TradeResult(
                symbol=symbol, direction=signal.direction,
                success=False, leverage_used=0, quantity=0,
                entry_price=signal.entry_price,
                take_profit=signal.take_profit,
                stop_loss=signal.stop_loss,
                order_id=None, tp_order_id=None, sl_order_id=None,
                margin_used=0, error_msg="Posisi sudah terbuka"
            )

        try:
            # Step 0: Cek spread bid/ask — jika terlalu lebar, tolak trade
            if SPREAD_PROTECTION_ENABLED:
                spread_pct, spread_ok = self._check_spread(symbol)
                if not spread_ok:
                    logger.warning(
                        f"[{symbol}] ⚠️ SPREAD TERLALU LEBAR: {spread_pct:.4f}% "
                        f"(max={MAX_SPREAD_PCT}%). Trade dibatalkan untuk melindungi dari slippage!"
                    )
                    return TradeResult(
                        symbol=symbol, direction=signal.direction,
                        success=False, leverage_used=0, quantity=0,
                        entry_price=signal.entry_price,
                        take_profit=signal.take_profit,
                        stop_loss=signal.stop_loss,
                        order_id=None, tp_order_id=None, sl_order_id=None,
                        margin_used=0,
                        error_msg=f"Spread terlalu lebar: {spread_pct:.4f}% (max {MAX_SPREAD_PCT}%)"
                    )

            # Step 1: Ambil info pair (precision, max leverage)
            info = self._get_symbol_info(symbol)
            if not info:
                raise ValueError(f"Tidak bisa ambil info untuk {symbol}")

            max_lev      = info['max_leverage']
            qty_step     = info['qty_step']
            price_tick   = info['price_tick']
            min_qty      = info['min_qty']

            # Step 2: Tentukan leverage dengan dynamic safe leverage capping
            # sl_pct = jarak stop loss ke entry price dalam %
            sl_pct = abs(signal.entry_price - signal.stop_loss) / signal.entry_price if signal.entry_price > 0 else 0
            if sl_pct > 0:
                max_safe_leverage = max(1, int(0.85 / sl_pct))
                logger.info(f"[{symbol}] Jarak SL: {sl_pct*100:.2f}% | Max Safe Leverage: {max_safe_leverage}x")
            else:
                max_safe_leverage = max_lev
                logger.info(f"[{symbol}] Jarak SL: 0% | Max Safe Leverage: {max_safe_leverage}x")

            leverage = min(LEVERAGE, max_lev, max_safe_leverage)
            logger.info(f"[{symbol}] Leverage: target={LEVERAGE}x, max={max_lev}x, safe={max_safe_leverage}x → pakai {leverage}x")

            # Step 3: Set margin type ISOLATED
            self._set_margin_type(symbol, MARGIN_TYPE)

            # Step 4: Set leverage
            self._set_leverage(symbol, leverage)

            # Step 5: Ambil balance & hitung quantity dengan professional risk-based sizing
            balance = self._get_available_balance()
            if balance <= 0:
                raise ValueError(f"Balance tidak cukup: {balance} USDT")

            quantity, margin_used = self._calc_quantity(
                balance=balance,
                price=signal.entry_price,
                stop_loss=signal.stop_loss,
                leverage=leverage,
                qty_step=qty_step,
                min_qty=min_qty,
                risk_pct=risk_pct,
            )

            if quantity <= 0:
                raise ValueError(f"Quantity terlalu kecil: {quantity}")

            # Step 6: Open market order
            side = 'BUY' if signal.direction == 'LONG' else 'SELL'
            order = self._place_market_order(symbol, side, quantity)
            order_id = order.get('orderId')

            # Ambil actual fill price dari order response
            fill_price = float(order.get('avgPrice') or signal.entry_price)
            if fill_price == 0:
                fill_price = signal.entry_price

            logger.info(
                f"[{symbol}] ✅ Market order filled | "
                f"side={side} qty={quantity} price={fill_price:.4f} "
                f"orderId={order_id}"
            )

            # Step 7: Recalculate TP/SL berdasarkan fill price aktual
            # (bisa sedikit berbeda dari signal.entry_price karena slippage)
            tp_price, sl_price = self._recalc_tp_sl(
                fill_price=fill_price,
                signal_entry=signal.entry_price,
                signal_tp=signal.take_profit,
                signal_sl=signal.stop_loss,
                direction=signal.direction,
                price_tick=price_tick,
            )

            # Hitung TP1 Price berdasarkan ATR multiplier
            tp_distance = abs(tp_price - fill_price)
            atr = tp_distance / max(0.1, signal.tp_multiplier)
            tp1_distance = atr * PARTIAL_TP_ATR_MULTIPLIER
            if signal.direction == 'LONG':
                tp1_price = fill_price + tp1_distance
            else:
                tp1_price = fill_price - tp1_distance
            tp1_price = self._round_to_tick(tp1_price, price_tick)

            # Hitung split quantity (50% untuk TP1)
            qty_1 = self._floor_to_step(quantity * 0.5, qty_step)
            qty_2 = self._floor_to_step(quantity - qty_1, qty_step)

            is_partial = False
            tp1_order_id = None
            tp2_order_id = None
            tp_order_id = None

            tp_side = 'SELL' if signal.direction == 'LONG' else 'BUY'

            # Cek apakah quantity cukup besar untuk dibagi dua, dan partial TP diaktifkan
            if ENABLE_PARTIAL_TP and qty_1 >= min_qty and qty_2 >= min_qty:
                is_partial = True
                logger.info(
                    f"[{symbol}] Split trade: qty1={qty_1}, qty2={qty_2} | "
                    f"TP1={tp1_price:.4f}, TP2={tp_price:.4f}"
                )

                # Step 8a: Pasang TP1 (partial, close_position=False)
                tp_order_1 = self._place_tp_order(symbol, tp_side, qty_1, tp1_price, close_position=False)
                tp1_order_id = tp_order_1.get('algoId') if tp_order_1 else None

                # Step 8b: Pasang TP2 (final, close_position=True)
                tp_order_2 = self._place_tp_order(symbol, tp_side, qty_2, tp_price, close_position=True)
                tp2_order_id = tp_order_2.get('algoId') if tp_order_2 else None

                tp_order_id = tp2_order_id
            else:
                # Fallback: single TP (100% position)
                if not ENABLE_PARTIAL_TP:
                    logger.info(f"[{symbol}] Partial TP dinonaktifkan, menggunakan single TP di harga {tp_price:.4f}.")
                else:
                    logger.info(f"[{symbol}] Quantity terlalu kecil untuk split ({quantity}), menggunakan single TP di harga {tp_price:.4f}.")
                tp_order = self._place_tp_order(symbol, tp_side, quantity, tp_price, close_position=True)
                tp_order_id = tp_order.get('algoId') if tp_order else None

            # Step 9: Pasang SL order (always 100% with close_position=True)
            sl_order = self._place_sl_order(symbol, tp_side, quantity, sl_price, close_position=True)
            sl_order_id = sl_order.get('algoId') if sl_order else None

            self._open_positions.add(symbol)

            if is_partial:
                logger.info(
                    f"[{symbol}] 🎯 TP1={tp1_price:.4f} (orderId={tp1_order_id}) | "
                    f"🎯 TP2={tp_price:.4f} (orderId={tp2_order_id}) | "
                    f"🛑 SL={sl_price:.4f} (orderId={sl_order_id})"
                )
            else:
                logger.info(
                    f"[{symbol}] 🎯 TP={tp_price:.4f} (orderId={tp_order_id}) | "
                    f"🛑 SL={sl_price:.4f} (orderId={sl_order_id})"
                )

            return TradeResult(
                symbol=symbol,
                direction=signal.direction,
                success=True,
                leverage_used=leverage,
                quantity=quantity,
                entry_price=fill_price,
                take_profit=tp_price,
                stop_loss=sl_price,
                order_id=order_id,
                tp_order_id=tp_order_id,
                sl_order_id=sl_order_id,
                margin_used=round(margin_used, 2),
                tp1_order_id=tp1_order_id,
                tp2_order_id=tp2_order_id,
                is_partial=is_partial,
                tp1_price=tp1_price,
            )

        except Exception as e:
            logger.error(f"[{symbol}] Trade execution failed: {e}", exc_info=True)
            return TradeResult(
                symbol=symbol, direction=signal.direction,
                success=False, leverage_used=0, quantity=0,
                entry_price=signal.entry_price,
                take_profit=signal.take_profit,
                stop_loss=signal.stop_loss,
                order_id=None, tp_order_id=None, sl_order_id=None,
                margin_used=0, error_msg=str(e)
            )

    def get_open_positions(self) -> list:
        """Ambil semua posisi terbuka dari Binance."""
        data = self._signed_get('/fapi/v2/positionRisk')
        active = [
            p for p in data
            if float(p.get('positionAmt', 0)) != 0
        ]
        # Sync internal tracker
        self._open_positions = {p['symbol'] for p in active}
        return active

    def count_open_positions(self) -> int:
        """Hitung jumlah posisi terbuka saat ini."""
        return len(self.get_open_positions())

    def _has_open_position(self, symbol: str) -> bool:
        """
        Cek langsung ke Binance apakah symbol ini sudah punya posisi terbuka.
        Ini yang mencegah duplikat order — selalu query real-time, bukan cache.
        """
        try:
            data = self._signed_get('/fapi/v2/positionRisk', {'symbol': symbol})
            for p in data:
                if float(p.get('positionAmt', 0)) != 0:
                    logger.debug(f"[{symbol}] Posisi aktif: {p['positionAmt']} @ {p['entryPrice']}")
                    return True
            return False
        except Exception as e:
            logger.error(f"[{symbol}] Error cek posisi: {e}")
            # Kalau gagal cek, lebih aman anggap sudah ada posisi (jangan buka baru)
            return True


    # ─────────────────────────────────────────────────────────────────
    # Spread Protection
    # ─────────────────────────────────────────────────────────────────

    def _check_spread(self, symbol: str) -> tuple:
        """
        Ambil spread bid/ask real-time dari Binance Futures bookTicker.

        Returns:
            (spread_pct: float, is_ok: bool)
            spread_pct = (ask - bid) / mid_price * 100
            is_ok      = True jika spread masih dalam batas MAX_SPREAD_PCT
        """
        try:
            url    = f"{BINANCE_BASE_URL}/fapi/v1/ticker/bookTicker"
            params = {'symbol': symbol}
            resp   = requests.get(url, params=params, timeout=5)
            data   = resp.json()
            bid = float(data.get('bidPrice', 0))
            ask = float(data.get('askPrice', 0))
            if bid <= 0 or ask <= 0:
                logger.warning(f"[{symbol}] bookTicker mengembalikan nilai 0, skip cek spread.")
                return 0.0, True   # Anggap OK jika data tidak valid (fail-open)
            mid_price  = (bid + ask) / 2.0
            spread_pct = ((ask - bid) / mid_price) * 100.0
            is_ok      = spread_pct <= MAX_SPREAD_PCT
            logger.info(
                f"[{symbol}] Spread: bid={bid} ask={ask} → {spread_pct:.4f}% "
                f"(max={MAX_SPREAD_PCT}%) {'✅ OK' if is_ok else '❌ TERLALU LEBAR'}"
            )
            return spread_pct, is_ok
        except Exception as e:
            logger.warning(f"[{symbol}] Gagal cek spread: {e}. Lanjut tanpa cek (fail-open).")
            return 0.0, True   # Fail-open: jangan blok trade hanya karena API error

    # ─────────────────────────────────────────────────────────────────
    # Exchange Info & Precision
    # ─────────────────────────────────────────────────────────────────


    def _get_symbol_info(self, symbol: str) -> Optional[Dict]:
        """
        Ambil info pair: max leverage, qty step, price tick, min qty.
        Di-cache per symbol untuk efisiensi.
        """
        if symbol in self._exchange_info_cache:
            return self._exchange_info_cache[symbol]

        try:
            # Exchange info (public endpoint)
            url = f"{self.BASE_URL}/fapi/v1/exchangeInfo"
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            exchange_data = resp.json()

            sym_data = next(
                (s for s in exchange_data['symbols'] if s['symbol'] == symbol),
                None
            )
            if not sym_data:
                return None

            # Parse filters
            qty_step   = 1.0
            price_tick = 0.01
            min_qty    = 0.001

            for f in sym_data.get('filters', []):
                if f['filterType'] == 'LOT_SIZE':
                    qty_step = float(f['stepSize'])
                    min_qty  = float(f['minQty'])
                elif f['filterType'] == 'PRICE_FILTER':
                    price_tick = float(f['tickSize'])

            # Max leverage dari leverage brackets
            max_lev = self._get_max_leverage(symbol)

            info = {
                'qty_step':    qty_step,
                'price_tick':  price_tick,
                'min_qty':     min_qty,
                'max_leverage': max_lev,
            }

            self._exchange_info_cache[symbol] = info
            return info

        except Exception as e:
            logger.error(f"Error getting symbol info for {symbol}: {e}")
            return None

    def _get_max_leverage(self, symbol: str) -> int:
        """
        Ambil max leverage yang diizinkan untuk pair ini.
        Gunakan leverage brackets endpoint.
        """
        try:
            data = self._signed_get(
                '/fapi/v1/leverageBracket',
                params={'symbol': symbol}
            )
            # Response: list of bracket objects
            if isinstance(data, list) and data:
                brackets = data[0].get('brackets', [])
            elif isinstance(data, dict):
                brackets = data.get('brackets', [])
            else:
                brackets = []

            if brackets:
                # Bracket pertama = tier terendah notional = max leverage
                max_lev = int(brackets[0].get('initialLeverage', 20))
                logger.debug(f"[{symbol}] Max leverage from brackets: {max_lev}x")
                return max_lev

        except Exception as e:
            logger.warning(f"[{symbol}] Gagal ambil leverage bracket: {e}, pakai default 20x")

        return 20  # fallback

    # ─────────────────────────────────────────────────────────────────
    # Pre-trade Setup
    # ─────────────────────────────────────────────────────────────────

    def _set_margin_type(self, symbol: str, margin_type: str = 'ISOLATED'):
        """Set margin type. Ignore error jika sudah sesuai."""
        try:
            self._signed_post('/fapi/v1/marginType', {
                'symbol':     symbol,
                'marginType': margin_type.upper(),
            })
            logger.debug(f"[{symbol}] Margin type set to {margin_type}")
        except Exception as e:
            # Binance returns error -4046 jika margin type sudah sama → aman diabaikan
            err_str = str(e)
            if '-4046' in err_str or 'No need to change' in err_str:
                logger.debug(f"[{symbol}] Margin type sudah {margin_type}, skip.")
            else:
                logger.warning(f"[{symbol}] Set margin type warning: {e}")

    def _set_leverage(self, symbol: str, leverage: int):
        """Set leverage untuk pair."""
        try:
            resp = self._signed_post('/fapi/v1/leverage', {
                'symbol':   symbol,
                'leverage': leverage,
            })
            logger.debug(f"[{symbol}] Leverage set to {resp.get('leverage')}x")
        except Exception as e:
            logger.warning(f"[{symbol}] Set leverage warning: {e}")

    # ─────────────────────────────────────────────────────────────────
    # Balance & Quantity
    # ─────────────────────────────────────────────────────────────────

    def _get_available_balance(self) -> float:
        """Ambil available USDT balance dari futures wallet."""
        try:
            data = self._signed_get('/fapi/v2/balance')
            for asset in data:
                if asset.get('asset') == 'USDT':
                    return float(asset.get('availableBalance', 0))
        except Exception as e:
            logger.error(f"Error fetching balance: {e}")
        return 0.0

    def get_margin_usage_pct(self) -> float:
        """
        Hitung persentase margin yang digunakan dari total wallet balance.
        Formula: (total_balance - available_balance) / total_balance
        """
        try:
            data = self._signed_get('/fapi/v2/balance')
            wallet_balance = 0.0
            available_balance = 0.0
            for asset in data:
                if asset.get('asset') == 'USDT':
                    wallet_balance = float(asset.get('balance', 0))
                    available_balance = float(asset.get('availableBalance', 0))
                    break
            
            if wallet_balance > 0:
                margin_used = wallet_balance - available_balance
                return margin_used / wallet_balance
        except Exception as e:
            logger.error(f"Error checking margin usage: {e}")
        return 0.0

    def _calc_quantity(
        self,
        balance: float,
        price: float,
        stop_loss: float,
        leverage: int,
        qty_step: float,
        min_qty: float,
        risk_pct: Optional[float] = None,
    ) -> Tuple[float, float]:
        """
        Hitung quantity berdasarkan risk management profesional.

        Formula:
          risk_amount  = balance × RISK_PER_TRADE
          raw_quantity = risk_amount / abs(price - stop_loss)
          quantity     = floor ke qty_step terdekat

        Jika margin yang dibutuhkan melebihi balance, quantity dipotong agar pas.

        Returns:
          (quantity, margin_used_usdt)
        """
        risk_rate = risk_pct if risk_pct is not None else RISK_PER_TRADE
        risk_amount = balance * risk_rate
        
        sl_distance = abs(price - stop_loss)
        if sl_distance > 0:
            raw_quantity = risk_amount / sl_distance
        else:
            # Fallback jika SL 0 atau sama dengan entry price
            raw_quantity = (risk_amount * leverage) / price

        # Floor ke step size yang valid
        quantity = self._floor_to_step(raw_quantity, qty_step)

        # Pastikan di atas minimum
        if quantity < min_qty:
            quantity = min_qty

        # Pastikan notional di atas minimum Binance ($5 biasanya)
        if quantity * price < MIN_NOTIONAL_USDT:
            quantity = self._ceil_to_step(MIN_NOTIONAL_USDT / price, qty_step)

        margin_used = (quantity * price) / leverage

        # Proteksi: Jika margin yang dibutuhkan melebihi balance, potong quantity agar pas
        if margin_used > balance:
            max_qty_possible = (balance * leverage) / price
            quantity = self._floor_to_step(max_qty_possible, qty_step)
            # Pastikan kembali di atas minimum
            if quantity < min_qty:
                quantity = min_qty
            margin_used = (quantity * price) / leverage

        return quantity, margin_used

    # ─────────────────────────────────────────────────────────────────
    # Order Placement
    # ─────────────────────────────────────────────────────────────────

    def _place_market_order(self, symbol: str, side: str, quantity: float, reduce_only: bool = False) -> Dict:
        """Buka atau tutup posisi dengan market order."""
        params = {
            'symbol':           symbol,
            'side':             side,
            'type':             'MARKET',
            'quantity':         quantity,
            'positionSide':     'BOTH',   # One-way mode
        }
        if reduce_only:
            params['reduceOnly'] = 'true'
        return self._signed_post('/fapi/v1/order', params)

    def _place_tp_order(
        self, symbol: str, side: str, quantity: float, tp_price: float, close_position: bool = True
    ) -> Optional[Dict]:
        """
        Pasang Take Profit via /fapi/v1/algoOrder (CONDITIONAL).
        Docs: POST /fapi/v1/algoOrder dengan algoType=CONDITIONAL
        """
        try:
            params = {
                'algoType':      'CONDITIONAL',
                'symbol':        symbol,
                'side':          side,
                'positionSide':  'BOTH',
                'type':          'TAKE_PROFIT_MARKET',
                'triggerPrice':  tp_price,
                'workingType':   'MARK_PRICE',
                'priceProtect':  'true',
                'timeInForce':   'GTC',
            }
            if close_position:
                params['closePosition'] = 'true'
            else:
                params['quantity'] = quantity
            return self._signed_post('/fapi/v1/algoOrder', params)
        except Exception as e:
            logger.error(f"[{symbol}] TP algo order failed: {e}")
            return None

    def _place_sl_order(
        self, symbol: str, side: str, quantity: float, sl_price: float, close_position: bool = True
    ) -> Optional[Dict]:
        """
        Pasang Stop Loss via /fapi/v1/algoOrder (CONDITIONAL).
        """
        try:
            params = {
                'algoType':      'CONDITIONAL',
                'symbol':        symbol,
                'side':          side,
                'positionSide':  'BOTH',
                'type':          'STOP_MARKET',
                'triggerPrice':  sl_price,
                'workingType':   'MARK_PRICE',
                'priceProtect':  'true',
                'timeInForce':   'GTC',
            }
            if close_position:
                params['closePosition'] = 'true'
            else:
                params['quantity'] = quantity
            return self._signed_post('/fapi/v1/algoOrder', params)
        except Exception as e:
            logger.error(f"[{symbol}] SL algo order failed: {e}")
            return None

    # ─────────────────────────────────────────────────────────────────
    # TP/SL Price Adjustment
    # ─────────────────────────────────────────────────────────────────

    def _recalc_tp_sl(
        self,
        fill_price: float,
        signal_entry: float,
        signal_tp: float,
        signal_sl: float,
        direction: str,
        price_tick: float,
    ) -> Tuple[float, float]:
        """
        Sesuaikan TP/SL ke fill price aktual dan round ke tick size.
        Jika fill price berbeda dari signal entry, geser TP/SL secara proporsional (persentase jarak).
        """
        if signal_entry <= 0:
            signal_entry = fill_price

        tp_pct = abs(signal_tp - signal_entry) / signal_entry
        sl_pct = abs(signal_entry - signal_sl) / signal_entry

        if direction == 'LONG':
            tp_price = fill_price * (1.0 + tp_pct)
            sl_price = fill_price * (1.0 - sl_pct)
        else:
            tp_price = fill_price * (1.0 - tp_pct)
            sl_price = fill_price * (1.0 + sl_pct)

        # Round ke tick size
        tp_price = self._round_to_tick(tp_price, price_tick)
        sl_price = self._round_to_tick(sl_price, price_tick)

        return tp_price, sl_price

    # ─────────────────────────────────────────────────────────────────
    # Signed HTTP Helpers
    # ─────────────────────────────────────────────────────────────────

    def _signed_get(self, path: str, params: dict = None) -> any:
        """GET request dengan HMAC signature."""
        params = params or {}
        params['timestamp'] = int(time.time() * 1000)
        params['recvWindow'] = 60000
        query   = urlencode(params)
        sig     = hmac.new(
            API_SECRET.encode(), query.encode(), hashlib.sha256
        ).hexdigest()
        url     = f"{self.BASE_URL}{path}?{query}&signature={sig}"
        headers = {'X-MBX-APIKEY': API_KEY}
        resp    = requests.get(url, headers=headers, timeout=10)
        self._raise_for_binance_error(resp)
        return resp.json()

    def _signed_post(self, path: str, params: dict = None) -> Dict:
        """POST request dengan HMAC signature."""
        params = params or {}
        params['timestamp'] = int(time.time() * 1000)
        params['recvWindow'] = 60000
        query   = urlencode(params)
        sig     = hmac.new(
            API_SECRET.encode(), query.encode(), hashlib.sha256
        ).hexdigest()
        url     = f"{self.BASE_URL}{path}"
        headers = {'X-MBX-APIKEY': API_KEY}
        resp    = requests.post(
            url, headers=headers,
            data=query + f"&signature={sig}",
            timeout=10
        )
        self._raise_for_binance_error(resp)
        return resp.json()

    def _signed_delete(self, path: str, params: dict = None) -> Dict:
        """DELETE request dengan HMAC signature."""
        params = params or {}
        params['timestamp'] = int(time.time() * 1000)
        params['recvWindow'] = 60000
        query   = urlencode(params)
        sig     = hmac.new(
            API_SECRET.encode(), query.encode(), hashlib.sha256
        ).hexdigest()
        url     = f"{self.BASE_URL}{path}?{query}&signature={sig}"
        headers = {'X-MBX-APIKEY': API_KEY}
        resp    = requests.delete(url, headers=headers, timeout=10)
        self._raise_for_binance_error(resp)
        return resp.json()

    def _cancel_algo_order(self, symbol: str, algo_id: any) -> Optional[Dict]:
        """Batalkan order TP/SL algo di Binance."""
        if not algo_id:
            return None
        try:
            params = {
                'symbol': symbol,
                'algoId': algo_id,
            }
            return self._signed_delete('/fapi/v1/algoOrder', params)
        except Exception as e:
            logger.error(f"[{symbol}] Gagal cancel algo order {algo_id}: {e}")
            return None

    def _cancel_all_algo_orders(self, symbol: str):
        """Batalkan semua open algo orders (TP/SL) dan regular orders untuk symbol."""
        # 1. Batalkan semua algo orders (TP/SL) sekaligus
        try:
            logger.info(f"[{symbol}] Membatalkan semua open algo orders...")
            res_algo = self._signed_delete('/fapi/v1/algoOpenOrders', {'symbol': symbol})
            logger.debug(f"[{symbol}] Cancel open algo orders response: {res_algo}")
        except Exception as e:
            logger.warning(f"[{symbol}] Gagal membatalkan algoOpenOrders: {e}")

        # 2. Batalkan semua regular orders sekaligus
        try:
            logger.info(f"[{symbol}] Membatalkan semua open regular orders...")
            res_reg = self._signed_delete('/fapi/v1/allOpenOrders', {'symbol': symbol})
            logger.debug(f"[{symbol}] Cancel open regular orders response: {res_reg}")
        except Exception as e:
            logger.warning(f"[{symbol}] Gagal membatalkan allOpenOrders: {e}")

    def execute_partial_close(
        self, symbol: str, quantity: float, direction: str, entry_price: float, tp2_price: float
    ) -> Tuple[bool, Optional[int], Optional[int], Optional[str]]:
        """
        Eksekusi partial close (50% posisi) dan pasang SL baru di Breakeven.

        Flow:
        1. Cek posisi aktual di Binance → tentukan berapa yang perlu di-close
        2. Kalau Binance TP1 algo sudah fill (posisi < 60% dari quantity), skip market close
        3. Cancel SEMUA algo orders (SL + TP lama)
        4. Pasang SL baru di Breakeven (entry_price)
        5. Pasang TP2 baru

        Returns: (success, new_tp_algo_id, new_sl_algo_id, error_msg)
        """
        logger.info(f"[{symbol}] ⚡ Memulai proses Partial Close & Breakeven...")
        try:
            info = self._get_symbol_info(symbol)
            if not info:
                raise ValueError("Gagal mengambil info symbol untuk presisi")
            qty_step   = info['qty_step']
            price_tick = info['price_tick']
            min_qty    = info['min_qty']

            # Ambil posisi aktual dari Binance untuk deteksi double-fill
            actual_qty = quantity  # default dari parameter
            try:
                positions = self._signed_get('/fapi/v2/positionRisk', {'symbol': symbol})
                for p in positions:
                    pos_amt = float(p.get('positionAmt', 0))
                    if pos_amt != 0:
                        actual_qty = abs(pos_amt)
                        break
            except Exception as e:
                logger.warning(f"[{symbol}] Gagal cek posisi aktual, pakai quantity dari tracker: {e}")

            side    = 'SELL' if direction == 'LONG' else 'BUY'
            tp_side = side  # sama, untuk close

            # Jika posisi aktual sudah ≤ 60% dari quantity awal,
            # berarti TP1 algo Binance sudah fill → skip market close manual
            already_partially_filled = actual_qty <= (quantity * 0.6)
            if already_partially_filled:
                logger.info(
                    f"[{symbol}] TP1 algo sudah di-fill Binance "
                    f"(actual={actual_qty:.4f} ≤ 60% dari {quantity:.4f}). "
                    f"Skip market close, langsung update SL ke BE."
                )
                remaining_qty = self._floor_to_step(actual_qty, qty_step)
            else:
                # Manual close 50%
                close_qty = self._floor_to_step(abs(quantity) * 0.5, qty_step)
                if close_qty < min_qty:
                    close_qty = min_qty

                # Cek notional minimum sebelum market close (Binance min ~$5)
                # Ambil mark price dari posisi untuk estimasi notional
                try:
                    pos_data = self._signed_get('/fapi/v2/positionRisk', {'symbol': symbol})
                    mark_price = float(pos_data[0].get('markPrice', 0)) if pos_data else 0.0
                except Exception:
                    mark_price = 0.0

                # Jika mark price tidak tersedia, estimasi dari entry_price
                if mark_price <= 0:
                    mark_price = entry_price

                close_notional = close_qty * mark_price
                if close_notional < MIN_NOTIONAL_USDT:
                    # Notional terlalu kecil → skip manual close, TP1 algo sudah/akan handle
                    logger.info(
                        f"[{symbol}] Skip manual partial close: notional {close_notional:.2f} USDT "
                        f"< minimum {MIN_NOTIONAL_USDT} USDT. Langsung update SL ke Breakeven."
                    )
                    remaining_qty = self._floor_to_step(actual_qty, qty_step)
                else:
                    logger.info(f"[{symbol}] Closing {close_qty} parsial via MARKET order (notional={close_notional:.2f} USDT)...")
                    self._place_market_order(symbol, side, close_qty, reduce_only=True)
                    remaining_qty = self._floor_to_step(abs(quantity) - close_qty, qty_step)

            if remaining_qty < min_qty:
                remaining_qty = min_qty

            # Cancel semua algo orders lama (SL + TP)
            logger.info(f"[{symbol}] Membatalkan semua algo orders lama...")
            self._cancel_all_algo_orders(symbol)

            # Pasang SL baru di Breakeven
            sl_price = self._round_to_tick(entry_price, price_tick)
            logger.info(f"[{symbol}] Memasang SL Breakeven di {sl_price:.4f} (remaining={remaining_qty})...")
            new_sl_order = self._place_sl_order(symbol, tp_side, remaining_qty, sl_price, close_position=True)
            new_sl_id = new_sl_order.get('algoId') if new_sl_order else None

            if new_sl_id is None:
                logger.error(f"[{symbol}] ❌ GAGAL pasang SL Breakeven! Posisi TIDAK TERLINDUNGI.")
                # Tetap lanjut pasang TP2, tapi return error agar caller tau
                new_tp_order = self._place_tp_order(symbol, tp_side, remaining_qty, tp2_price, close_position=True)
                new_tp_id = new_tp_order.get('algoId') if new_tp_order else None
                return False, new_tp_id, None, "SL Breakeven gagal dipasang"

            # Pasang TP2 Final
            logger.info(f"[{symbol}] Memasang TP2 Final di {tp2_price:.4f}...")
            new_tp_order = self._place_tp_order(symbol, tp_side, remaining_qty, tp2_price, close_position=True)
            new_tp_id = new_tp_order.get('algoId') if new_tp_order else None

            logger.info(
                f"[{symbol}] ✅ Partial close selesai | "
                f"SL BE={sl_price:.4f} (id={new_sl_id}) | "
                f"TP2={tp2_price:.4f} (id={new_tp_id})"
            )
            return True, new_tp_id, new_sl_id, None

        except Exception as e:
            err_msg = f"Gagal eksekusi partial close: {e}"
            logger.error(f"[{symbol}] {err_msg}", exc_info=True)
            return False, None, None, err_msg

    def execute_complete_close(self, symbol: str, direction: str) -> Tuple[bool, Optional[str]]:
        """
        Menutup seluruh posisi terbuka untuk symbol dan membatalkan semua open order/algo order-nya.
        Returns: (success, error_msg)
        """
        logger.info(f"[{symbol}] ⚠️ Memulai proses penutupan posisi total (Early Close)...")
        try:
            positions = self.get_open_positions()
            pos = next((p for p in positions if p['symbol'] == symbol), None)
            if not pos:
                logger.info(f"[{symbol}] Tidak ditemukan posisi terbuka di Binance.")
                self._cancel_all_algo_orders(symbol)
                return True, None

            qty = float(pos.get('positionAmt', 0))
            if qty == 0:
                logger.info(f"[{symbol}] Posisi sudah 0 di Binance.")
                self._cancel_all_algo_orders(symbol)
                return True, None

            side = 'SELL' if qty > 0 else 'BUY'
            logger.info(f"[{symbol}] Closing {abs(qty)} total posisi via MARKET order...")
            self._place_market_order(symbol, side, abs(qty), reduce_only=True)

            self._cancel_all_algo_orders(symbol)

            if symbol in self._open_positions:
                self._open_positions.remove(symbol)

            logger.info(f"[{symbol}] ✅ Early close selesai.")
            return True, None
        except Exception as e:
            err_msg = f"Gagal eksekusi complete close: {e}"
            logger.error(f"[{symbol}] {err_msg}", exc_info=True)
            return False, err_msg

    def _raise_for_binance_error(self, resp: requests.Response):
        """Raise exception dengan pesan Binance yang jelas."""
        if resp.status_code != 200:
            try:
                err = resp.json()
                code = err.get('code', resp.status_code)
                msg  = err.get('msg', resp.text)
                raise Exception(f"Binance API error {code}: {msg}")
            except ValueError:
                raise Exception(f"HTTP {resp.status_code}: {resp.text}")

    # ─────────────────────────────────────────────────────────────────
    # Math Helpers
    # ─────────────────────────────────────────────────────────────────

    @staticmethod
    def _floor_to_step(value: float, step: float) -> float:
        """Floor value ke kelipatan step terdekat."""
        if step <= 0:
            return value
        precision = max(0, -int(math.floor(math.log10(step))))
        return round(math.floor(value / step) * step, precision)

    @staticmethod
    def _ceil_to_step(value: float, step: float) -> float:
        """Ceil value ke kelipatan step terdekat."""
        if step <= 0:
            return value
        precision = max(0, -int(math.floor(math.log10(step))))
        return round(math.ceil(value / step) * step, precision)

    @staticmethod
    def _round_to_tick(price: float, tick: float) -> float:
        """Round harga ke tick size yang valid."""
        if tick <= 0:
            return price
        precision = max(0, -int(math.floor(math.log10(tick))))
        return round(round(price / tick) * tick, precision)
