"""
database.py - SQLite persistence untuk trade history NERA QUANT
Menyimpan semua posisi yang sudah close dengan data lengkap dari Binance.
"""

import sqlite3
import hmac
import hashlib
import time
import logging
import threading
import requests
from urllib.parse import urlencode
from typing import List, Dict, Optional
from config import API_KEY, API_SECRET, BINANCE_BASE_URL

logger = logging.getLogger(__name__)

DB_PATH = '/home/ajiekusumadhany.me/public_html/nera-quant/trades.db'

# Hanya catat PnL dari tanggal ini ke depan (2026-05-23 04:00 WIB = 2026-05-22 21:00:00 UTC)
SYNC_START_TS = 1779569100000
_db_lock = threading.Lock()


# ── Schema ────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol          TEXT    NOT NULL,
    side            TEXT    NOT NULL,          -- LONG / SHORT
    qty             REAL    NOT NULL,
    entry_price     REAL    NOT NULL,
    exit_price      REAL,
    open_time       INTEGER NOT NULL,          -- ms timestamp
    close_time      INTEGER,                   -- ms timestamp, NULL jika masih open
    realized_pnl    REAL    DEFAULT 0.0,
    commission      REAL    DEFAULT 0.0,
    net_pnl         REAL    DEFAULT 0.0,       -- realized_pnl - commission
    leverage        INTEGER DEFAULT 1,
    margin_used     REAL    DEFAULT 0.0,
    status          TEXT    DEFAULT 'OPEN',    -- OPEN / CLOSED / PARTIAL
    binance_order_id TEXT,                     -- orderId dari market order awal
    UNIQUE(symbol, open_time, side)            -- cegah duplikat posisi
);

CREATE TABLE IF NOT EXISTS income_log (
    trade_id        TEXT    PRIMARY KEY,       -- tradeId dari Binance income
    symbol          TEXT    NOT NULL,
    income_type     TEXT    NOT NULL,          -- REALIZED_PNL / COMMISSION / dll
    income          REAL    NOT NULL,
    timestamp       INTEGER NOT NULL,
    asset           TEXT    DEFAULT 'USDT'
);

CREATE TABLE IF NOT EXISTS sync_state (
    key             TEXT    PRIMARY KEY,
    value           TEXT    NOT NULL
);

-- ── Decision Intelligence System Tables ──────────────────────────────

