"""
notifier.py - Telegram notification untuk sinyal trading
"""

import requests
import logging
import time
from datetime import datetime
from typing import Optional
from collections import deque
from config import (
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
    NOTIFY_ON_SIGNAL, NOTIFY_ON_ERROR, MAX_SIGNALS_PER_HOUR
)
from monte_carlo import SimulationResult

logger = logging.getLogger(__name__)


class TelegramNotifier:
    """Kirim notifikasi trading signal ke Telegram."""

    BASE_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

    def __init__(self):
        self._signal_timestamps = deque()  # Throttle tracker

    def send_signal(self, result: SimulationResult, chart_path: str = None) -> bool:
        """
        Kirim notifikasi sinyal trading ke Telegram.
        Format mirip dashboard MiroFish dengan info lengkap.
        """
        if not NOTIFY_ON_SIGNAL:
            return False

        if not self._check_rate_limit():
            logger.warning("Rate limit reached, skipping notification")
            return False

        emoji_dir  = "🟢 LONG" if result.direction == 'LONG' else "🔴 SHORT"
        conf_bar   = self._confidence_bar(result.confidence)
        conf_pct   = f"{result.confidence * 100:.1f}%"
        win_pct    = f"{result.win_probability * 100:.1f}%"
        timestamp  = datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')

        # Format harga dengan presisi yang tepat
        price_fmt  = self._format_price(result.entry_price)
        tp_fmt     = self._format_price(result.take_profit)
        sl_fmt     = self._format_price(result.stop_loss)

        message = (
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⚡ *NERA QUANT SIGNAL*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 *{result.symbol}* | {emoji_dir}\n\n"
            f"🎯 *Confidence:* {conf_pct}\n"
            f"{conf_bar}\n\n"
            f"📈 *Win Probability:* {win_pct}\n"
            f"🔢 *Simulations:* {result.simulations_run:,} paths\n"
            f"✅ *Profitable Paths:* {result.profitable_paths:,}\n\n"
            f"💰 *Entry:* `{price_fmt}`\n"
            f"🎯 *Take Profit:* `{tp_fmt}`\n"
            f"🛑 *Stop Loss:* `{sl_fmt}`\n"
            f"⚖️ *Risk/Reward:* 1:{result.risk_reward:.2f}\n\n"
            f"📉 *Expected Return:* {result.expected_return:+.2f}%\n"
            f"🔍 *Signal Score:* {result.signal_score * 100:.1f}/100\n\n"
            f"🕐 `{timestamp}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⚠️ _DYOR. Bukan financial advice._"
        )

        if chart_path:
            return self._send_photo(chart_path, message)
        return self._send_message(message)

    def send_trade_executed(self, signal, trade, chart_path: str = None) -> bool:
        """
        Kirim notifikasi setelah order berhasil dieksekusi.
        Tampilkan detail order: leverage, quantity, margin, TP/SL order ID.
        """
        if not NOTIFY_ON_SIGNAL:
            return False

        if not self._check_rate_limit():
            return False

        timestamp = datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')

        if trade.success:
            emoji_dir = "🟢 LONG" if trade.direction == 'LONG' else "🔴 SHORT"
            conf_pct  = f"{signal.confidence * 100:.1f}%"
            conf_bar  = self._confidence_bar(signal.confidence)

            message = (
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"✅ *ORDER EXECUTED*\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📊 *{trade.symbol}* | {emoji_dir}\n\n"
                f"🎯 *Confidence:* {conf_pct}\n"
                f"{conf_bar}\n\n"
                f"⚙️ *Leverage:* {trade.leverage_used}x (Isolated)\n"
                f"📦 *Quantity:* `{trade.quantity}`\n"
                f"💵 *Margin Used:* `{trade.margin_used:.2f} USDT`\n\n"
                f"💰 *Entry:* `{self._format_price(trade.entry_price)}`\n"
                f"🎯 *Take Profit:* `{self._format_price(trade.take_profit)}`\n"
                f"🛑 *Stop Loss:* `{self._format_price(trade.stop_loss)}`\n"
                f"⚖️ *Risk/Reward:* 1:{signal.risk_reward:.2f}\n\n"
                f"🔖 *Order ID:* `{trade.order_id}`\n"
                f"🔖 *TP Order:* `{trade.tp_order_id}`\n"
                f"🔖 *SL Order:* `{trade.sl_order_id}`\n\n"
                f"🕐 `{timestamp}`\n"
                f"━━━━━━━━━━━━━━━━━━━━━━"
            )
            if chart_path:
                return self._send_photo(chart_path, message)
            return self._send_message(message)
        else:
            # Order gagal — log saja, tidak perlu notif Telegram
            logger.warning(f"Order failed [{trade.symbol}]: {trade.error_msg}")
            return False

    def send_scan_summary(self, total_pairs: int, signals_found: int, top_signals: list) -> bool:
        """Kirim ringkasan hasil scan ke Telegram."""
        timestamp = datetime.utcnow().strftime('%H:%M UTC')

        lines = [
            f"🔍 *SCAN SELESAI* | {timestamp}",
            f"━━━━━━━━━━━━━━━━━━━━━━",
            f"📊 Pairs dianalisis: *{total_pairs}*",
            f"⚡ Sinyal ditemukan: *{signals_found}*",
        ]

        if top_signals:
            lines.append("\n🏆 *Top Signals:*")
            for i, sig in enumerate(top_signals[:5], 1):
                dir_emoji = "🟢" if sig.direction == 'LONG' else "🔴"
                lines.append(
                    f"{i}. {dir_emoji} *{sig.symbol}* — "
                    f"conf: {sig.confidence*100:.1f}% | "
                    f"win: {sig.win_probability*100:.1f}%"
                )

        if not top_signals:
            lines.append("\n😴 Tidak ada sinyal kuat saat ini.")

        return self._send_message('\n'.join(lines))

    def send_error(self, error_msg: str) -> bool:
        """Kirim notifikasi error."""
        if not NOTIFY_ON_ERROR:
            return False
        message = f"⚠️ *NERA QUANT ERROR*\n\n`{error_msg}`"
        return self._send_message(message)

    def send_startup(self) -> bool:
        """Kirim notifikasi bot startup."""
        from config import AUTO_TRADE, LEVERAGE, MARGIN_TYPE, MAX_OPEN_POSITIONS
        trade_mode = "🤖 AUTO TRADE ON" if AUTO_TRADE else "👁 Signal Only Mode"
        message = (
            f"🚀 *NERA QUANT STARTED*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"✅ Bot aktif dan scanning...\n"
            f"📊 Monitoring top 50 Binance pairs\n"
            f"🎲 Monte Carlo: 5,000 simulations/pair\n"
            f"⏱ Scan interval: 60 detik\n"
            f"⚙️ Mode: *{trade_mode}*\n"
            f"📐 Leverage: up to *{LEVERAGE}x* (auto-cap per pair)\n"
            f"🔒 Margin: *{MARGIN_TYPE}*\n"
            f"📂 Max posisi: *{MAX_OPEN_POSITIONS}*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━"
        )
        return self._send_message(message)

    def send_partial_tp_executed(
        self, symbol: str, direction: str, qty: float, price: float, remaining_qty: float
    ) -> bool:
        """Kirim notifikasi Partial Take Profit & Breakeven ke Telegram."""
        timestamp = datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')
        emoji_dir = "🟢 LONG" if direction == 'LONG' else "🔴 SHORT"
        price_fmt = self._format_price(price)

        message = (
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"💥 *PARTIAL TAKE PROFIT (TP1)*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 *{symbol}* | {emoji_dir}\n\n"
            f"✅ *Status:* TP1 Hit! 50% Posisi Ditutup.\n"
            f"💵 *Harga Exit TP1:* `{price_fmt}`\n"
            f"📦 *Qty Terjual:* `{qty}`\n"
            f"📦 *Sisa Qty:* `{remaining_qty}`\n\n"
            f"🔒 *Stop Loss baru:* Disesuaikan ke *ENTRY (Breakeven)*! (Free Trade)\n\n"
            f"🕐 `{timestamp}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━"
        )
        return self._send_message(message)

    def send_early_close_executed(
        self, symbol: str, direction: str, price: float, pnl: float, confidence: float, win_prob: float
    ) -> bool:
        """Kirim notifikasi Early Close karena penurunan probabilitas."""
        timestamp = datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')
        emoji_dir = "🟢 LONG" if direction == 'LONG' else "🔴 SHORT"
        price_fmt = self._format_price(price)
        pnl_emoji = "🟢" if pnl >= 0 else "🔴"
        conf_bar = self._confidence_bar(confidence)

        message = (
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⚠️ *DYNAMIC EARLY EXIT*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 *{symbol}* | {emoji_dir}\n\n"
            f"🛑 *Status:* Ditutup Lebih Awal (Early Close)\n"
            f"🎯 *Confidence Terkini:* `{confidence*100:.1f}%`\n"
            f"{conf_bar}\n"
            f"📈 *Win Probability:* `{win_prob*100:.1f}%`\n\n"
            f"💵 *Harga Exit:* `{price_fmt}`\n"
            f"{pnl_emoji} *Estimasi PnL:* `{pnl:+.2f}%`\n\n"
            f"🔍 *Alasan:* Monte Carlo mendeteksi setup trade melemah di bawah batas aman. Posisi dilikuidasi demi keamanan modal.\n\n"
            f"🕐 `{timestamp}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━"
        )
        return self._send_message(message)

    def send_pending_setup_created(self, signal, trigger_price: float, invalidation_price: float, chart_path: str = None) -> bool:
        """Kirim notifikasi bahwa Setup Pending SMC berhasil dipasang."""
        if not NOTIFY_ON_SIGNAL:
            return False
        timestamp = datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')
        emoji_dir = "🟢 PENDING LONG" if signal.direction == 'LONG' else "🔴 PENDING SHORT"
        conf_pct  = f"{signal.confidence * 100:.1f}%"
        conf_bar  = self._confidence_bar(signal.confidence)

        message = (
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⏳ *SMC PENDING SETUP PLACED*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 *{signal.symbol}* | {emoji_dir}\n\n"
            f"🎯 *Confidence:* {conf_pct}\n"
            f"{conf_bar}\n\n"
            f"📥 *Entry Trigger:* `{self._format_price(trigger_price)}` (Pullback OB)\n"
            f"🛑 *Batas Invalidation:* `{self._format_price(invalidation_price)}` (OB Break)\n"
            f"🎯 *Target Profit (TP):* `{self._format_price(signal.take_profit)}`\n"
            f"⚖️ *Risk/Reward:* 1:{signal.risk_reward:.2f}\n\n"
            f"🔍 *Alasan:* Sinyal SMC sangat kuat, bot menunggu harga retrace/diskon ke zona Order Block untuk efisiensi R/R.\n\n"
            f"🕐 `{timestamp}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━"
        )
        if chart_path:
            return self._send_photo(chart_path, message)
        return self._send_message(message)

    def send_pending_setup_triggered(self, signal, trade, chart_path: str = None) -> bool:
        """Kirim notifikasi bahwa Setup Pending berhasil terpicu & terisi."""
        if not NOTIFY_ON_SIGNAL:
            return False
        timestamp = datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')
        if trade.success:
            emoji_dir = "🟢 LONG (OB Retest)" if trade.direction == 'LONG' else "🔴 SHORT (OB Retest)"
            conf_pct  = f"{signal.confidence * 100:.1f}%"
            conf_bar  = self._confidence_bar(signal.confidence)

            message = (
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🔥 *SMC PENDING TRIGGERED & FILLED*\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📊 *{trade.symbol}* | {emoji_dir}\n\n"
                f"🎯 *Confidence:* {conf_pct}\n"
                f"{conf_bar}\n\n"
                f"⚙️ *Leverage:* {trade.leverage_used}x (Isolated)\n"
                f"📦 *Quantity:* `{trade.quantity}`\n"
                f"💵 *Margin Used:* `{trade.margin_used:.2f} USDT`\n\n"
                f"💰 *Trigger Entry Price:* `{self._format_price(trade.entry_price)}`\n"
                f"🎯 *Take Profit:* `{self._format_price(trade.take_profit)}`\n"
                f"🛑 *Stop Loss:* `{self._format_price(trade.stop_loss)}`\n"
                f"⚖️ *Risk/Reward:* 1:{signal.risk_reward:.2f}\n\n"
                f"🔖 *Order ID:* `{trade.order_id}`\n"
                f"🔖 *TP Order:* `{trade.tp_order_id}`\n"
                f"🔖 *SL Order:* `{trade.sl_order_id}`\n\n"
                f"🕐 `{timestamp}`\n"
                f"━━━━━━━━━━━━━━━━━━━━━━"
            )
            if chart_path:
                return self._send_photo(chart_path, message)
            return self._send_message(message)
        else:
            logger.warning(f"Pending trigger trade failed [{trade.symbol}]: {trade.error_msg}")
            return False

    def send_pending_setup_invalidated(self, symbol: str, direction: str, price: float, reason: str) -> bool:
        """Kirim notifikasi bahwa Setup Pending dibatalkan (Invalidated)."""
        if not NOTIFY_ON_SIGNAL:
            return False
        timestamp = datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')
        emoji_dir = "🟢 LONG" if direction == 'LONG' else "🔴 SHORT"
        price_fmt = self._format_price(price)

        message = (
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"❌ *SMC SETUP INVALIDATED*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 *{symbol}* | {emoji_dir}\n\n"
            f"⚠️ *Status:* DIBATALKAN (Setup Invalidated)\n"
            f"💵 *Harga Saat Ini:* `{price_fmt}`\n\n"
            f"🔍 *Alasan:* {reason}\n\n"
            f"🕐 `{timestamp}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━"
        )
        return self._send_message(message)

    def send_circuit_breaker_alert(self, reason: str, resume_at: str) -> bool:
        """Kirim alert circuit breaker ke Telegram."""
        timestamp = datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')
        message = (
            f"🚨 *PSYCHOLOGICAL CIRCUIT BREAKER*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⚠️ *Trading Paused!*\n\n"
            f"🔍 *Alasan:* {reason}\n"
            f"🕐 *Estimasi Resume:* `{resume_at}`\n\n"
            f"Sistem menghentikan pembukaan posisi baru demi melindungi modal psikologis & finansial Anda. Posisi yang ada tetap akan dipantau & dikelola."
        )
        return self._send_message(message)

    def send_weekly_performance_report(self, stats: dict) -> bool:
        """Kirim laporan mingguan trading intelligence."""
        timestamp = datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')
        win_rate = stats.get('win_rate', 0.0)
        total_pnl = stats.get('total_pnl', 0.0)
        total_trades = stats.get('total_trades', 0)
        best_pair = stats.get('best_pair', 'N/A')
        best_session = stats.get('best_session', 'N/A')
        pnl_emoji = "🟢" if total_pnl >= 0 else "🔴"

        message = (
            f"📊 *NERA QUANT WEEKLY REPORT*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📅 *Timestamp:* `{timestamp}`\n\n"
            f"💼 *Total Trades:* `{total_trades}`\n"
            f"📈 *Win Rate:* `{win_rate:.2f}%`\n"
            f"{pnl_emoji} *Total PnL:* `{total_pnl:+.4f} USDT`\n\n"
            f"🏆 *Best Pair:* `{best_pair}`\n"
            f"🕒 *Best Session:* `{best_session}`\n\n"
            f"Sistem otomatis memperbarui model personality untuk mengoptimalkan dynamic risk. Terus pantau dashboard untuk wawasan mendalam!"
        )
        return self._send_message(message)

    def send_pair_insight(self, symbol: str, personality: dict) -> bool:
        """Kirim alert jika pair personality berubah signifikan."""
        timestamp = datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')
        win_rate = personality.get('win_rate', 0.0)
        best_session = personality.get('best_session', 'N/A')
        avoid_session = personality.get('avoid_session', 'N/A')
        rec_risk = personality.get('recommended_risk_pct', 0.02) * 100

        message = (
            f"💡 *PAIR INTELLIGENCE INSIGHT*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 *Pair:* *{symbol}*\n\n"
            f"📈 *Win Rate:* `{win_rate*100:.1f}%` ({personality.get('total_trades')} trades)\n"
            f"🕒 *Best Session:* `{best_session}`\n"
            f"❌ *Avoid Session:* `{avoid_session}`\n"
            f"🛡️ *Recommended Risk:* `{rec_risk:.2f}%` per trade\n\n"
            f"Sistem Decision Intelligence telah mendeteksi pergeseran karakteristik pair. Parameter risk engine otomatis disesuaikan."
        )
        return self._send_message(message)

    # ── Private methods ───────────────────────────────────────────────

    def _send_photo(self, photo_path: str, caption: str) -> bool:
        """Kirim foto beserta caption ke Telegram."""
        import os
        if not os.path.exists(photo_path):
            return self._send_message(caption)
            
        url = f"{self.BASE_URL}/sendPhoto"
        data = {
            'chat_id': TELEGRAM_CHAT_ID,
            'caption': caption,
            'parse_mode': 'Markdown',
        }
        for attempt in range(3):
            try:
                with open(photo_path, 'rb') as photo:
                    files = {'photo': photo}
                    resp = requests.post(url, data=data, files=files, timeout=15)
                if resp.status_code == 200:
                    self._signal_timestamps.append(time.time())
                    return True
                else:
                    logger.warning(f"Telegram sendPhoto API error {resp.status_code}: {resp.text}")
            except requests.RequestException as e:
                logger.error(f"Telegram sendPhoto attempt {attempt+1} failed: {e}")
                if attempt < 2:
                    time.sleep(2)
        return False

    def _send_message(self, text: str) -> bool:
        """Kirim pesan ke Telegram dengan retry."""
        url = f"{self.BASE_URL}/sendMessage"
        payload = {
            'chat_id':    TELEGRAM_CHAT_ID,
            'text':       text,
            'parse_mode': 'Markdown',
        }

        for attempt in range(3):
            try:
                resp = requests.post(url, json=payload, timeout=10)
                if resp.status_code == 200:
                    self._signal_timestamps.append(time.time())
                    return True
                else:
                    logger.warning(f"Telegram API error {resp.status_code}: {resp.text}")
            except requests.RequestException as e:
                logger.error(f"Telegram send attempt {attempt+1} failed: {e}")
                if attempt < 2:
                    time.sleep(2)

        return False

    def _check_rate_limit(self) -> bool:
        """Cek apakah masih dalam batas MAX_SIGNALS_PER_HOUR."""
        now = time.time()
        one_hour_ago = now - 3600

        # Hapus timestamps yang sudah lebih dari 1 jam
        while self._signal_timestamps and self._signal_timestamps[0] < one_hour_ago:
            self._signal_timestamps.popleft()

        return len(self._signal_timestamps) < MAX_SIGNALS_PER_HOUR

    def _confidence_bar(self, confidence: float) -> str:
        """Buat visual bar untuk confidence level."""
        filled = int(confidence * 10)
        empty  = 10 - filled
        bar    = '█' * filled + '░' * empty
        return f"`[{bar}]`"

    def _format_price(self, price: float) -> str:
        """Format harga dengan presisi yang sesuai."""
        if price >= 1000:
            return f"{price:,.2f}"
        elif price >= 1:
            return f"{price:.4f}"
        else:
            return f"{price:.6f}"
