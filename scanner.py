"""
scanner.py - Main scanner engine: orchestrate semua komponen
"""

import logging
import time
import concurrent.futures
from typing import List, Optional, Dict, Tuple
from datetime import datetime, timedelta

from config import (
    MC_CONFIDENCE_THRESHOLD, MIN_SIGNAL_SCORE,
    SCAN_INTERVAL_SECONDS, TOP_PAIRS_COUNT,
    AUTO_TRADE, MAX_OPEN_POSITIONS,
    ENABLE_CIO_AGENT, ENABLE_VISUAL_CHECK,
    SIGNAL_COOLDOWN_MINUTES, TRADE_COOLDOWN_MINUTES,
    MC_MIN_WIN_PROBABILITY, MC_MIN_EXPECTED_RETURN,
    MAX_MARGIN_USAGE_PCT, SCAN_TIMEFRAMES,
    EARLY_CLOSE_CONFIDENCE_THRESHOLD, EARLY_CLOSE_WIN_PROB_THRESHOLD,
    SMC_MODE, SMC_MC_CONFIDENCE_THRESHOLD, EARLY_CLOSE_ON_DECAY,
    SMC_OB_RETEST_ENTRY,
    ADAPTIVE_RISK, MIN_PAIR_TRADES_FOR_STATS,
    CIRCUIT_BREAKER_ENABLED, CIRCUIT_BREAKER_LOSSES,
    CIRCUIT_BREAKER_PAUSE_HOURS, RISK_REDUCTION_LOSSES,
    RISK_REDUCTION_PCT, TRACK_MAE_MFE,
    NEWS_BLACKOUT_ENABLED, BLACKOUT_MOVE_SL_TO_BE,
)
from market_data import MarketData
from indicators import TechnicalIndicators
from monte_carlo import MonteCarloEngine, SimulationResult
from notifier import TelegramNotifier
from trader import BinanceTrader
import database as db
from analytics_engine import get_pair_personality, run_all_analytics, get_setup_weight, get_timeframe_weight, get_blacklisted_symbols, get_blacklisted_pair_sessions
import sqlite3
import os

def get_backtest_blocked_pairs() -> set:
    try:
        db_path = os.path.join(os.path.dirname(__file__), 'pair_statistics.db')
        if not os.path.exists(db_path):
            return set()
        conn = sqlite3.connect(db_path)
        rows = conn.execute("SELECT symbol FROM pair_stats WHERE win_rate < 0.40 AND total_trades >= 10").fetchall()
        conn.close()
        return {r[0] for r in rows}
    except Exception as e:
        logger.error(f"Error fetching backtest blocked pairs: {e}")
        return set()

from market_context import get_full_context
from news_filter import get_news_filter
import gemini_client
import charting_engine
import rag_memory

logger = logging.getLogger(__name__)


class CircuitBreaker:
    def __init__(self):
        pass

    def check(self) -> Tuple[bool, float, str]:
        """
        Check circuit breaker status based on consecutive losses.
        Returns:
            is_paused: bool (True if trading should be fully paused)
            risk_multiplier: float (multiplier for risk sizing, e.g. 0.5 or 1.0)
            reason: str (status description or reason for pause/reduction)
        """
        if not CIRCUIT_BREAKER_ENABLED:
            return False, 1.0, ""

        consecutive_losses = db.get_consecutive_losses()
        if consecutive_losses == 0:
            return False, 1.0, ""

        last_loss_str = db.get_last_loss_close_time()
        if not last_loss_str:
            return False, 1.0, ""

        try:
            clean_str = last_loss_str.replace('Z', '+00:00')
            last_loss_time = datetime.fromisoformat(clean_str).replace(tzinfo=None)
        except Exception as e:
            logger.error(f"[CircuitBreaker] Failed to parse last loss close time '{last_loss_str}': {e}")
            return False, 1.0, ""

        time_since_loss = datetime.utcnow() - last_loss_time

        # 5 losses in a row -> pause 4 hours
        if consecutive_losses >= CIRCUIT_BREAKER_LOSSES:
            pause_duration = timedelta(hours=CIRCUIT_BREAKER_PAUSE_HOURS)
            if time_since_loss < pause_duration:
                remaining_secs = (pause_duration - time_since_loss).total_seconds()
                remaining_str = f"{int(remaining_secs // 3600)}j {int((remaining_secs % 3600) // 60)}m"
                return True, 0.0, f"Circuit Breaker Aktif: {consecutive_losses} loss berturut-turut. Pause trading selama {CIRCUIT_BREAKER_PAUSE_HOURS} jam. Sisa pause: {remaining_str}."

        # 3 losses in a row -> risk -50% for 2 hours
        if consecutive_losses >= RISK_REDUCTION_LOSSES:
            reduction_duration = timedelta(hours=2)
            if time_since_loss < reduction_duration:
                remaining_secs = (reduction_duration - time_since_loss).total_seconds()
                remaining_str = f"{int(remaining_secs // 3600)}j {int((remaining_secs % 3600) // 60)}m"
                return False, RISK_REDUCTION_PCT, f"Risk Reduction Aktif: {consecutive_losses} loss berturut-turut. Risk dikurangi {int((1-RISK_REDUCTION_PCT)*100)}% selama 2 jam. Sisa pengurangan: {remaining_str}."

        return False, 1.0, ""