CREATE TABLE IF NOT EXISTS trade_intelligence (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_ref                   TEXT    UNIQUE,   -- {symbol}_{entry_time_utc} unique ref
    binance_order_id            TEXT,             -- link ke trades.binance_order_id
    symbol                      TEXT    NOT NULL,
    direction                   TEXT    NOT NULL, -- LONG / SHORT
    entry_time_utc              TEXT    NOT NULL, -- ISO8601
    close_time_utc              TEXT,             -- diisi saat close
    timeframe                   TEXT    NOT NULL, -- 5m / 15m / 1h
    session                     TEXT,             -- ASIA / LONDON / NY / OFF
    entry_hour_utc              INTEGER,          -- 0-23
    entry_weekday               INTEGER,          -- 0=Mon … 6=Sun
    setup_type                  TEXT,             -- SMC_OB_PULLBACK / INSTANT / OI_DIVERGENCE / PENDING_TRIGGER
    smc_signals                 TEXT,             -- JSON: {bos, choch, fvg_dir, bull_ob_top, ...}
    mc_confidence               REAL,
    mc_win_prob                 REAL,
    signal_score                REAL,
    risk_reward                 REAL,
    atr                         REAL,
    atr_pct                     REAL,
    funding_rate                REAL,
    oi_change                   REAL,
    htf_bias                    TEXT,             -- BULLISH / BEARISH / NEUTRAL
    bb_pct                      REAL,
    rsi                         REAL,
    macd_cross                  INTEGER,          -- 1 / 0
    vol_spike                   INTEGER,          -- 1 / 0
    entry_price                 REAL,
    take_profit                 REAL,
    stop_loss                   REAL,
    exit_price                  REAL,
    result_pnl                  REAL,
    result_rr_achieved          REAL,
    outcome                     TEXT    DEFAULT 'OPEN', -- WIN / LOSS / BE / OPEN
    trade_duration_mins         INTEGER,
    mae                         REAL    DEFAULT 0.0,  -- Max Adverse Excursion
    mfe                         REAL    DEFAULT 0.0,  -- Max Favorable Excursion
    leverage                    INTEGER,
    margin_used                 REAL,
    pending_duration_mins       INTEGER DEFAULT 0,    -- 0 = instant entry
    consecutive_losses_at_entry INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS pair_stats (
    symbol              TEXT    PRIMARY KEY,
    total_trades        INTEGER DEFAULT 0,
    win_trades          INTEGER DEFAULT 0,
    loss_trades         INTEGER DEFAULT 0,
    win_rate            REAL    DEFAULT 0.0,
    avg_rr              REAL    DEFAULT 0.0,
    avg_rr_achieved     REAL    DEFAULT 0.0,
    avg_duration_mins   REAL    DEFAULT 0.0,
    best_session        TEXT,
    best_timeframe      TEXT,
    avg_atr_pct         REAL    DEFAULT 0.0,
    recommended_risk_pct REAL   DEFAULT 0.02,
    last_updated        TEXT
);

CREATE TABLE IF NOT EXISTS session_stats (
    session         TEXT    NOT NULL,
    timeframe       TEXT    NOT NULL,
    total_trades    INTEGER DEFAULT 0,
    win_trades      INTEGER DEFAULT 0,
    win_rate        REAL    DEFAULT 0.0,
    avg_rr          REAL    DEFAULT 0.0,
    last_updated    TEXT,
    PRIMARY KEY (session, timeframe)
);

CREATE TABLE IF NOT EXISTS setup_stats (
    setup_type      TEXT    PRIMARY KEY,
    total_trades    INTEGER DEFAULT 0,
    win_trades      INTEGER DEFAULT 0,
    win_rate        REAL    DEFAULT 0.0,
    avg_rr          REAL    DEFAULT 0.0,
    last_updated    TEXT
);

-- ── Feature 3: Standing Orders / Auto-Blacklist ───────────────────────
CREATE TABLE IF NOT EXISTS auto_blacklist (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol          TEXT    NOT NULL,
    reason          TEXT    NOT NULL,   -- e.g. "win_rate=0.20 over 15 trades"
    blacklist_type  TEXT    NOT NULL,   -- 'PAIR' | 'PAIR_SESSION'
    session         TEXT    DEFAULT '',  -- '' for full pair blacklist
    win_rate        REAL,
    total_trades    INTEGER,
    created_at      TEXT    NOT NULL,
    expires_at      TEXT,               -- NULL = permanent until manually cleared
    active          INTEGER DEFAULT 1,  -- 1=active, 0=cleared
    UNIQUE (symbol, blacklist_type, session)
);

-- ── OI vs Price Change Stats ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS oi_price_stats (
    oi_bucket       TEXT    NOT NULL,   -- e.g. 'STRONG_RISE', 'RISE', 'FLAT', 'DROP', 'STRONG_DROP'
    price_direction TEXT    NOT NULL,   -- 'LONG' | 'SHORT'
    total_trades    INTEGER DEFAULT 0,
    win_trades      INTEGER DEFAULT 0,
    win_rate        REAL    DEFAULT 0.0,
    avg_rr          REAL    DEFAULT 0.0,
    avg_mc_win_prob REAL    DEFAULT 0.0,  -- rata-rata mc_win_prob saat entry
    avg_oi_change   REAL    DEFAULT 0.0,  -- rata-rata oi_change aktual
    last_updated    TEXT,
    PRIMARY KEY (oi_bucket, price_direction)
);

-- ── Feature 4: L3 Meta-Feedback column (added via ALTER if missing) ───
-- meta_feedback TEXT added to trade_intelligence via migration below
"""


# ── DB Connection ─────────────────────────────────────────────────────

def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Buat tabel jika belum ada."""
    with _db_lock:
        conn = get_conn()
        conn.executescript(SCHEMA)
        conn.commit()

        # ── Migration: add meta_feedback column if not exists ──────────
        try:
            conn.execute("ALTER TABLE trade_intelligence ADD COLUMN meta_feedback TEXT")
            conn.commit()
            logger.info("[DB] Migration: added meta_feedback column to trade_intelligence")
        except Exception:
            pass  # Column already exists

        # ── Migration: add cio_bull / cio_bear columns ─────────────────
        for col in ('cio_bull_reasoning TEXT', 'cio_bear_reasoning TEXT', 'cio_verdict TEXT'):
            try:
                conn.execute(f"ALTER TABLE trade_intelligence ADD COLUMN {col}")
                conn.commit()
            except Exception:
                pass

        # ── Migration: create oi_price_stats if not exists ─────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS oi_price_stats (
                oi_bucket       TEXT    NOT NULL,
                price_direction TEXT    NOT NULL,
                total_trades    INTEGER DEFAULT 0,
                win_trades      INTEGER DEFAULT 0,
                win_rate        REAL    DEFAULT 0.0,
                avg_rr          REAL    DEFAULT 0.0,
                avg_mc_win_prob REAL    DEFAULT 0.0,
                avg_oi_change   REAL    DEFAULT 0.0,
                last_updated    TEXT,
                PRIMARY KEY (oi_bucket, price_direction)
            )
        """)
        conn.commit()

        conn.close()

    # Init RAG schema
    try:
        import rag_memory
        rag_memory.init_rag_schema()
    except Exception as e:
        logger.warning(f"[DB] RAG schema init warning: {e}")

    logger.info(f"[DB] Initialized: {DB_PATH}")


# ── Binance API helper ────────────────────────────────────────────────

def _signed_get(path: str, params: dict = None) -> any:
    params = params or {}
    params['timestamp'] = int(time.time() * 1000)
    params['recvWindow'] = 60000
    query = urlencode(params)
    sig = hmac.new(API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
    url = f"{BINANCE_BASE_URL}{path}?{query}&signature={sig}"
    resp = requests.get(url, headers={'X-MBX-APIKEY': API_KEY}, timeout=15)
    resp.raise_for_status()
    return resp.json()


# ── Sync State (last fetched timestamp) ──────────────────────────────

def _get_sync_time(key: str) -> int:
    """Ambil timestamp terakhir sync dari DB."""
    with _db_lock:
        conn = get_conn()
        row = conn.execute(
            "SELECT value FROM sync_state WHERE key = ?", (key,)
        ).fetchone()
        conn.close()
    # Default: mulai dari waktu saat ini agar tidak mengambil history lama
    return int(row['value']) if row else int(time.time() * 1000)


def _set_sync_time(key: str, ts: int):
    """Simpan timestamp terakhir sync ke DB."""
    with _db_lock:
        conn = get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO sync_state (key, value) VALUES (?, ?)",
            (key, str(ts))
        )
        conn.commit()
        conn.close()


# ── Income Log Sync ───────────────────────────────────────────────────

def sync_income_log() -> int:
    """
    Fetch semua REALIZED_PNL + COMMISSION dari Binance income history.
    Simpan ke income_log, deduplikasi by trade_id.
    Returns jumlah record baru yang disimpan.
    """
    total_new = 0
    income_types = ['REALIZED_PNL', 'COMMISSION']

    for income_type in income_types:
        sync_key = f'income_last_time_{income_type}'
        # Guard: jangan pernah ambil data sebelum SYNC_START_TS
        last_time = _get_sync_time(sync_key)
        
        # Fallback ke key lama jika key spesifik tipe belum ada
        if last_time == int(time.time() * 1000) or last_time == 0:
            legacy_time = _get_sync_time('income_last_time')
            if legacy_time > 0:
                last_time = legacy_time
            else:
                # Default 24 jam ke belakang
                last_time = int(time.time() * 1000) - 24 * 3600 * 1000

        # Look back 12 jam untuk menyembuhkan gap, tapi tidak boleh sebelum SYNC_START_TS
        fetch_start = max(SYNC_START_TS, last_time - 12 * 3600 * 1000)
        latest_fetched_time = last_time

        while True:
            params = {
                'incomeType': income_type,
                'limit':      1000,
            }
            if fetch_start:
                params['startTime'] = fetch_start

            try:
                params['recvWindow'] = 60000
                data = _signed_get('/fapi/v1/income', params)
            except Exception as e:
                logger.error(f"[DB] Income fetch error ({income_type}): {e}")
                break

            if not isinstance(data, list) or not data:
                _set_sync_time(sync_key, max(latest_fetched_time, int(time.time() * 1000)))
                break

            rows = []
            for item in data:
                item_time = int(item.get('time', 0))
                if item_time < SYNC_START_TS:
                    continue

                base_trade_id = str(item.get('tradeId', f"{item['symbol']}_{item['time']}"))
                db_trade_id = f"{base_trade_id}_{income_type}"
                
                rows.append((
                    db_trade_id,
                    item.get('symbol', ''),
                    income_type,
                    float(item.get('income', 0)),
                    item_time,
                    item.get('asset', 'USDT'),
                ))
                if item_time > latest_fetched_time:
                    latest_fetched_time = item_time

            if rows:
                with _db_lock:
                    conn = get_conn()
                    conn.executemany(
                        """INSERT OR IGNORE INTO income_log
                           (trade_id, symbol, income_type, income, timestamp, asset)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        rows
                    )
                    inserted = conn.total_changes
                    conn.commit()
                    conn.close()
                total_new += inserted

            if len(data) < 1000:
                _set_sync_time(sync_key, max(latest_fetched_time, int(time.time() * 1000)))
                break
            fetch_start = int(data[-1]['time']) + 1

    if total_new > 0:
        logger.info(f"[DB] Income sync: +{total_new} records baru")

    return total_new


