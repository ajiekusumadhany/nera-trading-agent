# ============================================================
# NERA QUANT - Trading AI Configuration (EXAMPLE)
# ============================================================
# Copy file ini ke config.py dan isi dengan nilai asli Anda.
# JANGAN commit config.py ke repository!
# ============================================================

# Binance Testnet Credentials
API_KEY = 'YOUR_BINANCE_API_KEY'
API_SECRET = 'YOUR_BINANCE_API_SECRET'

# Telegram Bot Settings
TELEGRAM_BOT_TOKEN = 'YOUR_TELEGRAM_BOT_TOKEN'
TELEGRAM_CHAT_ID = 'YOUR_TELEGRAM_CHAT_ID'

# Binance Base URLs
BINANCE_BASE_URL = 'https://testnet.binancefuture.com'
BINANCE_WS_URL = 'wss://stream.binancefuture.com'

# ============================================================
# Monte Carlo Simulation Settings
# ============================================================
MC_SIMULATIONS = 5000          # Jumlah simulasi per pair
MC_CONFIDENCE_THRESHOLD = 0.65 # Min confidence untuk open posisi (65%)
MC_LOOKBACK_CANDLES = 100      # Candle history untuk simulasi
MC_MIN_WIN_PROBABILITY = 0.45  # Min win probability dari simulasi (45%)
MC_MIN_EXPECTED_RETURN = 0.0   # Min expected return dari simulasi (0%)
SCAN_TIMEFRAMES = ['15m', '1h'] # Timeframes to scan simultaneously

# ============================================================
# Trading Parameters
# ============================================================
TOP_PAIRS_COUNT = 50          # Analisis top N pair
SCAN_INTERVAL_SECONDS = 15     # Interval scan (detik)
TIMEFRAME = '15m'              # Timeframe utama
LEVERAGE = 20                  # Target leverage (akan di-cap ke max pair)
RISK_PER_TRADE = 0.01          # Risk per trade (1% dari balance)
MIN_VOLUME_USDT = 5_000_000    # Min 24h volume (5M USDT)

# ============================================================
# Order Execution Settings
# ============================================================
AUTO_TRADE = True              # True = langsung open posisi, False = sinyal saja
MARGIN_TYPE = 'ISOLATED'       # 'ISOLATED' atau 'CROSSED'
MAX_OPEN_POSITIONS = 5         # Maksimal posisi terbuka bersamaan
MAX_MARGIN_USAGE_PCT = 0.75    # Maksimal margin terpakai (75% dari balance)
MIN_NOTIONAL_USDT = 10.0       # Minimum order size (USDT)
TP_ATR_MULTIPLIER = 1.2        # Take Profit = entry ± (ATR × multiplier)
SL_ATR_MULTIPLIER = 0.8        # Stop Loss   = entry ∓ (ATR × multiplier)

# ============================================================
# Dynamic Risk & Partial TP Settings
# ============================================================
ENABLE_PARTIAL_TP = True         # DIAKTIFKAN: Gunakan TP1 (close 50% & move SL to BE), amankan profit awal
PARTIAL_TP_ATR_MULTIPLIER = 0.6  # Take Profit 1 (Close 50% & move SL to breakeven)
EARLY_CLOSE_CONFIDENCE_THRESHOLD = 0.40  # Close if confidence drops below this
EARLY_CLOSE_WIN_PROB_THRESHOLD = 0.40    # Close if win probability drops below this

# ============================================================
# Signal Thresholds
# ============================================================
RSI_OVERSOLD = 35
RSI_OVERBOUGHT = 65
MIN_SIGNAL_SCORE = 0.60        # Min composite score untuk sinyal

# ============================================================
# Cooldown & Dedup Settings
# ============================================================
SIGNAL_COOLDOWN_MINUTES = 3    # Pair yang sama tidak muncul lagi dalam X menit
TRADE_COOLDOWN_MINUTES  = 10   # Pair yang sudah di-trade tidak di-trade lagi dalam X menit

# ============================================================
# Notification Settings
# ============================================================
NOTIFY_ON_SIGNAL = True
NOTIFY_ON_ERROR = True
MAX_SIGNALS_PER_HOUR = 10      # Throttle notifikasi

# ============================================================
# Smart Money Concepts (SMC) & Aggressive Settings
# ============================================================
SMC_MODE = True                     # Aktifkan analisis SMC & MC Struktur
SMC_SWING_WINDOW = 5                # Window size untuk deteksi Swing High/Low
SMC_MC_CONFIDENCE_THRESHOLD = 0.65  # Threshold ditingkatkan (disamakan dengan default) agar lebih aman
SMC_OB_RETEST_ENTRY = True          # Agresif: entry langsung ketika harga retest OB
SMC_MAX_LEVERAGE_BOOST = True       # Tingkatkan leverage jika confluence SMC sangat kuat
EARLY_CLOSE_ON_DECAY = False        # DINONAKTIFKAN: Jangan keluar prematur saat MC decay

# ============================================================
# Decision Intelligence System
# ============================================================