class NeraScanner:
    """
    Main scanner: scan top 50 pairs, jalankan Monte Carlo,
    filter sinyal kuat, eksekusi order, kirim notifikasi Telegram.
    """

    def __init__(self):
        self.market     = MarketData()
        self.indicator  = TechnicalIndicators()
        self.mc_engine  = MonteCarloEngine()
        self.notifier   = TelegramNotifier()
        self.trader     = BinanceTrader()
        self.scan_count = 0
        self._circuit_breaker = CircuitBreaker()

        # News Blackout Filter (singleton, starts background refresh thread)
        if NEWS_BLACKOUT_ENABLED:
            self._news_filter = get_news_filter()
            logger.info("[NewsFilter] News Blackout Filter: AKTIF ✅")
        else:
            self._news_filter = None
            logger.info("[NewsFilter] News Blackout Filter: NONAKTIF")

        # Cooldown trackers: symbol -> datetime terakhir sinyal/trade
        self._signal_cooldown: Dict[str, datetime] = {}
        self._trade_cooldown:  Dict[str, datetime] = {}

        # Blacklist dikelola oleh MarketData (persist ke file)

        # Active trade persistence
        self._active_trades_file = '/home/ajiekusumadhany.me/public_html/nera-quant/active_trades.json'
        self.active_trades = self._load_active_trades()

        # Pending setups persistence
        self._pending_setups_file = '/home/ajiekusumadhany.me/public_html/nera-quant/pending_setups.json'
        self.pending_setups = self._load_pending_setups()

    def _get_adaptive_risk_pct(self, symbol: str, risk_multiplier: float) -> float:
        from config import RISK_PER_TRADE, ADAPTIVE_RISK
        if not ADAPTIVE_RISK:
            return RISK_PER_TRADE * risk_multiplier
        p = get_pair_personality(symbol)
        base_risk = p.get('recommended_risk_pct', RISK_PER_TRADE)
        return base_risk * risk_multiplier

    def _log_trade_intelligence(self, signal: SimulationResult, trade, setup_type: str, pending_duration_mins: int = 0) -> str:
        try:
            utcnow = datetime.utcnow()
            entry_time_utc = utcnow.strftime('%Y-%m-%dT%H:%M:%SZ')
            trade_ref = f"{signal.symbol}_{utcnow.strftime('%Y%m%d_%H%M%S')}"

            # Session info
            from market_context import get_session_and_meta
            session, hour_utc, weekday = get_session_and_meta(utcnow)

            # SMC signals info
            smc_signals = {
                'bull_ob_top': getattr(signal, 'bull_ob_top', 0.0),
                'bull_ob_bot': getattr(signal, 'bull_ob_bot', 0.0),
                'bear_ob_top': getattr(signal, 'bear_ob_top', 0.0),
                'bear_ob_bot': getattr(signal, 'bear_ob_bot', 0.0)
            }
            if getattr(signal, 'indicator_breakdown', None):
                smc_signals.update(signal.indicator_breakdown)

            # Consecutive losses
            consecutive_losses = db.get_consecutive_losses()

            db.log_trade_open(
                trade_ref=trade_ref,
                symbol=signal.symbol,
                direction=signal.direction,
                entry_time_utc=entry_time_utc,
                timeframe=signal.timeframe,
                session=session,
                entry_hour_utc=hour_utc,
                entry_weekday=weekday,
                setup_type=setup_type,
                smc_signals=smc_signals,
                mc_confidence=signal.confidence,
                mc_win_prob=signal.win_probability,
                signal_score=signal.signal_score,
                risk_reward=signal.risk_reward,
                atr=getattr(signal, 'atr', 0.0),
                atr_pct=getattr(signal, 'atr_pct', 0.0),
                funding_rate=getattr(signal, 'funding_rate', 0.0),
                oi_change=getattr(signal, 'oi_change', 0.0),
                htf_bias=getattr(signal, 'htf_bias', 'NEUTRAL'),
                bb_pct=getattr(signal, 'bb_pct', 0.5),
                rsi=getattr(signal, 'rsi', 50.0),
                macd_cross=getattr(signal, 'macd_cross', 0),
                vol_spike=getattr(signal, 'vol_spike', 0),
                entry_price=trade.entry_price if trade and trade.entry_price else signal.entry_price,
                take_profit=trade.take_profit if trade and trade.take_profit else signal.take_profit,
                stop_loss=trade.stop_loss if trade and trade.stop_loss else signal.stop_loss,
                leverage=trade.leverage_used if trade and trade.leverage_used else 1,
                margin_used=trade.margin_used if trade and trade.margin_used else 0.0,
                pending_duration_mins=pending_duration_mins,
                consecutive_losses_at_entry=consecutive_losses,
                binance_order_id=str(trade.order_id) if trade and trade.order_id else None
            )
            return trade_ref
        except Exception as e:
            logger.error(f"Error logging trade intelligence: {e}", exc_info=True)
            return ""

    def _log_trade_close_details(self, symbol: str):
        active_trade = self.active_trades.get(symbol)
        if not active_trade:
            return
        
        trade_ref = active_trade.get('trade_ref')
        if not trade_ref:
            return

        # Fetch latest user trades from Binance to get exact exit price and PnL
        try:
            # Parse open time to ms
            open_time_ms = 0
            trade_timestamp = active_trade.get('timestamp')
            if trade_timestamp:
                try:
                    from datetime import timezone
                    open_dt = datetime.fromisoformat(trade_timestamp.replace('Z', ''))
                    open_time_ms = int(open_dt.replace(tzinfo=timezone.utc).timestamp() * 1000)
                except Exception as te:
                    logger.warning(f"[{symbol}] Failed to parse active trade timestamp '{trade_timestamp}': {te}")

            trades_data = self.trader._signed_get('/fapi/v1/userTrades', {'symbol': symbol, 'limit': 10})
            if isinstance(trades_data, list) and len(trades_data) > 0:
                # Find the most recent trade with non-zero realized PnL and time >= open_time_ms (with 1 min tolerance)
                closed_trades = [
                    t for t in trades_data 
                    if float(t.get('realizedPnl', 0.0)) != 0.0
                    and int(t.get('time', 0)) >= (open_time_ms - 60000)
                ]
                if closed_trades:
                    closed_trades.sort(key=lambda x: int(x.get('time', 0)), reverse=True)
                    latest_close = closed_trades[0]
                    exit_price = float(latest_close.get('price'))
                    realized_pnl = float(latest_close.get('realizedPnl'))
                    close_time_ms = int(latest_close.get('time'))
                    close_time_utc = datetime.utcfromtimestamp(close_time_ms / 1000.0).strftime('%Y-%m-%dT%H:%M:%SZ')
                    binance_order_id = str(latest_close.get('orderId'))

                    db.log_trade_close(
                        trade_ref=trade_ref,
                        exit_price=exit_price,
                        result_pnl=realized_pnl,
                        close_time_utc=close_time_utc,
                        binance_order_id=binance_order_id
                    )
                    logger.info(f"[DI] Position exit captured from userTrades for {symbol}: exit_price={exit_price}, pnl={realized_pnl}")
                    try:
                        run_all_analytics()
                    except Exception as ae:
                        logger.error(f"Gagal memicu run_all_analytics() setelah userTrades close: {ae}")

                    # Feature 5: Store pattern in RAG memory
                    try:
                        _features = active_trade.get('indicator_breakdown') or {}
                        _outcome_str = 'WIN' if realized_pnl > 0 else ('LOSS' if realized_pnl < 0 else 'BE')
                        rag_memory.store_pattern(
                            trade_ref=trade_ref,
                            symbol=symbol,
                            direction=active_trade.get('direction', ''),
                            features=_features,
                            outcome=_outcome_str,
                            result_pnl=realized_pnl,
                            risk_reward=active_trade.get('risk_reward', 0.0),
                            session=active_trade.get('session'),
                            timeframe=active_trade.get('timeframe'),
                        )
                    except Exception as re:
                        logger.debug(f"[RAG] store_pattern skipped (no features): {re}")

                    # Feature 4: Trigger L3 meta-feedback (async-safe, best-effort)
                    try:
                        from analytics_engine import run_meta_feedback_loop
                        run_meta_feedback_loop(limit=5)
                    except Exception as me:
                        logger.debug(f"[MetaFeedback] Skipped: {me}")

                    return
                else:
                    logger.info(f"[{symbol}] No recent exit trades found in userTrades since open_time_ms={open_time_ms}. Fallback to estimation.")
        except Exception as e:
            logger.warning(f"[{symbol}] Failed to fetch exact exit details from userTrades: {e}. Fallback to estimation.")

        # Fallback to estimated exit parameters if Binance fetch fails or finds nothing
        mark_price = self.market.get_ticker_price(symbol) or active_trade['entry_price']
        entry_price = active_trade['entry_price']
        direction = active_trade['direction']
        quantity = active_trade['quantity']
        
        # Estimate PnL
        est_pnl = 0.0
        if direction == 'LONG':
            est_pnl = (mark_price - entry_price) * quantity
        else:
            est_pnl = (entry_price - mark_price) * quantity

        close_time_utc = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
        db.log_trade_close(
            trade_ref=trade_ref,
            exit_price=mark_price,
            result_pnl=est_pnl,
            close_time_utc=close_time_utc
        )
        logger.info(f"[DI] Position exit estimated for {symbol}: exit_price={mark_price}, pnl={est_pnl}")
        try:
            run_all_analytics()
        except Exception as ae:
            logger.error(f"Gagal memicu run_all_analytics() setelah estimated close: {ae}")

        # Feature 5: Store pattern in RAG memory (estimated close)
        try:
            _features = {}
            _outcome_str = 'WIN' if est_pnl > 0 else ('LOSS' if est_pnl < 0 else 'BE')
            rag_memory.store_pattern(
                trade_ref=trade_ref,
                symbol=symbol,
                direction=active_trade.get('direction', ''),
                features=_features,
                outcome=_outcome_str,
                result_pnl=est_pnl,
                risk_reward=active_trade.get('risk_reward', 0.0),
                session=active_trade.get('session'),
                timeframe=active_trade.get('timeframe'),
            )
        except Exception as re:
            logger.debug(f"[RAG] store_pattern (estimated) skipped: {re}")

        # Feature 4: Trigger L3 meta-feedback
        try:
            from analytics_engine import run_meta_feedback_loop
            run_meta_feedback_loop(limit=5)
        except Exception as me:
            logger.debug(f"[MetaFeedback] Skipped: {me}")

    def _load_active_trades(self) -> dict:
        import os
        import json
        if os.path.exists(self._active_trades_file):
            try:
                with open(self._active_trades_file, 'r') as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"Gagal me-load active_trades.json: {e}")
        return {}

    def _save_active_trades(self):
        import json
        try:
            with open(self._active_trades_file, 'w') as f:
                json.dump(self.active_trades, f, indent=4)
        except Exception as e:
            logger.error(f"Gagal menyimpan active_trades.json: {e}")

    def _load_pending_setups(self) -> dict:
        import os
        import json
        if os.path.exists(self._pending_setups_file):
            try:
                with open(self._pending_setups_file, 'r') as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"Gagal me-load pending_setups.json: {e}")
        return {}

    def _save_pending_setups(self):
        import json
        try:
            with open(self._pending_setups_file, 'w') as f:
                json.dump(self.pending_setups, f, indent=4)
        except Exception as e:
            logger.error(f"Gagal menyimpan pending_setups.json: {e}")


    def run_forever(self):
        """Loop utama: scan terus menerus setiap SCAN_INTERVAL_SECONDS."""
        logger.info("=" * 60)
        logger.info("  NERA QUANT - Trading AI Scanner")
        logger.info("  Monte Carlo Probability Engine")
        logger.info(f"  AUTO TRADE: {'ON ✅' if AUTO_TRADE else 'OFF (signal only)'}")
        logger.info("=" * 60)

        self.notifier.send_startup()

        while True:
            try:
                self.scan_count += 1
                logger.info(f"\n{'='*50}")
                logger.info(f"SCAN #{self.scan_count} | {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}")
                logger.info(f"{'='*50}")

                signals = self.run_scan()

                # Kirim summary setiap 10 scan
                if self.scan_count % 10 == 0:
                    self.notifier.send_scan_summary(
                        total_pairs=TOP_PAIRS_COUNT,
                        signals_found=len(signals),
                        top_signals=signals[:5]
                    )

                logger.info(f"Scan selesai. {len(signals)} sinyal ditemukan.")
                logger.info(f"Menunggu {SCAN_INTERVAL_SECONDS}s untuk scan berikutnya...")
                time.sleep(SCAN_INTERVAL_SECONDS)

            except KeyboardInterrupt:
                logger.info("Scanner dihentikan oleh user.")
                break
            except Exception as e:
                logger.error(f"Error di main loop: {e}", exc_info=True)
                self.notifier.send_error(str(e))
                time.sleep(30)

    def run_scan(self) -> List[SimulationResult]:
        """
        Jalankan satu siklus scan lengkap.
        Returns list sinyal yang memenuhi threshold, sorted by confidence.
        """
        # Step 1: Ambil top 50 pairs (sudah difilter tradeable oleh MarketData)
        symbols = self.market.get_top_pairs()
        
        # Pastikan semua symbol yang sedang di-hold ada di daftar scan agar MC diupdate
        for active_symbol in list(self.active_trades.keys()):
            if active_symbol not in symbols:
                symbols.append(active_symbol)

        # ─── BACKTEST BLACKLIST CHECK ──────────────────────────────────────────
        backtest_blocked = get_backtest_blocked_pairs()
        if backtest_blocked:
            symbols = [s for s in symbols if s not in backtest_blocked or s in self.active_trades]
                
        logger.info(f"Scanning {len(symbols)} pairs... (Blocked by backtest: {len(backtest_blocked)})")

        # ─── NEWS BLACKOUT CHECK ───────────────────────────────────────────────
        # Jika blackout aktif (30m sebelum / 15m setelah berita High Impact),
        # suspend semua pembukaan posisi baru dan (opsional) geser SL ke Breakeven.
        _news_blackout_active = (
            self._news_filter is not None
            and self._news_filter.is_blackout_active()
        )
        if _news_blackout_active:
            logger.warning(
                "🚨 [NewsFilter] BLACKOUT AKTIF — Semua pembukaan posisi baru DITANGGUHKAN! "
                "Scan tetap berjalan untuk monitoring posisi aktif."
            )
            self.notifier._send_message(
                "🚨 *NEWS BLACKOUT AKTIF*\n"
                "Berita High Impact akan segera rilis.\n"
                "Bot MENGHENTIKAN pembukaan posisi baru untuk menghindari spike.\n"
                "Monitoring posisi aktif tetap berjalan."
            )

            # Geser SL semua posisi aktif ke Breakeven (jika diaktifkan)
            if BLACKOUT_MOVE_SL_TO_BE and self.active_trades:
                logger.info("[NewsFilter] Mencoba pindahkan SL ke Breakeven untuk semua posisi aktif...")
                for sym, trade_data in list(self.active_trades.items()):
                    try:
                        if trade_data.get('status') != 'OPEN':
                            continue
                        entry_price = float(trade_data.get('entry_price', 0))
                        current_sl  = float(trade_data.get('stop_loss', 0))
                        direction   = trade_data.get('direction', '')
                        if entry_price <= 0 or not direction:
                            continue
                        # Cek apakah SL sudah di breakeven atau lebih baik
                        if direction == 'LONG' and current_sl >= entry_price:
                            logger.info(f"[{sym}] SL sudah di/melewati breakeven ({current_sl:.4f} >= {entry_price:.4f}), skip.")
                            continue
                        if direction == 'SHORT' and current_sl <= entry_price:
                            logger.info(f"[{sym}] SL sudah di/melewati breakeven ({current_sl:.4f} <= {entry_price:.4f}), skip.")
                            continue
                        # Hitung TP2 price (pakai nilai TP aktif dari tracker)
                        tp2_price = float(trade_data.get('take_profit', 0))
                        quantity  = float(trade_data.get('quantity', 0))
                        logger.info(
                            f"[{sym}] Blackout: memindahkan SL ke Breakeven "
                            f"(entry={entry_price:.4f}, SL_lama={current_sl:.4f})"
                        )
                        success, new_tp_id, new_sl_id, err = self.trader.execute_partial_close(
                            symbol     = sym,
                            quantity   = quantity,
                            direction  = direction,
                            entry_price= entry_price,
                            tp2_price  = tp2_price,
                        )
                        if success:
                            self.active_trades[sym]['stop_loss'] = entry_price
                            if new_sl_id:
                                self.active_trades[sym]['sl_order_id'] = new_sl_id
                            self._save_active_trades()
                            logger.info(f"[{sym}] ✅ SL Breakeven terpasang (blackout protection).")
                        else:
                            logger.warning(f"[{sym}] ⚠️ Gagal geser SL ke BE: {err}")
                    except Exception as be_err:
                        logger.error(f"[{sym}] Error saat blackout BE move: {be_err}")


        results = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=30) as executor:
            futures = {}
            for symbol in symbols:
                for tf in SCAN_TIMEFRAMES:
                    futures[executor.submit(self._analyze_pair, symbol, tf)] = (symbol, tf)
            for future in concurrent.futures.as_completed(futures):
                symbol, tf = futures[future]
                try:
                    result = future.result(timeout=30)
                    if result is not None:
                        results.append(result)
                except Exception as e:
                    logger.error(f"Error analyzing {symbol} on {tf}: {e}")

        # Step 2.5: Sinkronisasi active_trades dengan posisi riil di Binance (auto-heal)
        try:
            open_positions = self.trader.get_open_positions()
            open_symbols = {p['symbol'] for p in open_positions}

            # Ambil sisa quantity riil di Binance
            binance_qtys = {}
            for p in open_positions:
                binance_qtys[p['symbol']] = float(p.get('positionAmt', 0))

            # Hapus active trades yang sudah tidak ada di Binance
            for active_symbol in list(self.active_trades.keys()):
                if active_symbol not in open_symbols:
                    logger.info(f"[{active_symbol}] Posisi sudah ditutup di Binance. Hapus dari active_trades dan cancel order.")
                    try:
                        self._log_trade_close_details(active_symbol)
                    except Exception as e:
                        logger.error(f"[{active_symbol}] Failed to log trade close: {e}")
                    self.trader._cancel_all_algo_orders(active_symbol)
                    del self.active_trades[active_symbol]
                    self._save_active_trades()

            # Account-wide auto-heal untuk membersihkan orphaned algo orders di Binance
            try:
                # Query semua open algo orders di akun
                open_algo_data = self.trader._signed_get('/fapi/v1/openAlgoOrders')
                open_algo_symbols = set()
                if isinstance(open_algo_data, list):
                    open_algo_symbols = {o['symbol'] for o in open_algo_data if 'symbol' in o}
                elif isinstance(open_algo_data, dict):
                    orders = open_algo_data.get('orders', [])
                    open_algo_symbols = {o['symbol'] for o in orders if 'symbol' in o}
                
                # Jika ada symbol yang punya open algo orders tapi tidak ada posisi terbuka
                for symbol in open_algo_symbols:
                    if symbol not in open_symbols:
                        logger.warning(f"[{symbol}] Deteksi open algo orders tanpa posisi aktif. Melakukan auto-heal cleanup...")
                        self.trader._cancel_all_algo_orders(symbol)
            except Exception as he:
                logger.debug(f"Gagal menjalankan auto-heal open algo orders: {he}")

        except Exception as e:
            logger.error(f"Gagal melakukan sinkronisasi active_trades dengan Binance: {e}")
            binance_qtys = {}

        # Step 2.6: Active Position Monitoring Loop (TP1 / Breakeven / Early Close)
        analysis_map = {(r.symbol, r.timeframe): r for r in results if r is not None}
        for symbol, active_trade in list(self.active_trades.items()):
            try:
                # Ambil mark price terkini
                mark_price = self.market.get_ticker_price(symbol)
                if not mark_price:
                    continue

                # Update MAE / MFE
                trade_ref = active_trade.get('trade_ref')
                if TRACK_MAE_MFE and trade_ref:
                    db.update_mae_mfe(trade_ref, mark_price)

                direction = active_trade['direction']
                entry_price = active_trade['entry_price']
                status = active_trade['status']
                quantity = abs(binance_qtys.get(symbol, active_trade['quantity']))

                # A. Pengecekan TP1 (Partial Close & Breakeven)
                if active_trade.get('is_partial') and status == 'OPEN':
                    tp1_price = active_trade['tp1_price']
                    tp2_price = active_trade['take_profit'] # Final TP

                    trigger_partial = False
                    if direction == 'LONG' and mark_price >= tp1_price:
                        trigger_partial = True
                    elif direction == 'SHORT' and mark_price <= tp1_price:
                        trigger_partial = True
                    
                    if trigger_partial:
                        logger.info(f"[{symbol}] 🔥 Target TP1 ({tp1_price:.4f}) tercapai di harga {mark_price:.4f}!")
                        success, new_tp_id, new_sl_id, err = self.trader.execute_partial_close(
                            symbol=symbol,
                            quantity=quantity,
                            direction=direction,
                            entry_price=entry_price,
                            tp2_price=tp2_price
                        )
                        if success:
                            # Partial close sukses + SL BE terpasang
                            active_trade['status'] = 'PARTIAL_CLOSED'
                            active_trade['sl_order_id'] = new_sl_id
                            active_trade['tp2_order_id'] = new_tp_id
                            active_trade['tp1_order_id'] = None
                            self._save_active_trades()

                            self.notifier.send_partial_tp_executed(
                                symbol=symbol,
                                direction=direction,
                                qty=quantity * 0.5,
                                price=mark_price,
                                remaining_qty=quantity - (quantity * 0.5)
                            )
                        elif new_tp_id is not None:
                            # Partial close berhasil tapi SL BE gagal dipasang
                            # Tetap mark PARTIAL_CLOSED agar tidak re-trigger market close
                            logger.error(f"[{symbol}] ⚠️ SL Breakeven GAGAL dipasang! TP2 id={new_tp_id}. Posisi tidak terlindungi.")
                            active_trade['status'] = 'PARTIAL_CLOSED'
                            active_trade['sl_order_id'] = None   # SL tidak ada!
                            active_trade['tp2_order_id'] = new_tp_id
                            active_trade['tp1_order_id'] = None
                            self._save_active_trades()
                            self.notifier.send_error(
                                f"⚠️ [{symbol}] SL Breakeven GAGAL dipasang setelah TP1! Error: {err}\n"
                                f"Posisi sisa masih terbuka TANPA SL. Cek manual!"
                            )

                # B. Pengecekan Early Close (Confidence / Win Prob drop / Reversal)
                trade_timeframe = active_trade.get('timeframe', '15m')
                result = analysis_map.get((symbol, trade_timeframe))
                if result:
                    is_reversal = (result.direction != 'NEUTRAL' and result.direction != direction)
                    is_decay = False
                    if EARLY_CLOSE_ON_DECAY:
                        is_decay = (result.confidence < EARLY_CLOSE_CONFIDENCE_THRESHOLD) or (result.win_probability < EARLY_CLOSE_WIN_PROB_THRESHOLD)
                    
                    if is_reversal or is_decay:
                        if is_reversal:
                            logger.warning(
                                f"[{symbol}] ⚠️ Reversal terdeteksi! "
                                f"Direction Baru: {result.direction} (Arah Posisi: {direction}), "
                                f"Confidence Baru: {result.confidence*100:.1f}%, "
                                f"Win Prob Baru: {result.win_probability*100:.1f}%"
                            )
                        else:
                            logger.warning(
                                f"[{symbol}] ⚠️ Decay terdeteksi! "
                                f"Confidence Baru: {result.confidence*100:.1f}% (Threshold: {EARLY_CLOSE_CONFIDENCE_THRESHOLD*100:.1f}%), "
                                f"Win Prob Baru: {result.win_probability*100:.1f}% (Threshold: {EARLY_CLOSE_WIN_PROB_THRESHOLD*100:.1f}%)"
                            )

                        est_pnl = ((mark_price - entry_price) / entry_price * 100) if direction == 'LONG' else ((entry_price - mark_price) / entry_price * 100)

                        success, err = self.trader.execute_complete_close(symbol, direction)
                        if success:
                            if symbol in self.active_trades:
                                try:
                                    self._log_trade_close_details(symbol)
                                except Exception as e:
                                    logger.error(f"[{symbol}] Failed to log early close details: {e}")
                                del self.active_trades[symbol]
                                self._save_active_trades()

                            self.notifier.send_early_close_executed(
                                symbol=symbol,
                                direction=direction,
                                price=mark_price,
                                pnl=est_pnl,
                                confidence=result.confidence,
                                win_prob=result.win_probability
                            )
            except Exception as e:
                logger.error(f"[{symbol}] Error saat monitoring aktif: {e}", exc_info=True)

        # Step 2.7: Pending SMC Setups Monitoring Loop (Trigger or Invalidation)
        for symbol, setup in list(self.pending_setups.items()):
            try:
                # 1. Check expiration (2 hours maximum age)
                setup_time = datetime.fromisoformat(setup['timestamp'].replace('Z', '+00:00')).replace(tzinfo=None)
                if datetime.utcnow() - setup_time > timedelta(hours=2):
                    logger.info(f"[{symbol}] ⏳ SMC Pending Setup expired (older than 2 hours). Cancelling.")
                    mark_price = self.market.get_ticker_price(symbol) or setup['entry_price']
                    self.notifier.send_pending_setup_invalidated(
                        symbol=symbol,
                        direction=setup['direction'],
                        price=mark_price,
                        reason="Setup expired (older than 2 hours)"
                    )
                    del self.pending_setups[symbol]
                    self._save_pending_setups()
                    continue

                # 2. Get current mark price
                mark_price = self.market.get_ticker_price(symbol)
                if not mark_price:
                    continue

                direction = setup['direction']
                trigger_price = setup['trigger_price']
                invalidation_price = setup['invalidation_price']

                # 3. Check invalidation and triggers
                triggered = False
                invalidated = False
                invalidation_reason = ""

                if direction == 'LONG':
                    if mark_price < invalidation_price:
                        invalidated = True
                        invalidation_reason = f"Price broke below invalidation price ({invalidation_price:.4f}) (Stop Loss boundary / OB bottom)"
                    elif mark_price <= trigger_price:
                        triggered = True
                elif direction == 'SHORT':
                    if mark_price > invalidation_price:
                        invalidated = True
                        invalidation_reason = f"Price broke above invalidation price ({invalidation_price:.4f}) (Stop Loss boundary / OB top)"
                    elif mark_price >= trigger_price:
                        triggered = True

                if invalidated:
                    logger.info(f"[{symbol}] ❌ Pending setup invalidated at {mark_price:.4f} | Reason: {invalidation_reason}")
                    self.notifier.send_pending_setup_invalidated(
                        symbol=symbol,
                        direction=direction,
                        price=mark_price,
                        reason=invalidation_reason
                    )
                    del self.pending_setups[symbol]
                    self._save_pending_setups()
                elif triggered:
                    logger.info(f"[{symbol}] 🔥 Pending setup triggered at {mark_price:.4f} (Trigger price: {trigger_price:.4f})")
                    
                    # Check position limits
                    open_count = self.trader.count_open_positions()
                    if open_count >= MAX_OPEN_POSITIONS:
                        logger.warning(f"[{symbol}] Cannot execute triggered setup because MAX_OPEN_POSITIONS ({MAX_OPEN_POSITIONS}) is reached. Skipping this cycle.")
                        continue
                    
                    margin_usage_pct = self.trader.get_margin_usage_pct()
                    if margin_usage_pct >= MAX_MARGIN_USAGE_PCT:
                        logger.warning(
                            f"[{symbol}] Batas total margin tercapai! "
                            f"Margin terpakai: {margin_usage_pct * 100:.1f}% (Batas: {MAX_MARGIN_USAGE_PCT * 100:.1f}%). "
                            f"Skipping triggered setup this cycle."
                        )
                        continue

                    # Cek News Blackout — tunda eksekusi pending setup
                    if _news_blackout_active:
                        logger.warning(
                            f"[{symbol}] 🚫 Pending setup DITUNDA — News Blackout aktif. "
                            f"Setup tetap tersimpan dan akan dicoba di siklus berikutnya."
                        )
                        continue

                    # Reconstruct SimulationResult
                    from monte_carlo import SimulationResult
                    signal = SimulationResult(
                        symbol=setup['symbol'],
                        direction=setup['direction'],
                        confidence=setup['confidence'],
                        win_probability=setup['win_probability'],
                        expected_return=setup.get('expected_return', 0.0),
                        risk_reward=setup['risk_reward'],
                        entry_price=mark_price,
                        take_profit=setup['take_profit'],
                        stop_loss=setup['stop_loss'],
                        simulations_run=setup.get('simulations_run', 5000),
                        profitable_paths=setup.get('profitable_paths', 0),
                        signal_score=setup['signal_score'],
                        tp_multiplier=setup.get('tp_multiplier', 2.5),
                        sl_multiplier=setup.get('sl_multiplier', 1.5),
                        timeframe=setup.get('timeframe', '15m'),
                        bull_ob_top=setup.get('bull_ob_top', 0.0),
                        bull_ob_bot=setup.get('bull_ob_bot', 0.0),
                        bear_ob_top=setup.get('bear_ob_top', 0.0),
                        bear_ob_bot=setup.get('bear_ob_bot', 0.0)
                    )

                    # Execute trade
                    is_paused, risk_mult, cb_reason = self._circuit_breaker.check()
                    if is_paused:
                        logger.warning(f"[{symbol}] Circuit Breaker aktif. Batal mengeksekusi pending setup. Detail: {cb_reason}")
                        continue

                    chart_path = None
                    if ENABLE_VISUAL_CHECK:
                        chart_path = charting_engine.generate_chart(
                            symbol=signal.symbol, timeframe=signal.timeframe,
                            entry_price=mark_price, tp=signal.take_profit, sl=signal.stop_loss,
                            ob_top=getattr(signal, 'bull_ob_top', getattr(signal, 'bear_ob_top', 0)),
                            ob_bot=getattr(signal, 'bull_ob_bot', getattr(signal, 'bear_ob_bot', 0))
                        )
                        
                    if ENABLE_CIO_AGENT:
                        # Feature 1: Multi-analyst debate for pending setup
                        # Feature 5: RAG context enrichment
                        _rag_patterns = rag_memory.find_similar_patterns(
                            features=getattr(signal, 'indicator_breakdown', {}) or {},
                            top_k=5,
                        )
                        _rag_str = rag_memory.format_similar_patterns_for_context(_rag_patterns)
                        _ctx_str = (
                            f"Pending setup triggered. "
                            f"Confidence={signal.confidence:.2f} WinProb={signal.win_probability:.2f} "
                            f"Score={signal.signal_score:.2f} RR={signal.risk_reward:.2f} "
                            f"Entry={mark_price:.4f} TP={signal.take_profit:.4f} SL={signal.stop_loss:.4f}"
                        )
                        debate = gemini_client.ask_gemini_debate(
                            symbol=signal.symbol,
                            direction=signal.direction,
                            context_str=_ctx_str,
                            chart_path=chart_path,
                            similar_patterns_str=_rag_str,
                        )
                        if debate['verdict'] == 'REJECT':
                            logger.warning(
                                f"[{symbol}] CIO Debate REJECTED pending setup execution. "
                                f"Bull={debate.get('bull_strength')} Bear={debate.get('bear_strength')} | {debate['reasoning']}"
                            )
                            continue
                        _cio_verdict = debate['verdict']
                        _cio_bull    = debate.get('bull', '')
                        _cio_bear    = debate.get('bear', '')
                    else:
                        _cio_verdict = None
                        _cio_bull    = ''
                        _cio_bear    = ''

                    risk_pct = self._get_adaptive_risk_pct(symbol, risk_mult)
                    trade = self.trader.execute(signal, risk_pct=risk_pct)
                    self.notifier.send_pending_setup_triggered(signal, trade, chart_path, debate if ENABLE_CIO_AGENT else None)
                    
                    # Clean up pending setup regardless of execution success to prevent duplicate trigger loops
                    del self.pending_setups[symbol]
                    self._save_pending_setups()

                    if trade.success:
                        self._trade_cooldown[(symbol, setup['timeframe'])] = datetime.utcnow()
                        
                        setup_dur_mins = int((datetime.utcnow() - setup_time).total_seconds() / 60)
                        # Pending setups saat ini hanya dibuat untuk SMC_OB_PULLBACK
                        # tapi jika di masa depan OI_DIVERGENCE juga punya pending mode,
                        # cek oi_divergence di sini juga
                        _pend_setup_type = 'SMC_OB_PULLBACK'
                        if getattr(signal, 'oi_divergence', 0) != 0:
                            _pend_setup_type = 'OI_DIVERGENCE'
                        trade_ref = self._log_trade_intelligence(signal, trade, _pend_setup_type, setup_dur_mins)

                        # Feature 4: Save CIO debate details for later meta-eval
                        if _cio_verdict and trade_ref:
                            db.save_meta_feedback(
                                trade_ref=trade_ref,
                                meta_feedback='',
                                cio_verdict=_cio_verdict,
                                cio_bull_reasoning=_cio_bull[:500],
                                cio_bear_reasoning=_cio_bear[:500],
                            )

                        # Add to active_trades tracker
                        self.active_trades[trade.symbol] = {
                            'symbol':               trade.symbol,
                            'direction':            trade.direction,
                            'quantity':             trade.quantity,
                            'entry_price':          trade.entry_price,
                            'take_profit':          trade.take_profit,
                            'stop_loss':            trade.stop_loss,
                            'tp1_price':            getattr(trade, 'tp1_price', 0.0),
                            'is_partial':           getattr(trade, 'is_partial', False),
                            'status':               'OPEN',
                            'tp1_order_id':         getattr(trade, 'tp1_order_id', None),
                            'tp2_order_id':         getattr(trade, 'tp2_order_id', None),
                            'sl_order_id':          trade.sl_order_id,
                            'timestamp':            datetime.utcnow().isoformat(),
                            'timeframe':            setup['timeframe'],
                            'trade_ref':            trade_ref,
                            'indicator_breakdown':  getattr(signal, 'indicator_breakdown', {}) or {},
                            'risk_reward':          signal.risk_reward,
                            'session':              setup.get('session', ''),
                        }
                        self._save_active_trades()

                        import api_server as api
                        api.append_trade({
                            'symbol':    trade.symbol,
                            'direction': trade.direction,
                            'entry':     trade.entry_price,
                            'tp':        trade.take_profit,
                            'sl':        trade.stop_loss,
                            'leverage':  trade.leverage_used,
                            'margin':    trade.margin_used,
                            'pnl':       0,
                            'timestamp': datetime.utcnow().isoformat(),
                            'timeframe': setup['timeframe'],
                        })
            except Exception as e:
                logger.error(f"[{symbol}] Error saat monitoring pending setup: {e}", exc_info=True)

        # Step 2.8: Circuit Breaker Pre-Scan Validation
        is_paused, risk_mult, cb_reason = self._circuit_breaker.check()
        if is_paused:
            logger.warning(f"Circuit Breaker AKTIF: New trading signals will be bypassed. Reason: {cb_reason}")
            if not getattr(self, '_cb_alert_sent', False):
                last_loss_str = db.get_last_loss_close_time()
                resume_at = "Unknown"
                if last_loss_str:
                    try:
                        clean_str = last_loss_str.replace('Z', '+00:00')
                        last_loss_time = datetime.fromisoformat(clean_str).replace(tzinfo=None)
                        resume_dt = last_loss_time + timedelta(hours=CIRCUIT_BREAKER_PAUSE_HOURS)
                        resume_at = resume_dt.strftime('%H:%M:%S UTC')
                    except Exception:
                        pass
                self.notifier.send_circuit_breaker_alert(cb_reason, resume_at)
                self._cb_alert_sent = True
            strong_signals = []
        else:
            self._cb_alert_sent = False

            # Step 3: Filter sinyal yang memenuhi threshold
            target_conf_threshold = SMC_MC_CONFIDENCE_THRESHOLD if SMC_MODE else MC_CONFIDENCE_THRESHOLD

            # Feature 3: Load auto-blacklist (Standing Orders)
            _blacklisted_pairs    = get_blacklisted_symbols()
            _blacklisted_sessions = get_blacklisted_pair_sessions()
            from market_context import get_session
            _current_session = get_session()

            # Feature 2: Apply ε-greedy setup & timeframe weighting to signal_score
            for r in results:
                if r.direction == 'NEUTRAL':
                    continue
                # Determine setup type for this signal
                _setup_type = 'INSTANT'
                if SMC_MODE and (getattr(r, 'bull_ob_top', 0) > 0 or getattr(r, 'bear_ob_bot', 0) > 0):
                    _setup_type = 'SMC_OB_PULLBACK'
                elif getattr(r, 'oi_divergence', 0) != 0:
                    _setup_type = 'OI_DIVERGENCE'
                _sw = get_setup_weight(_setup_type)
                _tw = get_timeframe_weight(r.timeframe)
                r.signal_score = round(r.signal_score * _sw * _tw, 4)

            from config import OI_DIVERGENCE_CONF_THRESHOLD, OI_DIVERGENCE_MIN_SCORE

            def _get_conf_threshold(r) -> float:
                """Threshold confidence per setup type."""
                if getattr(r, 'oi_divergence', 0) != 0:
                    return OI_DIVERGENCE_CONF_THRESHOLD
                return target_conf_threshold

            def _get_min_score(r) -> float:
                """Min signal score per setup type."""
                if getattr(r, 'oi_divergence', 0) != 0:
                    return OI_DIVERGENCE_MIN_SCORE
                return MIN_SIGNAL_SCORE

            strong_signals = [
                r for r in results
                if r.direction != 'NEUTRAL'
                and r.confidence >= _get_conf_threshold(r)
                and r.signal_score >= _get_min_score(r)
                and r.win_probability >= MC_MIN_WIN_PROBABILITY
                and r.expected_return >= MC_MIN_EXPECTED_RETURN
                # Feature 3: Skip fully blacklisted pairs
                and r.symbol not in _blacklisted_pairs
                # Feature 3: Skip pair+session combos that are blacklisted
                and (r.symbol, _current_session) not in _blacklisted_sessions
            ]

        # Step 4: Sort by confidence descending
        strong_signals.sort(key=lambda x: x.confidence, reverse=True)

        # Step 5: Eksekusi & notifikasi
        open_count = self.trader.count_open_positions()
        logger.info(f"Posisi terbuka saat ini: {open_count}/{MAX_OPEN_POSITIONS}")

        for signal in strong_signals:
            now = datetime.utcnow()

            # ── Cek signal cooldown ───────────────────────────────────
            last_signal = self._signal_cooldown.get((signal.symbol, signal.timeframe))
            if last_signal and (now - last_signal) < timedelta(minutes=SIGNAL_COOLDOWN_MINUTES):
                remaining = SIGNAL_COOLDOWN_MINUTES - int((now - last_signal).total_seconds() / 60)
                logger.info(f"  ⏳ {signal.symbol} | {signal.timeframe} cooldown: {remaining}m tersisa, skip.")
                continue

            self._log_signal(signal)
            self._signal_cooldown[(signal.symbol, signal.timeframe)] = now  # Set cooldown

            # Push ke dashboard state
            import api_server as api
            api.append_signal({
                'symbol':               signal.symbol,
                'direction':            signal.direction,
                'confidence':           signal.confidence,
                'win_probability':      signal.win_probability,
                'entry_price':          signal.entry_price,
                'take_profit':          signal.take_profit,
                'stop_loss':            signal.stop_loss,
                'risk_reward':          signal.risk_reward,
                'signal_score':         signal.signal_score,
                'profitable_paths':     signal.profitable_paths,
                'tp_multiplier':        signal.tp_multiplier,
                'sl_multiplier':        signal.sl_multiplier,
                'timestamp':            now.isoformat(),
                'timeframe':            signal.timeframe,
                'oi_divergence':        getattr(signal, 'oi_divergence', 0),
                'indicator_breakdown':  getattr(signal, 'indicator_breakdown', {}) or {},
            })

            chart_path = None
            if ENABLE_VISUAL_CHECK:
                chart_path = charting_engine.generate_chart(
                    symbol=signal.symbol, timeframe=signal.timeframe,
                    entry_price=signal.entry_price, tp=signal.take_profit, sl=signal.stop_loss,
                    ob_top=getattr(signal, 'bull_ob_top', getattr(signal, 'bear_ob_top', 0)),
                    ob_bot=getattr(signal, 'bull_ob_bot', getattr(signal, 'bear_ob_bot', 0))
                )

            if AUTO_TRADE and open_count < MAX_OPEN_POSITIONS:
                # ── Cek News Blackout sebelum eksekusi trade ─────────────
                if _news_blackout_active:
                    logger.warning(
                        f"  🚫 [{signal.symbol}] Trade DITOLAK \u2014 News Blackout aktif. "
                        f"Sinyal dikirim tanpa eksekusi."
                    )
                    self.notifier.send_signal(signal, chart_path)
                    continue

                # ── Cek margin usage limit ────────────────────────────
                margin_usage_pct = self.trader.get_margin_usage_pct()
                if margin_usage_pct >= MAX_MARGIN_USAGE_PCT:
                    logger.warning(
                        f"⚠️ Batas total margin tercapai! "
                        f"Margin terpakai: {margin_usage_pct * 100:.1f}% (Batas: {MAX_MARGIN_USAGE_PCT * 100:.1f}%). "
                        f"Kirim sinyal saja."
                    )
                    self.notifier.send_signal(signal, chart_path)
                    continue

                # ── Cek trade cooldown ────────────────────────────────
                last_trade = self._trade_cooldown.get((signal.symbol, signal.timeframe))
                if last_trade and (now - last_trade) < timedelta(minutes=TRADE_COOLDOWN_MINUTES):
                    remaining = TRADE_COOLDOWN_MINUTES - int((now - last_trade).total_seconds() / 60)
                    logger.info(f"  ⏳ {signal.symbol} | {signal.timeframe} trade cooldown: {remaining}m tersisa, kirim sinyal saja.")
                    self.notifier.send_signal(signal, chart_path)
                else:
                    # Route to Pending Setup or Execute Immediately
                    use_pending = False
                    trigger_price = 0.0
                    invalidation_price = 0.0

                    if signal.symbol in self.active_trades or self.trader._has_open_position(signal.symbol):
                        logger.info(f"[{signal.symbol}] Posisi sudah terbuka di active_trades atau Binance, skip setup/trade.")
                        continue

                    if SMC_MODE and SMC_OB_RETEST_ENTRY:
                        # Pullback Entry Zone Rules
                        if signal.direction == 'LONG':
                            bull_ob_top = getattr(signal, 'bull_ob_top', 0.0)
                            if bull_ob_top > 0.0:
                                # If entry price is already inside/below the OB top boundary, execute instantly
                                if signal.entry_price <= bull_ob_top:
                                    logger.info(f"[{signal.symbol}] Price {signal.entry_price:.4f} is already within Bullish OB ({bull_ob_top:.4f}). Executing instant entry.")
                                else:
                                    use_pending = True
                                    trigger_price = bull_ob_top
                                    invalidation_price = signal.stop_loss
                        elif signal.direction == 'SHORT':
                            bear_ob_bot = getattr(signal, 'bear_ob_bot', 0.0)
                            if bear_ob_bot > 0.0:
                                # If entry price is already inside/above the OB bottom boundary, execute instantly
                                if signal.entry_price >= bear_ob_bot:
                                    logger.info(f"[{signal.symbol}] Price {signal.entry_price:.4f} is already within Bearish OB ({bear_ob_bot:.4f}). Executing instant entry.")
                                else:
                                    use_pending = True
                                    trigger_price = bear_ob_bot
                                    invalidation_price = signal.stop_loss

                    if use_pending:
                        logger.info(f"[{signal.symbol}] ⏳ Creating pending setup | Trigger: {trigger_price:.4f} | SL/Invalidation: {invalidation_price:.4f}")
                        
                        # Add to pending setups
                        self.pending_setups[signal.symbol] = {
                            'symbol':           signal.symbol,
                            'direction':        signal.direction,
                            'confidence':       signal.confidence,
                            'win_probability':  signal.win_probability,
                            'entry_price':      signal.entry_price,
                            'take_profit':      signal.take_profit,
                            'stop_loss':        signal.stop_loss,
                            'risk_reward':      signal.risk_reward,
                            'signal_score':     signal.signal_score,
                            'profitable_paths': signal.profitable_paths,
                            'tp_multiplier':    signal.tp_multiplier,
                            'sl_multiplier':    signal.sl_multiplier,
                            'timestamp':        now.isoformat(),
                            'timeframe':        signal.timeframe,
                            'trigger_price':    trigger_price,
                            'invalidation_price': invalidation_price,
                            'simulations_run':  signal.simulations_run,
                            'expected_return':  signal.expected_return,
                            'bull_ob_top':      getattr(signal, 'bull_ob_top', 0.0),
                            'bull_ob_bot':      getattr(signal, 'bull_ob_bot', 0.0),
                            'bear_ob_top':      getattr(signal, 'bear_ob_top', 0.0),
                            'bear_ob_bot':      getattr(signal, 'bear_ob_bot', 0.0)
                        }
                        self._save_pending_setups()
                        
                        self.notifier.send_pending_setup_created(signal, trigger_price, invalidation_price, chart_path)
                    else:
                        # Proceed with immediate trade execution
                        is_paused, risk_mult, cb_reason = self._circuit_breaker.check()
                        if is_paused:
                            logger.warning(f"[{signal.symbol}] Circuit Breaker aktif. Skip immediate execution.")
                            continue
                            
                        if ENABLE_CIO_AGENT:
                            # Feature 1: Multi-analyst debate (Bull vs Bear)
                            # Feature 5: Enrich context with RAG similar patterns
                            _rag_patterns = rag_memory.find_similar_patterns(
                                features=getattr(signal, 'indicator_breakdown', {}) or {},
                                top_k=5,
                            )
                            _rag_str = rag_memory.format_similar_patterns_for_context(_rag_patterns)
                            _ctx_str = (
                                f"Confidence={signal.confidence:.2f} WinProb={signal.win_probability:.2f} "
                                f"Score={signal.signal_score:.2f} RR={signal.risk_reward:.2f} "
                                f"Entry={signal.entry_price:.4f} TP={signal.take_profit:.4f} SL={signal.stop_loss:.4f}"
                            )
                            debate = gemini_client.ask_gemini_debate(
                                symbol=signal.symbol,
                                direction=signal.direction,
                                context_str=_ctx_str,
                                chart_path=chart_path,
                                similar_patterns_str=_rag_str,
                            )
                            if debate['verdict'] == 'REJECT':
                                logger.warning(
                                    f"[{signal.symbol}] CIO Debate REJECTED immediate execution. "
                                    f"Bull={debate.get('bull_strength')} Bear={debate.get('bear_strength')} | {debate['reasoning']}"
                                )
                                continue
                            _cio_verdict      = debate['verdict']
                            _cio_bull         = debate.get('bull', '')
                            _cio_bear         = debate.get('bear', '')
                        else:
                            _cio_verdict = None
                            _cio_bull    = ''
                            _cio_bear    = ''

                        risk_pct = self._get_adaptive_risk_pct(signal.symbol, risk_mult)
                        trade = self.trader.execute(signal, risk_pct=risk_pct)
                        self.notifier.send_trade_executed(signal, trade, chart_path, debate if ENABLE_CIO_AGENT else None)
                        if trade.success:
                            open_count += 1
                            self._trade_cooldown[(signal.symbol, signal.timeframe)] = now
                            
                            # Determine setup type for logging
                            _exec_setup_type = 'INSTANT'
                            if SMC_MODE and (getattr(signal, 'bull_ob_top', 0) > 0 or getattr(signal, 'bear_ob_bot', 0) > 0):
                                _exec_setup_type = 'SMC_OB_PULLBACK'
                            elif getattr(signal, 'oi_divergence', 0) != 0:
                                _exec_setup_type = 'OI_DIVERGENCE'

                            # Log trade open intelligence
                            trade_ref = self._log_trade_intelligence(signal, trade, _exec_setup_type)

                            # Feature 4: Save CIO debate details for later meta-eval
                            if _cio_verdict and trade_ref:
                                db.save_meta_feedback(
                                    trade_ref=trade_ref,
                                    meta_feedback='',
                                    cio_verdict=_cio_verdict,
                                    cio_bull_reasoning=_cio_bull[:500],
                                    cio_bear_reasoning=_cio_bear[:500],
                                )

                            # Tambah ke active_trades tracker
                            self.active_trades[trade.symbol] = {
                                'symbol':               trade.symbol,
                                'direction':            trade.direction,
                                'quantity':             trade.quantity,
                                'entry_price':          trade.entry_price,
                                'take_profit':          trade.take_profit,
                                'stop_loss':            trade.stop_loss,
                                'tp1_price':            getattr(trade, 'tp1_price', 0.0),
                                'is_partial':           getattr(trade, 'is_partial', False),
                                'status':               'OPEN',
                                'tp1_order_id':         getattr(trade, 'tp1_order_id', None),
                                'tp2_order_id':         getattr(trade, 'tp2_order_id', None),
                                'sl_order_id':          trade.sl_order_id,
                                'timestamp':            now.isoformat(),
                                'timeframe':            signal.timeframe,
                                'trade_ref':            trade_ref,
                                'indicator_breakdown':  getattr(signal, 'indicator_breakdown', {}) or {},
                                'risk_reward':          signal.risk_reward,
                                'session':              getattr(signal, 'session', ''),
                            }
                            self._save_active_trades()

                            api.append_trade({
                                'symbol':    trade.symbol,
                                'direction': trade.direction,
                                'entry':     trade.entry_price,
                                'tp':        trade.take_profit,
                                'sl':        trade.stop_loss,
                                'leverage':  trade.leverage_used,
                                'margin':    trade.margin_used,
                                'pnl':       0,
                                'timestamp': now.isoformat(),
                                'timeframe': signal.timeframe,
                            })
                        elif trade.error_msg and any(
                            code in str(trade.error_msg) for code in ['-4140', '-4141', 'Symbol is closed']
                        ):
                            self.market.add_to_blacklist(signal.symbol)
            else:
                self.notifier.send_signal(signal, chart_path)

            time.sleep(0.5)

        # Update scan state
        import api_server as api
        api.update_state('scan_count', self.scan_count)
        api.update_state('last_scan', datetime.utcnow().isoformat())

        # Update node graph
        self._update_nodes(results)

        self._log_scan_results(results, strong_signals)
        return strong_signals

    def _analyze_pair(self, symbol: str, timeframe: str = '15m') -> Optional[SimulationResult]:
        """Analisis lengkap untuk satu pair."""
        try:
            # 1. Ambil data timeframe utama
            df = self.market.get_klines(symbol, interval=timeframe)
            if df is None or len(df) < 50:
                return None

            df_with_indicators = self.indicator.compute_all(df)
            if df_with_indicators is None:
                return None

            features = self.indicator.get_signal_features(df_with_indicators)
            if features is None:
                return None

            # 2. Ambil data Higher Timeframe (HTF) untuk konfirmasi trend utama
            htf_features = None
            try:
                # 5m → HTF 15m | 15m/1h → HTF 1h | 1m/3m → HTF 15m
                if timeframe == '5m':
                    htf_interval = '15m'
                elif timeframe in ['1m', '3m']:
                    htf_interval = '15m'
                else:
                    htf_interval = '1h'
                df_htf = self.market.get_klines(symbol, interval=htf_interval, limit=50)
                if df_htf is not None and len(df_htf) >= 30:
                    df_htf_with_indicators = self.indicator.compute_all(df_htf)
                    if df_htf_with_indicators is not None:
                        htf_features = self.indicator.get_signal_features(df_htf_with_indicators)
            except Exception as e:
                logger.warning(f"[{symbol}] Gagal mengambil HTF data: {e}")

            # 3. Ambil funding rate & Open Interest change
            funding_rate = self.market.get_funding_rate(symbol)
            oi_change = 0.0
            try:
                oi_period = '5m' if timeframe in ['1m', '3m', '5m'] else '15m'
                oi_change = self.market.get_oi_change(symbol, period=oi_period)
            except Exception as e:
                logger.warning(f"[{symbol}] Gagal mengambil OI change: {e}")

            # 4. Jalankan Monte Carlo simulation dengan filter HTF dan OI change
            return self.mc_engine.run(
                symbol=symbol,
                df=df_with_indicators,
                features=features,
                timeframe=timeframe,
                funding_rate=funding_rate,
                htf_features=htf_features,
                oi_change=oi_change
            )

        except Exception as e:
            logger.error(f"Error in _analyze_pair({symbol}): {e}")
            return None


    def _log_signal(self, result: SimulationResult):
        """Log sinyal kuat ke console."""
        dir_icon = "▲" if result.direction == 'LONG' else "▼"
        logger.info(
            f"  🔥 SIGNAL | {result.symbol:12s} | {result.timeframe:3s} | {dir_icon} {result.direction:5s} | "
            f"conf={result.confidence*100:.1f}% | "
            f"win={result.win_probability*100:.1f}% | "
            f"entry={result.entry_price:.4f} | "
            f"TP={result.take_profit:.4f} | "
            f"SL={result.stop_loss:.4f}"
        )

    def _update_nodes(self, results: list):
        """
        Bangun node graph dari hasil scan:
        - Setiap pair = 1 node
        - Ukuran node = confidence
        - Warna = LONG (hijau) / SHORT (merah) / NEUTRAL (abu)
        - Edge = pasangan pair dengan arah sinyal berlawanan (divergence)
          atau searah (confluence) berdasarkan confidence tinggi
        """
        import api_server as api
        import math

        if not results:
            return

        nodes = []
        # Layout: posisikan node dalam lingkaran, pair dengan confidence
        # tinggi lebih ke tengah (radius lebih kecil)
        n = len(results)
        cx, cy = 500, 300  # center canvas

        # Sort by confidence untuk layout
        sorted_r = sorted(results, key=lambda x: x.confidence, reverse=True)

        for i, r in enumerate(sorted_r):
            angle  = (2 * math.pi * i) / n
            # Radius: confidence tinggi → lebih dekat ke tengah
            radius = 80 + (1 - r.confidence) * 220
            x = cx + radius * math.cos(angle)
            y = cy + radius * math.sin(angle)

            color = '#00ff88' if r.direction == 'LONG' else \
                    '#ff4466' if r.direction == 'SHORT' else '#444444'

            # Ukuran node: confidence 65%+ = besar, sisanya kecil
            size = 6 + r.confidence * 18

            nodes.append({
                'id':         f"{r.symbol}_{r.timeframe}",
                'label':      f"{r.symbol.replace('USDT', '')} {r.timeframe}",
                'x':          round(x, 1),
                'y':          round(y, 1),
                'size':       round(size, 1),
                'color':      color,
                'direction':  r.direction,
                'confidence': round(r.confidence, 4),
                'win_prob':   round(r.win_probability, 4),
                'score':      round(r.signal_score, 4),
                'breakdown':  getattr(r, 'indicator_breakdown', {}),
            })

        # Edge: hubungkan hanya top-15 pair TERKUAT (confidence tertinggi)
        # Batasi maks 40 edges agar graph tidak gerombol akibat gaya tarik berlebih
        MAX_STRONG = 15
        MAX_EDGES  = 40
        edges = []

        # Ambil top-15 berdasarkan confidence, exclude NEUTRAL
        strong = sorted(
            [n for n in nodes if n['direction'] != 'NEUTRAL' and n['confidence'] >= 0.60],
            key=lambda x: x['confidence'],
            reverse=True
        )[:MAX_STRONG]

        for i, a in enumerate(strong):
            if len(edges) >= MAX_EDGES:
                break
            for b in strong[i+1:]:
                if len(edges) >= MAX_EDGES:
                    break
                if a['direction'] == b['direction']:
                    # Confluence (searah) — hanya jika keduanya sangat kuat (≥65%)
                    if a['confidence'] < 0.65 or b['confidence'] < 0.65:
                        continue  # Skip confluence lemah supaya edge tidak membludak
                    edge_color = '#00ff8833' if a['direction'] == 'LONG' else '#ff446633'
                    etype = 'confluence'
                else:
                    # Divergence (berlawanan arah) — selalu tampilkan, informatif
                    edge_color = '#ffcc0022'
                    etype = 'divergence'
                edges.append({
                    'from':  a['id'],
                    'to':    b['id'],
                    'color': edge_color,
                    'type':  etype,
                })

        api.update_nodes(nodes, edges)

    def _log_scan_results(self, all_results: list, signals: list):
        """Log ringkasan hasil scan."""
        total    = len(all_results)
        longs    = sum(1 for r in all_results if r.direction == 'LONG')
        shorts   = sum(1 for r in all_results if r.direction == 'SHORT')
        neutrals = sum(1 for r in all_results if r.direction == 'NEUTRAL')

        logger.info(f"\n  📊 Scan Summary:")
        logger.info(f"     Total analyzed : {total}")
        logger.info(f"     LONG signals   : {longs}")
        logger.info(f"     SHORT signals  : {shorts}")
        logger.info(f"     NEUTRAL        : {neutrals}")
        logger.info(f"     Strong signals : {len(signals)}")

        if all_results:
            avg_conf = sum(r.confidence for r in all_results) / len(all_results)
            logger.info(f"     Avg confidence : {avg_conf*100:.1f}%")