# ── Trade Position Sync ───────────────────────────────────────────────

def sync_closed_trades() -> int:
    """
    Rekonstruksi posisi closed dari income_log.
    Cocokkan REALIZED_PNL + COMMISSION per symbol per waktu close.
    Simpan ke tabel trades.
    Returns jumlah posisi baru yang disimpan.
    """
    with _db_lock:
        conn = get_conn()

        # Ambil semua REALIZED_PNL sejak SYNC_START_TS yang belum ada di trades
        rows = conn.execute("""
            SELECT il.symbol, il.income AS pnl, il.timestamp AS close_time, il.trade_id
            FROM income_log il
            WHERE il.income_type = 'REALIZED_PNL'
              AND il.income != 0
              AND il.timestamp >= ?
            ORDER BY il.timestamp ASC
        """, (SYNC_START_TS,)).fetchall()

        existing_trade_ids = set(
            r[0] for r in conn.execute(
                "SELECT binance_order_id FROM trades WHERE binance_order_id IS NOT NULL"
            ).fetchall()
        )
        conn.close()

    if not rows:
        return 0

    # Fetch user trades dari Binance untuk dapat entry/exit price
    # Group by symbol untuk efisiensi
    symbols = list({r['symbol'] for r in rows if r['symbol']})
    user_trades_map: Dict[str, List] = {}

    # Dapatkan startTime dinamis: 5 menit sebelum close_time unsynced paling awal
    min_close_time = min(int(r['close_time']) for r in rows)
    start_time_param = max(min_close_time - 300000, int(time.time() * 1000) - 7 * 24 * 3600 * 1000)

    for symbol in symbols:
        try:
            params = {
                'symbol':    symbol,
                'limit':     1000,
                'startTime': start_time_param
            }
            data = _signed_get('/fapi/v1/userTrades', params)
            if isinstance(data, list):
                user_trades_map[symbol] = data
        except Exception as e:
            logger.warning(f"[DB] userTrades fetch error {symbol}: {e}")
            user_trades_map[symbol] = []

    # Rekonstruksi posisi: match income record dengan user trade yang sesuai
    new_count = 0
    with _db_lock:
        conn = get_conn()

        for row in rows:
            trade_id = str(row['trade_id'])
            # Dapatkan raw trade_id numerik jika menggunakan format bersuffix
            actual_trade_id = trade_id.split('_')[0]

            if actual_trade_id in existing_trade_ids or trade_id in existing_trade_ids:
                continue

            symbol     = row['symbol']
            pnl        = float(row['pnl'])
            close_time = int(row['close_time'])

            # Cari commission untuk trade ini (coba via ID langsung dulu, fallback ke timestamp)
            commission = 0.0
            commission_row = conn.execute("""
                SELECT income FROM income_log
                WHERE trade_id = ?
            """, (f"{actual_trade_id}_COMMISSION",)).fetchone()

            if commission_row:
                commission = abs(float(commission_row['income']))
            else:
                commission = conn.execute("""
                    SELECT COALESCE(SUM(ABS(income)), 0) as fee
                    FROM income_log
                    WHERE symbol = ? AND income_type = 'COMMISSION'
                      AND timestamp BETWEEN ? AND ?
                """, (symbol, close_time - 1000, close_time + 1000)).fetchone()['fee']

            net_pnl = pnl - abs(commission)

            # Cari user trade yang cocok
            ut_list = user_trades_map.get(symbol, [])
            closest = None

            # 1. Cari exact match menggunakan trade ID (id)
            for ut in ut_list:
                if str(ut.get('id')) == actual_trade_id:
                    closest = ut
                    break

            # 2. Fallback ke fuzzy match by timestamp jika exact match tidak ditemukan
            if not closest:
                min_diff = float('inf')
                for ut in ut_list:
                    diff = abs(int(ut.get('time', 0)) - close_time)
                    if diff < min_diff:
                        min_diff = diff
                        closest = ut
                if closest and min_diff >= 60000:
                    closest = None

            if closest:
                side       = 'LONG' if closest.get('buyer') else 'SHORT'
                exit_price = float(closest.get('price', 0))
                qty        = float(closest.get('qty', 0))
                # Estimasi entry dari PnL: entry = exit ± (pnl / qty)
                if qty > 0:
                    if side == 'LONG':
                        entry_price = exit_price - (pnl / qty)
                    else:
                        entry_price = exit_price + (pnl / qty)
                else:
                    entry_price = exit_price
            else:
                # Fallback jika tidak ada user trade yang cocok
                side        = 'UNKNOWN'
                exit_price  = 0.0
                entry_price = 0.0
                qty         = 0.0

            # Gunakan actual_trade_id sebagai binance_order_id agar tidak terjadi
            # duplikat saat sync berikutnya (karena existing_trade_ids mengecek ini).
            order_id = actual_trade_id

            # Tambahkan offset kecil unik ke open_time untuk menghindari UNIQUE(symbol, open_time, side)
            # constraint violation jika ada beberapa trade di detik/milidetik yang sama.
            try:
                unique_offset = int(actual_trade_id) % 1000
            except ValueError:
                unique_offset = 0

            unique_open_time = close_time - 60000 + unique_offset

            try:
                conn.execute("""
                    INSERT OR IGNORE INTO trades
                    (symbol, side, qty, entry_price, exit_price,
                     open_time, close_time, realized_pnl, commission, net_pnl,
                     status, binance_order_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'CLOSED', ?)
                """, (
                    symbol, side, qty, entry_price, exit_price,
                    unique_open_time,
                    close_time,
                    pnl, commission, net_pnl,
                    order_id,
                ))
                new_count += conn.total_changes
            except Exception as e:
                logger.warning(f"[DB] Insert trade error {symbol}: {e}")

        conn.commit()
        conn.close()

    if new_count > 0:
        logger.info(f"[DB] Closed trades sync: +{new_count} posisi baru")

    return new_count


