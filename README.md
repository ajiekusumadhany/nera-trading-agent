<div align="center">

```
╔══════════════════════════════════════════════════════════════╗
║          ███╗   ██╗███████╗██████╗  █████╗                  ║
║          ████╗  ██║██╔════╝██╔══██╗██╔══██╗                 ║
║          ██╔██╗ ██║█████╗  ██████╔╝███████║                 ║
║          ██║╚██╗██║██╔══╝  ██╔══██╗██╔══██║                 ║
║          ██║ ╚████║███████╗██║  ██║██║  ██║                 ║
║          ╚═╝  ╚═══╝╚══════╝╚═╝  ╚═╝╚═╝  ╚═╝                ║
║                                                              ║
║            Q U A N T   T R A D I N G   A I   v1.0           ║
║        Monte Carlo Probability Engine • SMC Intelligence     ║
╚══════════════════════════════════════════════════════════════╝
```

[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://python.org)
[![Binance](https://img.shields.io/badge/Binance-Futures-F0B90B?style=for-the-badge&logo=binance&logoColor=black)](https://testnet.binancefuture.com)
[![Gemini AI](https://img.shields.io/badge/Gemini_2.5_Pro-CIO_Agent-4285F4?style=for-the-badge&logo=google&logoColor=white)](https://ai.google.dev)
[![Telegram](https://img.shields.io/badge/Telegram-Notifier-26A5E4?style=for-the-badge&logo=telegram&logoColor=white)](https://core.telegram.org/bots)

</div>

---

## 📋 Table of Contents

- [Overview](#-overview)
- [Architecture](#-architecture)
- [Core Engines](#-core-engines)
- [Safety Features](#-safety-features)
- [Technical Indicators](#-technical-indicators)
- [File Structure](#-file-structure)
- [Configuration](#-configuration)
- [Installation & Usage](#-installation--usage)
- [Dashboard & Notifications](#-dashboard--notifications)
- [Database Schema](#-database-schema)
- [AI Integration](#-ai-integration-gemini-25-pro)
- [Bug Fixes & Changelog](#-bug-fixes--changelog)

---

## 🌐 Overview

**NERA QUANT** adalah sistem trading algoritmik AI otonom untuk **Binance Futures**. Sistem ini secara kontinu memindai **Top 50 pair USDT perpetual** dan menggabungkan simulasi Monte Carlo probabilistik dengan **Smart Money Concepts (SMC)** untuk menghasilkan sinyal trading confidence tinggi.

Ketika sinyal memenuhi threshold multi-layer yang ketat, sistem secara otomatis mengeksekusi bracket order (Entry + TP1 + TP2 + SL) dan mengelola posisi terbuka secara real-time. Setiap trade diawasi oleh **Gemini 2.5 Pro** sebagai CIO Agent yang meninjau chart sebelum eksekusi disetujui.

---

## 🏗️ Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│                    NERA QUANT — Thread Topology                      │
├──────────────────────────────────────────────────────────────────────┤
│  Main Thread   → NeraScanner.run_forever()  (15s scan loop)         │
│  Thread 1      → api_server.start_server()  (HTTP :8000)            │
│  Thread 2      → database.run_sync_loop()   (Binance sync 10s)      │
│  Thread 3      → api_server.run_cache_updater() (balance/pos 10s)   │
│  Thread 4      → NewsBlackoutFilter._refresh_loop() (calendar 1h)   │
│  Thread 5      → _run_weekly_retrospective_loop() (AI report 7d)    │
└──────────────────────────────────────────────────────────────────────┘

Data Flow per Scan Cycle (every 15s):
  MarketData → TechnicalIndicators (+ SMC) → MonteCarloEngine
  → Signal Filtering → [Gemini CIO Debate] → BinanceTrader.execute()
  → Notifier → Database → Analytics
```

---

## 🧠 Core Engines

### 🎲 Monte Carlo Probability Engine (`monte_carlo.py`)

- **5,000 GBM paths** per pair per timeframe
- **Fat-tail shocks**: 5% chance of ±3σ event per step
- **SMC-aware TP/SL**: TP placed at opposite OB boundary, SL at OB edge + 0.5×ATR
- **FVG Gravity**: price drifts toward unfilled Fair Value Gaps
- **OB Elastic Barrier**: 75% bounce probability when price enters Order Block
- **Composite confidence**: `win_prob × 0.60 + signal_score × 0.40`, hard-capped at 0.92
- **HTF Strict Gatekeeper**: blocks 100% of counter-trend trades when 1H EMA opposes direction

### 📦 SMC Intelligence (`indicators.py`)

| Component | Detection Logic |
|---|---|
| **Swing High/Low** | window=5 candles each side |
| **BOS** (Break of Structure) | close > last swing_high (bullish) or < swing_low (bearish) |
| **CHoCH** (Change of Character) | BOS in opposite direction of prior trend → reversal signal |
| **Order Blocks** | Last opposing candle before impulse move; invalidated when price closes beyond OB |
| **Fair Value Gaps** | `low[i] > high[i-2]` (bullish FVG) or `high[i] < low[i-2]` (bearish FVG) |

### 🧩 Decision Intelligence System (`analytics_engine.py` + `database.py`)

Every trade is logged to `trade_intelligence` table with 30+ fields. After close, analytics runs every 10 minutes:

- **Pair Personality**: win rate, best session, best timeframe, adaptive risk %
- **Session Stats**: win rate per Asia/London/NY session
- **Setup Stats**: INSTANT vs SMC_OB_PULLBACK performance
- **ε-greedy Weighting**: setup and timeframe weights adjusted by historical win rate
- **Auto-Blacklist**: pairs with <35% win rate over 15+ trades are blacklisted
- **MAE/MFE Tracking**: max adverse/favorable excursion per trade
- **L3 Meta-Feedback**: Gemini evaluates whether CIO debate was correct post-trade
- **Weekly AI Retrospective**: Gemini reviews last 50 trades every 7 days (scheduled thread)

### 🧠 RAG Pattern Memory (`rag_memory.py`)

- Stores 19-feature float32 embedding per closed trade in SQLite BLOB
- Cosine similarity retrieval (min 0.80 threshold)
- Enriches Gemini CIO context with top-5 similar historical setups before each trade
- Features: RSI, BB%, EMA trend, MACD, volume, SMC signals, funding rate, OI, ATR, confidence, win_prob, R:R

---

## 🛡️ Safety Features

| Layer | Mechanism |
|---|---|
| **1. Position Sizing** | `risk_amount = balance × 2%` / `quantity = risk_amount / SL_distance`. Adaptive: scales with pair win-rate |
| **2. Safe Leverage** | `max_safe = 0.85 / sl_pct`. Final = `min(target, exchange_max, safe)` |
| **3. Margin Guard** | Block new trades if `margin_used > 75%` of wallet balance |
| **4. Position Limit** | Max 5 simultaneous open positions |
| **5. Partial TP + Breakeven** | TP1 closes 50% @ 0.6×ATR. After TP1 hit → SL moved to entry price (free trade) |
| **6. Circuit Breaker** | 3 consecutive losses → risk −50% for 2h. 5 losses → full 4h trading pause |
| **7. Spread Protection** | Reject execution if bid/ask spread > 0.05% (5bps) via `bookTicker` |
| **8. Cooldown System** | Signal cooldown: 3 min per pair. Trade cooldown: 10 min per pair |
| **9. HTF Gatekeeper** | Block 100% counter-trend signals when 1H EMA trend opposes entry |
| **10. News Blackout** | Suspend new trades 30m before + 15m after High Impact events (CPI, NFP, FOMC). Move active SL to breakeven during blackout |
| **11. Binance Auto-Sync** | Every scan: sync `active_trades.json` vs real Binance positions. Cancel orphaned algo orders |
| **12. Gemini CIO Approval** | Gemini 2.5 Pro reviews chart + RAG context before every trade. REJECT = skip trade |
| **13. Auto-Blacklist** | Pairs with <35% win rate over 15+ trades auto-blacklisted from scanning |

---

## 📊 Technical Indicators

```
╔════════════════════════════════════════════════════════════════╗
║                  INDICATOR SCORING SYSTEM                      ║
╠════════════════════════╦═══════════╦════════════════════════════╣
║  Indicator             ║  Weight   ║  Bullish Condition         ║
╠════════════════════════╬═══════════╬════════════════════════════╣
║  EMA 9/21/50 Alignment ║  2.0      ║  9 > 21 > 50               ║
║  Price vs EMA50        ║  1.5      ║  close > EMA50             ║
║  MACD Crossover        ║  2.0      ║  MACD crosses above signal ║
║  MACD Line Position    ║  1.0      ║  MACD > 0                  ║
║  RSI Signal            ║  1.5      ║  RSI < 35 (oversold)       ║
║  Stochastic Cross      ║  1.5      ║  %K crosses above %D       ║
║  RSI Divergence        ║  2.0      ║  Price LL, RSI HL          ║
║  MACD Divergence       ║  1.5      ║  Price HH, MACD LH         ║
║  Bollinger Band        ║  1.0      ║  BB% < 20%                 ║
║  Volume Spike          ║  1.5      ║  > 2× average volume       ║
╠════════════════════════╬═══════════╬════════════════════════════╣
║  SMC: BOS/CHoCH        ║  3.0 ⭐   ║  close > last swing_high   ║
║  SMC: OB Retest        ║  2.5 ⭐   ║  Price inside Bullish OB   ║
║  SMC: FVG Attraction   ║  1.5      ║  Unfilled bullish gap above║
╠════════════════════════╬═══════════╬════════════════════════════╣
║  Funding Rate Bias     ║  0.5      ║  funding < −0.05%          ║
║  Open Interest Change  ║  1.0      ║  OI > 1.5% confirms trend  ║
╠════════════════════════╩═══════════╩════════════════════════════╣
║  HTF 1H EMA Filter: BLOCKS counter-trend trades (×0.0)        ║
╚════════════════════════════════════════════════════════════════╝
```

---

## 📁 File Structure

```
nera-quant/
│
├── 🚀 ENTRY POINTS
│   ├── main.py              ← Entry point + thread orchestration
│   ├── start.sh             ← Start bot via nohup
│   ├── stop.sh              ← Stop bot gracefully
│   └── status.sh            ← Check status & last 20 log lines
│
├── 🧠 CORE ENGINE
│   ├── scanner.py           ← NeraScanner: main orchestration loop
│   ├── monte_carlo.py       ← Monte Carlo Engine + GBM simulation
│   ├── indicators.py        ← Technical indicators + SMC detection
│   ├── trader.py            ← Binance order execution engine
│   └── market_data.py       ← Market data fetching + pair filtering
│
├── 📊 INTELLIGENCE LAYER
│   ├── analytics_engine.py  ← Pair/session/setup statistics + AI retrospective
│   ├── database.py          ← SQLite ORM + trade intelligence schema
│   ├── rag_memory.py        ← RAG pattern memory (cosine similarity)
│   ├── market_context.py    ← Session detection + HTF bias voting
│   └── news_filter.py       ← Economic news blackout + X.com FUD scraper
│
├── 🤖 AI INTEGRATION
│   ├── gemini_client.py     ← Gemini 2.5 Pro (text + vision + debate + meta-eval)
│   └── charting_engine.py   ← mplfinance chart generation for CIO review
│
├── 🌐 WEB INTERFACE
│   ├── api_server.py        ← HTTP REST server (:8000)
│   └── dashboard.html       ← Live trading dashboard (single-file SPA)
│
├── ⚙️ CONFIGURATION
│   ├── config.py            ← All settings & thresholds (gitignored)
│   └── config.example.py    ← Template with placeholder values
│
├── 🗄️ DATA
│   ├── trades.db            ← SQLite database (gitignored)
│   ├── active_trades.json   ← Live position tracker (gitignored)
│   └── pending_setups.json  ← SMC pending setup tracker (gitignored)
│
└── 🧪 SCRATCH / UTILITIES
    └── scratch/             ← One-off analysis & calibration scripts
```

---

## ⚙️ Configuration

Copy `config.example.py` ke `config.py` dan isi dengan credentials kamu:

```bash
cp config.example.py config.py
nano config.py
```

Key settings:

| Setting | Default | Description |
|---|---|---|
| `AUTO_TRADE` | `True` | Enable live order execution |
| `LEVERAGE` | `20` | Target leverage (auto-capped per pair) |
| `RISK_PER_TRADE` | `0.02` | 2% risk per trade |
| `MAX_OPEN_POSITIONS` | `5` | Max simultaneous positions |
| `MC_CONFIDENCE_THRESHOLD` | `0.58` | Min confidence to trade |
| `SMC_MC_CONFIDENCE_THRESHOLD` | `0.60` | Min confidence for SMC setups |
| `MIN_SIGNAL_SCORE` | `0.60` | Min composite signal score |
| `MC_MIN_WIN_PROBABILITY` | `0.45` | Min win probability |
| `SCAN_INTERVAL_SECONDS` | `15` | Scan cycle interval |
| `SCAN_TIMEFRAMES` | `['15m', '1h']` | Timeframes to scan |
| `SMC_MODE` | `True` | Enable SMC detection |
| `HTF_STRICT_GATEKEEPER` | `True` | Block counter-trend trades |
| `ENABLE_CIO_AGENT` | `True` | Enable Gemini CIO review |
| `ENABLE_CIO_DEBATE` | `True` | Enable bull vs bear debate |
| `ENABLE_PARTIAL_TP` | `True` | Enable TP1/TP2 split |
| `CIRCUIT_BREAKER_ENABLED` | `True` | Enable circuit breaker |
| `NEWS_BLACKOUT_ENABLED` | `True` | Enable news blackout filter |
| `ADAPTIVE_RISK` | `True` | Scale risk by pair win-rate |
| `ENABLE_AI_RETROSPECTIVE` | `True` | Enable weekly AI report |

---

## 🚀 Installation & Usage

### Prerequisites

```bash
python3 --version  # 3.10+
pip install -r requirements.txt
```

### Dependencies

```
python-binance==1.0.19    # Binance API client
requests==2.31.0          # HTTP requests
numpy==1.26.4             # Numerical computation
pandas==2.2.2             # DataFrame operations
ta==0.11.0                # Technical analysis library
aiohttp==3.9.5            # Async HTTP
colorlog==6.8.2           # Colored logging
google-genai==1.16.0      # Gemini AI client
Pillow==10.4.0            # Image processing for charts
mplfinance==0.12.10b0     # Candlestick chart generation
matplotlib==3.9.2         # Chart rendering backend
ntscraper==0.3.8          # X.com/Nitter scraper for FUD detection
schedule==1.2.2           # Task scheduling
```

### Start / Stop

```bash
# Start bot (background, nohup)
./start.sh

# Check status
./status.sh

# Stop bot
./stop.sh

# View live logs
tail -f nera_quant.log
```

### Manual run (foreground)

```bash
python3 main.py
```

---

## 📡 Dashboard & Notifications

### Web Dashboard (`:8000`)

REST API endpoints:

| Endpoint | Description |
|---|---|
| `GET /` | Serve `dashboard.html` |
| `GET /api/state` | Current scan state (count, last scan, signals) |
| `GET /api/balance` | Binance wallet balance |
| `GET /api/positions` | Open positions |
| `GET /api/nodes` | Signal graph nodes & edges |
| `GET /api/pair-stats` | Pair personality stats |
| `GET /api/session-stats` | Session win rate stats |
| `GET /api/setup-stats` | Setup type win rate stats |
| `GET /api/hourly-stats` | Hourly UTC performance heatmap |
| `GET /api/intelligence/{symbol}` | Full intelligence profile for a pair |

### Telegram Notifications

| Event | Notification |
|---|---|
| Bot startup | System info + mode |
| Signal found | Confidence bar, entry/TP/SL, win prob |
| Trade executed | Order IDs, leverage, margin used + chart |
| TP1 hit | Partial close details + breakeven SL |
| Early close | Reason, estimated PnL |
| Pending setup created | Trigger price, invalidation price |
| Pending setup triggered | Full trade details |
| Pending setup invalidated | Reason |
| Circuit breaker | Reason + resume time |
| Scan summary | Every 10 scans |
| Weekly AI report | Gemini retrospective analysis |
| Error | Error message |

---

## 🗄️ Database Schema

SQLite database (`trades.db`) dengan 9 tabel:

| Table | Purpose |
|---|---|
| `trades` | Raw trade records synced from Binance income log |
| `income_log` | Full Binance income history (REALIZED_PNL, COMMISSION, etc.) |
| `sync_state` | Last sync timestamp per income type |
| `trade_intelligence` | Rich per-trade analytics (30+ fields: session, SMC signals, MAE/MFE, CIO verdict) |
| `pair_stats` | Aggregated pair personality (win rate, best session, adaptive risk) |
| `session_stats` | Win rate per session × timeframe combination |
| `setup_stats` | Win rate per setup type (INSTANT, SMC_OB_PULLBACK) |
| `auto_blacklist` | Chronically underperforming pairs/sessions |
| `pattern_embeddings` | RAG memory: 19-feature float32 embeddings for cosine similarity retrieval |

---

## 🤖 AI Integration (Gemini 2.5 Pro)

### CIO Debate (`ask_gemini_debate`)

Before every trade execution, two AI analysts debate:
- **Bull Analyst**: argues for the trade
- **Bear Analyst**: argues against
- **CIO**: renders final `APPROVE` or `REJECT` verdict

Context includes: signal metrics, chart image (if `ENABLE_VISUAL_CHECK=True`), and top-5 similar historical patterns from RAG memory.

### Visual Chart Review (`charting_engine.py`)

Generates a candlestick chart with:
- Entry price line (blue dashed)
- Take Profit line (green)
- Stop Loss line (red)
- Order Block zone (purple fill, only when OB levels exist)

Chart is sent to Gemini for visual analysis and optionally to Telegram.

### Meta-Feedback Loop (`ask_gemini_meta_eval`)

After each trade closes, Gemini evaluates whether the CIO debate verdict was correct given the actual outcome. Results stored in `trade_intelligence.meta_feedback` for continuous learning.

### Weekly AI Retrospective (`run_weekly_ai_retrospective`)

Every 7 days, Gemini reviews the last 50 closed trades and provides:
- Analysis of losing trade patterns
- Common characteristics of winning trades
- 2-3 specific parameter adjustment suggestions

Report is sent to Telegram.

---

## 🔴 Switch to Live Trading

1. Ganti `BINANCE_BASE_URL` di `config.py` ke `https://fapi.binance.com`
2. Ganti API key & secret ke credentials akun live kamu
3. Set `AUTO_TRADE = True`
4. Mulai dengan `RISK_PER_TRADE = 0.01` (1%) untuk live trading awal
5. Monitor dashboard dan Telegram notifications

> ⚠️ **DISCLAIMER**: Bot ini untuk tujuan edukasi dan penelitian. Trading futures mengandung risiko tinggi kehilangan modal. Gunakan dengan bijak dan selalu DYOR.

---

## 🐛 Bug Fixes & Changelog

### v1.1.0 (Latest)

**Bug Fixes:**

| Bug | File | Fix |
|---|---|---|
| `fill_between=None` crash di mplfinance | `charting_engine.py` | Hanya pass `fill_between` ke `mpf.plot()` ketika OB levels tersedia. Sebelumnya semua chart generation gagal. |
| `send_trade_executed()` arity mismatch | `notifier.py` | Tambah parameter `chart_path=None`. Scanner memanggil dengan 3 args tapi fungsi hanya menerima 2. |
| `execute_partial_close()` wrong args di blackout path | `scanner.py` | Fix: hapus `partial_pct=0.0` (tidak ada di signature), tambah `quantity` yang hilang. |
| RAG features selalu empty dict | `scanner.py` | Fix: `active_trade` adalah dict, bukan object. Ganti `getattr(active_trade, 'indicator_breakdown', None)` → `active_trade.get('indicator_breakdown')`. |
| `indicator_breakdown` tidak disimpan ke `active_trades` | `scanner.py` | Tambah `indicator_breakdown`, `risk_reward`, dan `session` ke kedua active_trades dict (INSTANT + SMC_OB_PULLBACK). |
| Weekly retrospective tidak pernah berjalan | `main.py` | Tambah background thread `_run_weekly_retrospective_loop()` yang berjalan setiap 7 hari. |
| Missing dependencies di `requirements.txt` | `requirements.txt` | Tambah: `google-genai`, `Pillow`, `mplfinance`, `matplotlib`, `ntscraper`. |

---

*NERA QUANT — Built with ❤️ for algorithmic trading research*
