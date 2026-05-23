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
RISK_PER_TRADE = 0.02          # Risk per trade (2% dari balance)
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
ENABLE_PARTIAL_TP = True         # Gunakan TP1 (close 50% & move SL to BE)
PARTIAL_TP_ATR_MULTIPLIER = 0.6  # Take Profit 1 (Close 50% & move SL to breakeven)
EARLY_CLOSE_CONFIDENCE_THRESHOLD = 0.45
EARLY_CLOSE_WIN_PROB_THRESHOLD = 0.40

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
SMC_MODE = True
SMC_SWING_WINDOW = 5
SMC_MC_CONFIDENCE_THRESHOLD = 0.58
SMC_OB_RETEST_ENTRY = True
SMC_MAX_LEVERAGE_BOOST = True
EARLY_CLOSE_ON_DECAY = False

# ============================================================
# Decision Intelligence System
# ============================================================
ADAPTIVE_RISK = True
MIN_PAIR_TRADES_FOR_STATS = 10
CIRCUIT_BREAKER_ENABLED = True
CIRCUIT_BREAKER_LOSSES = 5
CIRCUIT_BREAKER_PAUSE_HOURS = 4
RISK_REDUCTION_LOSSES = 3
RISK_REDUCTION_PCT = 0.50
TRACK_MAE_MFE = True
ANALYTICS_UPDATE_INTERVAL = 600

# ============================================================
# Feature 1: News Blackout Filter
# ============================================================
NEWS_BLACKOUT_ENABLED    = True
BLACKOUT_BEFORE_MINS     = 30
BLACKOUT_AFTER_MINS      = 15
BLACKOUT_MOVE_SL_TO_BE   = True

# ============================================================
# Feature 2: Strict HTF Trend Gatekeeper
# ============================================================
HTF_STRICT_GATEKEEPER    = True
HTF_REQUIRE_BOTH_CONFIRM = False

# ============================================================
# Feature 3: Spread & Slippage Protection
# ============================================================
SPREAD_PROTECTION_ENABLED = True
MAX_SPREAD_PCT            = 0.05

# ============================================================
# Feature 4: Volatility Calibration
# ============================================================
VOLATILITY_LOOKBACK_DAYS  = 7
VOLATILITY_CALIBRATION_INTERVAL = 604800

# ============================================================
# Gemini AI Integration Settings
# ============================================================
GEMINI_API_KEY = 'YOUR_GEMINI_API_KEY'
ENABLE_CIO_AGENT = True
ENABLE_VISUAL_CHECK = True
ENABLE_TWITTER_SCRAPE = True
ENABLE_AI_RETROSPECTIVE = True