# ── Query helpers untuk dashboard ────────────────────────────────────

def get_stats() -> Dict:
    """Hitung statistik dari semua closed trades di DB (teragregasi by orderId)."""
    with _db_lock:
        conn = get_conn()
        row = conn.execute("""
            WITH lagged AS (
                SELECT symbol, side, net_pnl, realized_pnl, commission,
                       LAG(close_time) OVER (PARTITION BY symbol, side ORDER BY close_time) as prev_close,
                       close_time
                FROM trades
                WHERE status = 'CLOSED' AND close_time >= ?
            ),
            marked AS (
                SELECT *, CASE WHEN prev_close IS NULL OR (close_time - prev_close) > 7200000 THEN 1 ELSE 0 END as is_new_group
                FROM lagged
            ),
            grouped AS (
                SELECT *, SUM(is_new_group) OVER (PARTITION BY symbol, side ORDER BY close_time) as group_id
                FROM marked
            ),
            aggregated_trades AS (
                SELECT 
                    symbol, 
                    side, 
                    group_id,
                    SUM(net_pnl) AS net_pnl,
                    SUM(realized_pnl) AS realized_pnl,
                    SUM(commission) AS commission
                FROM grouped
                GROUP BY symbol, side, group_id
            )
            SELECT
                COUNT(*)                                    AS total_trades,
                SUM(CASE WHEN net_pnl > 0 THEN 1 ELSE 0 END) AS win_trades,
                SUM(CASE WHEN net_pnl < 0 THEN 1 ELSE 0 END) AS loss_trades,
                COALESCE(SUM(net_pnl), 0)                   AS total_pnl,
                COALESCE(SUM(realized_pnl), 0)              AS total_realized,
                COALESCE(SUM(commission), 0)                AS total_commission
            FROM aggregated_trades
        """, (SYNC_START_TS,)).fetchone()
        conn.close()

    total  = row['total_trades'] or 0
    wins   = row['win_trades']   or 0
    losses = row['loss_trades']  or 0

    return {
        'total_trades':      total,
        'win_trades':        wins,
        'loss_trades':       losses,
        'total_pnl':         round(row['total_pnl'] or 0, 4),
        'total_realized':    round(row['total_realized'] or 0, 4),
        'total_commission':  round(row['total_commission'] or 0, 4),
        'win_rate':          round(wins / total * 100, 2) if total > 0 else 0.0,
    }