# Adaptive Risk — sesuaikan risk per trade berdasarkan win rate historis pair
ADAPTIVE_RISK = True               # True = aktif, False = selalu pakai RISK_PER_TRADE
MIN_PAIR_TRADES_FOR_STATS = 10     # Min jumlah trade sebelum pair stats dipakai (fallback ke RISK_PER_TRADE)

# Circuit Breaker — pause / kurangi risk setelah consecutive losses
CIRCUIT_BREAKER_ENABLED = True     # Aktifkan circuit breaker
CIRCUIT_BREAKER_LOSSES = 5         # Pause trading penuh setelah N losses berturut-turut
CIRCUIT_BREAKER_PAUSE_HOURS = 4    # Durasi pause (jam)
RISK_REDUCTION_LOSSES = 3          # Kurangi risk setelah N losses berturut-turut
RISK_REDUCTION_PCT = 0.50          # Faktor pengurangan (0.50 = setengah dari risk normal)

# MAE / MFE Tracking — rekam drawdown & profit max selama trade berlangsung
TRACK_MAE_MFE = True               # Update mae/mfe di setiap scan cycle

# Analytics Engine — komputasi ulang statistik pair/session/setup
ANALYTICS_UPDATE_INTERVAL = 600    # Interval update analytics (detik, 600 = 10 menit)

# ============================================================
# Feature 1: News Blackout Filter
# ============================================================
NEWS_BLACKOUT_ENABLED    = True    # Aktifkan filter berita makro
BLACKOUT_BEFORE_MINS     = 30     # Menit sebelum berita High Impact → suspend trading baru
BLACKOUT_AFTER_MINS      = 15     # Menit setelah berita High Impact → tetap suspend
BLACKOUT_MOVE_SL_TO_BE   = True   # Pindahkan SL ke Breakeven saat blackout aktif

# ============================================================
# Feature 2: Strict HTF Trend Gatekeeper
# ============================================================
HTF_STRICT_GATEKEEPER    = True   # True = blok 100% sinyal yang berlawanan dengan 1H trend
                                   # (dahulu hanya mengurangi score, sekarang blok mutlak)
HTF_REQUIRE_BOTH_CONFIRM = False  # Jika True: butuh EMA trend DAN above_ema50 keduanya konfirmasi
                                   # Jika False: cukup EMA trend saja yang searah

# ============================================================
# Feature 3: Spread & Slippage Protection
# ============================================================
SPREAD_PROTECTION_ENABLED = True  # Aktifkan cek spread bid/ask sebelum eksekusi
MAX_SPREAD_PCT            = 0.05  # Max spread yg diizinkan (0.05% = 5 bps)

# ============================================================
# Feature 4: Volatility Calibration
# ============================================================
VOLATILITY_LOOKBACK_DAYS  = 7     # Jumlah hari historical untuk hitung sigma
VOLATILITY_CALIBRATION_INTERVAL = 604800  # Interval auto-update sigma (detik, 604800 = 1 minggu)

# ============================================================
# Gemini 2.5 Pro Integration Settings
# ============================================================
GEMINI_API_KEY = 'YOUR_GEMINI_API_KEY'
ENABLE_CIO_AGENT = True         # Gemini CIO Approval before trade
ENABLE_VISUAL_CHECK = True      # Send chart to Gemini & Telegram
ENABLE_TWITTER_SCRAPE = True    # Scrape X.com for macro news sentiment
ENABLE_AI_RETROSPECTIVE = True  # Weekly AI Evaluation


# ============================================================
# Feature A: Multi-Analyst Debate (Bull vs Bear CIO)
# ============================================================
ENABLE_CIO_DEBATE = True        # True = bull/bear debate, False = single-prompt CIO
CIO_DEBATE_EPSILON = 0.10       # ε for ε-greedy (10% chance to skip debate → APPROVE)


# ============================================================
# Feature B: ε-greedy Dynamic Setup Weighting
# ============================================================
SETUP_WEIGHT_EPSILON = 0.10     # ε for setup type weighting (10% explore)
TIMEFRAME_WEIGHT_EPSILON = 0.10 # ε for timeframe weighting


# ============================================================
# Feature C: Standing Orders / Auto-Blacklist
# ============================================================
AUTO_BLACKLIST_ENABLED = False  # Aktifkan auto-blacklist dari analytics
AUTO_BLACKLIST_MIN_TRADES = 15  # Min trades sebelum pair bisa di-blacklist
AUTO_BLACKLIST_MAX_WIN_RATE = 0.35  # Blacklist jika win rate < 35%
AUTO_BLACKLIST_SESSION_MIN_TRADES = 8
AUTO_BLACKLIST_SESSION_MAX_WIN_RATE = 0.30


# ============================================================
# Feature D: L3 Meta-Feedback
# ============================================================
ENABLE_META_FEEDBACK = True     # Aktifkan L3 meta-evaluation setelah trade close
META_FEEDBACK_BATCH = 5         # Jumlah trade yang dievaluasi per batch


# ============================================================
# Feature E: RAG Pattern Memory
# ============================================================
ENABLE_RAG_MEMORY = True        # Aktifkan RAG similarity search
RAG_TOP_K = 5                   # Jumlah similar patterns yang diambil
RAG_MIN_SIMILARITY = 0.80       # Min cosine similarity untuk dianggap relevan