def get_recent_trades(limit: int = 50) -> List[Dict]:
    """Ambil trade terbaru dari DB (teragregasi by orderId — 1 order = 1 baris)."""
    with _db_lock:
        conn = get_conn()
        rows = conn.execute("""
            WITH lagged AS (
                SELECT *, LAG(close_time) OVER (PARTITION BY symbol, side ORDER BY close_time) as prev_close
                FROM trades
                WHERE status = 'CLOSED' AND close_time >= ?
            ),
            marked AS (
                SELECT *, CASE WHEN prev_close IS NULL OR (close_time - prev_close) > 7200000 THEN 1 ELSE 0 END as is_new_group
                FROM lagged
            ),
            grouped AS (
                SELECT *, SUM(is_new_group) OVER (PARTITION BY symbol, side ORDER BY close_time) as group_id
                FROM marked
            )
            SELECT 
                symbol, 
                side, 
                SUM(qty) AS qty,
                CASE WHEN SUM(qty) > 0 THEN SUM(entry_price * qty) / SUM(qty) ELSE AVG(entry_price) END AS entry_price,
                CASE WHEN SUM(qty) > 0 THEN SUM(exit_price * qty) / SUM(qty) ELSE AVG(exit_price) END AS exit_price,
                MIN(open_time) AS open_time, 
                MAX(close_time) AS close_time, 
                SUM(realized_pnl) AS realized_pnl, 
                SUM(commission) AS commission,
                SUM(net_pnl) AS net_pnl, 
                MAX(leverage) AS leverage, 
                'CLOSED' AS status
            FROM grouped
            GROUP BY symbol, side, group_id
            ORDER BY close_time DESC
            LIMIT ?
        """, (SYNC_START_TS, limit)).fetchall()
        conn.close()

    return [dict(r) for r in rows]


def get_pnl_curve(limit: int = 200) -> List[Dict]:
    """Ambil data untuk PnL curve chart (teragregasi by close_time)."""
    with _db_lock:
        conn = get_conn()
        rows = conn.execute("""
            WITH lagged AS (
                SELECT *, LAG(close_time) OVER (PARTITION BY symbol, side ORDER BY close_time) as prev_close
                FROM trades
                WHERE status = 'CLOSED' AND close_time >= ?
            ),
            marked AS (
                SELECT *, CASE WHEN prev_close IS NULL OR (close_time - prev_close) > 7200000 THEN 1 ELSE 0 END as is_new_group
                FROM lagged
            ),
            grouped AS (
                SELECT *, SUM(is_new_group) OVER (PARTITION BY symbol, side ORDER BY close_time) as group_id
                FROM marked
            ),
            aggregated_trades AS (
                SELECT MAX(close_time) as close_time, SUM(net_pnl) AS net_pnl
                FROM grouped
                GROUP BY symbol, side, group_id
            )
            SELECT close_time, net_pnl,
                   SUM(net_pnl) OVER (ORDER BY close_time) AS cumulative_pnl
                FROM aggregated_trades
                ORDER BY close_time DESC
                LIMIT ?
        """, (SYNC_START_TS, limit)).fetchall()
        conn.close()

    return [dict(r) for r in reversed(rows)]



# ── Trade Intelligence CRUD ──────────────────────────────────────────

import json as _json
from datetime import datetime as _dt

def log_trade_open(
    trade_ref: str,
    symbol: str,
    direction: str,
    entry_time_utc: str,
    timeframe: str,
    session: str,
    entry_hour_utc: int,
    entry_weekday: int,
    setup_type: str,
    smc_signals: dict,
    mc_confidence: float,
    mc_win_prob: float,
    signal_score: float,
    risk_reward: float,
    atr: float,
    atr_pct: float,
    funding_rate: float,
    oi_change: float,
    htf_bias: str,
    bb_pct: float,
    rsi: float,
    macd_cross: int,
    vol_spike: int,
    entry_price: float,
    take_profit: float,
    stop_loss: float,
    leverage: int,
    margin_used: float,
    pending_duration_mins: int = 0,
    consecutive_losses_at_entry: int = 0,
    binance_order_id: str = None,
):
    """Catat pembukaan trade baru ke tabel trade_intelligence."""
    with _db_lock:
        conn = get_conn()
        try:
            conn.execute("""
                INSERT OR IGNORE INTO trade_intelligence (
                    trade_ref, binance_order_id, symbol, direction,
                    entry_time_utc, timeframe, session, entry_hour_utc, entry_weekday,
                    setup_type, smc_signals, mc_confidence, mc_win_prob, signal_score,
                    risk_reward, atr, atr_pct, funding_rate, oi_change, htf_bias,
                    bb_pct, rsi, macd_cross, vol_spike,
                    entry_price, take_profit, stop_loss,
                    leverage, margin_used, pending_duration_mins,
                    consecutive_losses_at_entry, outcome
                ) VALUES (
                    ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
                )
            """, (
                trade_ref, binance_order_id, symbol, direction,
                entry_time_utc, timeframe, session, entry_hour_utc, entry_weekday,
                setup_type, _json.dumps(smc_signals), mc_confidence, mc_win_prob, signal_score,
                risk_reward, atr, atr_pct, funding_rate, oi_change, htf_bias,
                bb_pct, rsi, macd_cross, vol_spike,
                entry_price, take_profit, stop_loss,
                leverage, margin_used, pending_duration_mins,
                consecutive_losses_at_entry, 'OPEN'
            ))
            conn.commit()
            logger.info(f"[DI] Logged trade open: {trade_ref}")
        except Exception as e:
            logger.error(f"[DI] log_trade_open error: {e}")
        finally:
            conn.close()


def log_trade_close(
    trade_ref: str,
    exit_price: float,
    result_pnl: float,
    close_time_utc: str,
    binance_order_id: str = None,
):
    """Update trade_intelligence saat posisi close."""
    with _db_lock:
        conn = get_conn()
        try:
            row = conn.execute(
                "SELECT entry_price, stop_loss, take_profit, entry_time_utc, direction FROM trade_intelligence WHERE trade_ref = ?",
                (trade_ref,)
            ).fetchone()

            if not row:
                # Jika tidak ada trade_ref, coba cari by binance_order_id
                if binance_order_id:
                    row = conn.execute(
                        "SELECT entry_price, stop_loss, take_profit, entry_time_utc, direction, trade_ref FROM trade_intelligence WHERE binance_order_id = ?",
                        (binance_order_id,)
                    ).fetchone()
                    if row:
                        trade_ref = row['trade_ref']

            if not row:
                logger.warning(f"[DI] log_trade_close: trade_ref '{trade_ref}' not found")
                conn.close()
                return

            entry_price = float(row['entry_price'])
            stop_loss   = float(row['stop_loss'])
            take_profit = float(row['take_profit'])
            direction   = row['direction']
            entry_time  = row['entry_time_utc']

            # Hitung RR yang tercapai
            sl_dist = abs(entry_price - stop_loss)
            if sl_dist > 0:
                price_move = exit_price - entry_price if direction == 'LONG' else entry_price - exit_price
                result_rr_achieved = price_move / sl_dist
            else:
                result_rr_achieved = 0.0

            # Tentukan outcome
            if result_pnl > 0:
                outcome = 'WIN'
            elif result_pnl < 0:
                outcome = 'LOSS'
            else:
                outcome = 'BE'

            # Hitung durasi
            try:
                t_open  = _dt.fromisoformat(entry_time.replace('Z', ''))
                t_close = _dt.fromisoformat(close_time_utc.replace('Z', ''))
                duration_mins = int((t_close - t_open).total_seconds() / 60)
            except Exception:
                duration_mins = 0

            conn.execute("""
                UPDATE trade_intelligence
                SET exit_price = ?, result_pnl = ?, result_rr_achieved = ?,
                    close_time_utc = ?, outcome = ?, trade_duration_mins = ?
                WHERE trade_ref = ?
            """, (
                exit_price, result_pnl, result_rr_achieved,
                close_time_utc, outcome, duration_mins,
                trade_ref
            ))
            conn.commit()
            logger.info(f"[DI] Logged trade close: {trade_ref} → {outcome} | PnL: {result_pnl:.4f}")
        except Exception as e:
            logger.error(f"[DI] log_trade_close error: {e}")
        finally:
            conn.close()


def update_mae_mfe(trade_ref: str, mark_price: float):
    """Update MAE dan MFE untuk trade yang masih open."""
    with _db_lock:
        conn = get_conn()
        try:
            row = conn.execute(
                "SELECT entry_price, stop_loss, direction, mae, mfe FROM trade_intelligence WHERE trade_ref = ? AND outcome = 'OPEN'",
                (trade_ref,)
            ).fetchone()
            if not row:
                conn.close()
                return

            entry   = float(row['entry_price'])
            sl      = float(row['stop_loss'])
            sl_dist = abs(entry - sl)
            if sl_dist == 0:
                conn.close()
                return

            direction = row['direction']
            cur_mae   = float(row['mae'] or 0)
            cur_mfe   = float(row['mfe'] or 0)

            if direction == 'LONG':
                excursion = (mark_price - entry) / sl_dist   # positive = profit, negative = adverse
            else:
                excursion = (entry - mark_price) / sl_dist

            new_mae = min(cur_mae, excursion)   # most negative
            new_mfe = max(cur_mfe, excursion)   # most positive

            if new_mae != cur_mae or new_mfe != cur_mfe:
                conn.execute(
                    "UPDATE trade_intelligence SET mae = ?, mfe = ? WHERE trade_ref = ?",
                    (new_mae, new_mfe, trade_ref)
                )
                conn.commit()
        except Exception as e:
            logger.error(f"[DI] update_mae_mfe error: {e}")
        finally:
            conn.close()


def get_consecutive_losses(symbol: str = None) -> int:
    """Hitung jumlah losses berturut-turut terbaru (opsional filter per pair)."""
    with _db_lock:
        conn = get_conn()
        try:
            if symbol:
                rows = conn.execute(
                    "SELECT outcome FROM trade_intelligence WHERE outcome IN ('WIN','LOSS','BE') AND symbol = ? ORDER BY entry_time_utc DESC LIMIT 20",
                    (symbol,)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT outcome FROM trade_intelligence WHERE outcome IN ('WIN','LOSS','BE') ORDER BY entry_time_utc DESC LIMIT 20"
                ).fetchall()
            conn.close()

            count = 0
            for r in rows:
                if r['outcome'] == 'LOSS':
                    count += 1
                else:
                    break
            return count
        except Exception as e:
            logger.error(f"[DI] get_consecutive_losses error: {e}")
            conn.close()
            return 0


def get_last_loss_close_time() -> Optional[str]:
    """Ambil close_time_utc dari loss terakhir."""
    with _db_lock:
        conn = get_conn()
        try:
            row = conn.execute(
                "SELECT close_time_utc FROM trade_intelligence WHERE outcome = 'LOSS' AND close_time_utc IS NOT NULL ORDER BY close_time_utc DESC LIMIT 1"
            ).fetchone()
            conn.close()
            return row['close_time_utc'] if row else None
        except Exception as e:
            logger.error(f"[DI] get_last_loss_close_time error: {e}")
            conn.close()
            return None


def get_pair_stats(symbol: str = None) -> List[Dict]:
    """Ambil statistik per pair dari tabel pair_stats."""
    with _db_lock:
        conn = get_conn()
        if symbol:
            rows = conn.execute("SELECT * FROM pair_stats WHERE symbol = ?", (symbol,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM pair_stats ORDER BY win_rate DESC").fetchall()
        conn.close()
    return [dict(r) for r in rows]


def get_session_stats() -> List[Dict]:
    """Ambil statistik per session dari tabel session_stats."""
    with _db_lock:
        conn = get_conn()
        rows = conn.execute("SELECT * FROM session_stats ORDER BY win_rate DESC").fetchall()
        conn.close()
    return [dict(r) for r in rows]


def get_setup_stats() -> List[Dict]:
    """Ambil statistik per setup dari tabel setup_stats."""
    with _db_lock:
        conn = get_conn()
        rows = conn.execute("SELECT * FROM setup_stats ORDER BY win_rate DESC").fetchall()
        conn.close()
    return [dict(r) for r in rows]


def get_oi_price_stats() -> List[Dict]:
    """Ambil statistik OI vs Price Change dari tabel oi_price_stats."""
    with _db_lock:
        conn = get_conn()
        rows = conn.execute("""
            SELECT * FROM oi_price_stats
            ORDER BY
                CASE oi_bucket
                    WHEN 'STRONG_RISE' THEN 1
                    WHEN 'RISE'        THEN 2
                    WHEN 'FLAT'        THEN 3
                    WHEN 'DROP'        THEN 4
                    WHEN 'STRONG_DROP' THEN 5
                    ELSE 6
                END,
                price_direction
        """).fetchall()
        conn.close()
    return [dict(r) for r in rows]


def get_intelligence_by_symbol(symbol: str, limit: int = 100) -> List[Dict]:
    """Ambil histori trade_intelligence untuk satu pair."""
    with _db_lock:
        conn = get_conn()
        rows = conn.execute(
            "SELECT * FROM trade_intelligence WHERE symbol = ? ORDER BY entry_time_utc DESC LIMIT ?",
            (symbol, limit)
        ).fetchall()
        conn.close()
    return [dict(r) for r in rows]


def get_hourly_stats() -> List[Dict]:
    """Hitung win rate per jam UTC dari trade_intelligence."""
    with _db_lock:
        conn = get_conn()
        rows = conn.execute("""
            SELECT entry_hour_utc,
                   COUNT(*) as total,
                   SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END) as wins,
                   ROUND(1.0 * SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END) / COUNT(*), 4) as win_rate
            FROM trade_intelligence
            WHERE outcome IN ('WIN','LOSS','BE')
            GROUP BY entry_hour_utc
            ORDER BY entry_hour_utc
        """).fetchall()
        conn.close()
    return [dict(r) for r in rows]


# ── Background sync loop ──────────────────────────────────────────────

def run_sync_loop():
    """
    Background thread: sync income + trades dari Binance setiap 10 detik.
    """
    init_db()
    logger.info("[DB] Starting background sync loop...")

    import analytics_engine
    from config import ANALYTICS_UPDATE_INTERVAL

    last_analytics_time = 0.0

    # Sync pertama langsung saat start
    try:
        sync_income_log()
        sync_closed_trades()
        
        # Run initial analytics
        try:
            analytics_engine.run_all_analytics()
            last_analytics_time = time.time()
        except Exception as ae:
            logger.error(f"[DB] Initial analytics update error: {ae}")

        # Update stats ke api_server state
        _push_stats_to_state()
    except Exception as e:
        logger.error(f"[DB] Initial sync error: {e}")

    while True:
        time.sleep(30)  # 30s untuk mengurangi frekuensi request ke Binance API
        try:
            new_income = sync_income_log()
            if new_income > 0:
                sync_closed_trades()

            # Run periodic analytics
            now = time.time()
            if now - last_analytics_time >= ANALYTICS_UPDATE_INTERVAL:
                try:
                    analytics_engine.run_all_analytics()
                    last_analytics_time = now
                except Exception as ae:
                    logger.error(f"[DB] Periodic analytics update error: {ae}")

            _push_stats_to_state()
        except Exception as e:
            logger.error(f"[DB] Sync loop error: {e}")


def _push_stats_to_state():
    """Push stats dari DB ke api_server _state."""
    try:
        import api_server as api
        stats = get_stats()
        trades = get_recent_trades(50)
        pnl_curve = get_pnl_curve(200)

        with api._state_lock:
            api._state['stats'] = {
                'total_trades': stats['total_trades'],
                'win_trades':   stats['win_trades'],
                'loss_trades':  stats['loss_trades'],
                'total_pnl':    stats['total_pnl'],
                'win_rate':     stats['win_rate'],
            }
            api._state['trades']    = trades
            api._state['pnl_curve'] = pnl_curve

    except Exception as e:
        logger.error(f"[DB] Push stats error: {e}")


# ── Feature 3: Auto-Blacklist (Standing Orders) ───────────────────────

def set_auto_blacklist(
    symbol: str,
    reason: str,
    blacklist_type: str = 'PAIR',
    session: str = None,
    win_rate: float = 0.0,
    total_trades: int = 0,
    expires_at: str = None,
):
    """Insert or update an auto-blacklist entry."""
    from datetime import datetime, timezone
    created_at = datetime.now(timezone.utc).isoformat()
    # Normalise session: None → '' so UNIQUE(symbol, blacklist_type, session) works
    session_key = session or ''
    with _db_lock:
        conn = get_conn()
        try:
            conn.execute("""
                INSERT INTO auto_blacklist
                    (symbol, reason, blacklist_type, session, win_rate, total_trades,
                     created_at, expires_at, active)
                VALUES (?,?,?,?,?,?,?,?,1)
                ON CONFLICT(symbol, blacklist_type, session) DO UPDATE SET
                    reason=excluded.reason,
                    win_rate=excluded.win_rate,
                    total_trades=excluded.total_trades,
                    created_at=excluded.created_at,
                    expires_at=excluded.expires_at,
                    active=1
            """, (symbol, reason, blacklist_type, session_key, win_rate, total_trades, created_at, expires_at))
            conn.commit()
            logger.info(f"[Blacklist] Set: {symbol} type={blacklist_type} session={session_key} | {reason}")
        except Exception as e:
            logger.error(f"[Blacklist] set_auto_blacklist error: {e}")
        finally:
            conn.close()


def get_active_blacklist() -> List[Dict]:
    """Return all active auto-blacklist entries."""
    with _db_lock:
        conn = get_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM auto_blacklist WHERE active=1 ORDER BY created_at DESC"
            ).fetchall()
            conn.close()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.error(f"[Blacklist] get_active_blacklist error: {e}")
            conn.close()
            return []


def clear_blacklist_entry(symbol: str, blacklist_type: str = 'PAIR', session: str = None):
    """Deactivate a blacklist entry (soft delete)."""
    session_key = session or ''
    with _db_lock:
        conn = get_conn()
        try:
            conn.execute(
                "UPDATE auto_blacklist SET active=0 WHERE symbol=? AND blacklist_type=? AND session=?",
                (symbol, blacklist_type, session_key)
            )
            conn.commit()
            logger.info(f"[Blacklist] Cleared: {symbol} type={blacklist_type} session={session_key}")
        except Exception as e:
            logger.error(f"[Blacklist] clear_blacklist_entry error: {e}")
        finally:
            conn.close()


# ── Feature 4: L3 Meta-Feedback ──────────────────────────────────────

def save_meta_feedback(
    trade_ref: str,
    meta_feedback: str,
    cio_verdict: str = None,
    cio_bull_reasoning: str = None,
    cio_bear_reasoning: str = None,
):
    """Save CIO debate details and meta-feedback to trade_intelligence."""
    with _db_lock:
        conn = get_conn()
        try:
            conn.execute("""
                UPDATE trade_intelligence
                SET meta_feedback=?,
                    cio_verdict=?,
                    cio_bull_reasoning=?,
                    cio_bear_reasoning=?
                WHERE trade_ref=?
            """, (meta_feedback, cio_verdict, cio_bull_reasoning, cio_bear_reasoning, trade_ref))
            conn.commit()
            logger.info(f"[MetaFeedback] Saved for trade_ref={trade_ref}")
        except Exception as e:
            logger.error(f"[MetaFeedback] save_meta_feedback error: {e}")
        finally:
            conn.close()


def get_trades_for_meta_eval(limit: int = 20) -> List[Dict]:
    """
    Fetch recently closed trades that have a CIO verdict but no meta_feedback yet.
    Used by L3 meta-feedback loop.
    """
    with _db_lock:
        conn = get_conn()
        try:
            rows = conn.execute("""
                SELECT trade_ref, symbol, direction, outcome, result_pnl,
                       cio_verdict, cio_bull_reasoning, cio_bear_reasoning
                FROM trade_intelligence
                WHERE outcome IN ('WIN','LOSS','BE')
                  AND cio_verdict IS NOT NULL
                  AND (meta_feedback IS NULL OR meta_feedback = '')
                ORDER BY close_time_utc DESC
                LIMIT ?
            """, (limit,)).fetchall()
            conn.close()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.error(f"[MetaFeedback] get_trades_for_meta_eval error: {e}")
            conn.close()
            return []
