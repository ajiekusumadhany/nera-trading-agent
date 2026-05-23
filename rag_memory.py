"""
rag_memory.py - RAG Pattern Memory for NERA QUANT (Feature 5)

Stores trade setup feature vectors as embeddings in SQLite (BLOB).
Uses numpy cosine similarity for retrieval — no external vector DB needed.

Flow:
  1. When a trade closes → store_pattern(trade_ref, symbol, features, outcome)
  2. Before CIO check → find_similar_patterns(features, top_k=5) → enrich context
"""

import json
import logging
import threading
import numpy as np
import sqlite3
from typing import List, Dict, Optional

logger = logging.getLogger(__name__)

DB_PATH = '/home/ajiekusumadhany.me/public_html/nera-quant/trades.db'
_rag_lock = threading.Lock()

# ── Feature keys used for embedding (order matters — must be consistent) ──
_FEATURE_KEYS = [
    'rsi', 'bb_pct', 'ema_trend', 'above_ema50', 'macd_cross',
    'macd_positive', 'vol_ratio', 'stoch_cross', 'bos', 'choch',
    'fvg_dir', 'ob_retest', 'funding_rate', 'oi_change',
    'atr_pct', 'signal_score', 'mc_confidence', 'mc_win_prob',
    'risk_reward',
]

# ── Schema ─────────────────────────────────────────────────────────────

RAG_SCHEMA = """
CREATE TABLE IF NOT EXISTS pattern_embeddings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_ref       TEXT    UNIQUE NOT NULL,
    symbol          TEXT    NOT NULL,
    direction       TEXT    NOT NULL,
    features_json   TEXT    NOT NULL,
    embedding_blob  BLOB    NOT NULL,
    outcome         TEXT    NOT NULL,   -- WIN / LOSS / BE
    result_pnl      REAL    DEFAULT 0.0,
    risk_reward     REAL    DEFAULT 0.0,
    session         TEXT,
    timeframe       TEXT,
    created_at      TEXT    NOT NULL
);
"""


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_rag_schema():
    """Create pattern_embeddings table if not exists."""
    with _rag_lock:
        conn = _get_conn()
        conn.executescript(RAG_SCHEMA)
        conn.commit()
        conn.close()
    logger.info("[RAG] Schema initialized.")


# ── Embedding ──────────────────────────────────────────────────────────

def embed_features(features: dict) -> np.ndarray:
    """
    Convert a signal features dict into a fixed-length float32 vector.
    Missing keys default to 0.0. Values are normalized to [0, 1] range.
    """
    vec = []
    for key in _FEATURE_KEYS:
        val = features.get(key, 0.0)
        if val is None:
            val = 0.0
        try:
            val = float(val)
        except (TypeError, ValueError):
            val = 0.0
        vec.append(val)

    arr = np.array(vec, dtype=np.float32)

    # Normalize RSI to [0,1]
    if arr[0] > 1.0:
        arr[0] = arr[0] / 100.0

    # Normalize risk_reward (cap at 5.0)
    rr_idx = _FEATURE_KEYS.index('risk_reward')
    arr[rr_idx] = min(arr[rr_idx] / 5.0, 1.0)

    # Normalize atr_pct (cap at 5%)
    atr_idx = _FEATURE_KEYS.index('atr_pct')
    arr[atr_idx] = min(arr[atr_idx] / 0.05, 1.0)

    return arr


def _cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity between two vectors."""
    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return float(np.dot(a, b) / (norm_a * norm_b))


# ── Store ──────────────────────────────────────────────────────────────

def store_pattern(
    trade_ref: str,
    symbol: str,
    direction: str,
    features: dict,
    outcome: str,
    result_pnl: float = 0.0,
    risk_reward: float = 0.0,
    session: str = None,
    timeframe: str = None,
):
    """
    Store a completed trade's feature vector in pattern_embeddings.
    Called after trade closes.
    """
    try:
        embedding = embed_features(features)
        embedding_blob = embedding.tobytes()
        features_json = json.dumps({k: features.get(k, 0.0) for k in _FEATURE_KEYS})

        from datetime import datetime, timezone
        created_at = datetime.now(timezone.utc).isoformat()

        with _rag_lock:
            conn = _get_conn()
            try:
                conn.execute("""
                    INSERT OR REPLACE INTO pattern_embeddings
                        (trade_ref, symbol, direction, features_json, embedding_blob,
                         outcome, result_pnl, risk_reward, session, timeframe, created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    trade_ref, symbol, direction, features_json, embedding_blob,
                    outcome, result_pnl, risk_reward, session, timeframe, created_at
                ))
                conn.commit()
                logger.info(f"[RAG] Stored pattern: {trade_ref} | {symbol} {direction} → {outcome}")
            finally:
                conn.close()
    except Exception as e:
        logger.error(f"[RAG] store_pattern error: {e}")


# ── Retrieve ───────────────────────────────────────────────────────────

def find_similar_patterns(
    features: dict,
    top_k: int = 5,
    min_similarity: float = 0.80,
    exclude_trade_ref: str = None,
) -> List[Dict]:
    """
    Find top-k most similar historical patterns using cosine similarity.
    Returns list of dicts with similarity score and outcome info.
    """
    try:
        query_vec = embed_features(features)

        with _rag_lock:
            conn = _get_conn()
            try:
                rows = conn.execute("""
                    SELECT trade_ref, symbol, direction, embedding_blob,
                           outcome, result_pnl, risk_reward, session, timeframe
                    FROM pattern_embeddings
                    WHERE outcome IN ('WIN', 'LOSS', 'BE')
                """).fetchall()
            finally:
                conn.close()

        if not rows:
            return []

        scored = []
        for row in rows:
            if exclude_trade_ref and row['trade_ref'] == exclude_trade_ref:
                continue
            try:
                stored_vec = np.frombuffer(row['embedding_blob'], dtype=np.float32)
                if len(stored_vec) != len(query_vec):
                    continue
                sim = _cosine_similarity(query_vec, stored_vec)
                if sim >= min_similarity:
                    scored.append({
                        'trade_ref':  row['trade_ref'],
                        'symbol':     row['symbol'],
                        'direction':  row['direction'],
                        'outcome':    row['outcome'],
                        'result_pnl': row['result_pnl'],
                        'risk_reward': row['risk_reward'],
                        'session':    row['session'],
                        'timeframe':  row['timeframe'],
                        'similarity': round(sim, 4),
                    })
            except Exception:
                continue

        scored.sort(key=lambda x: x['similarity'], reverse=True)
        return scored[:top_k]

    except Exception as e:
        logger.error(f"[RAG] find_similar_patterns error: {e}")
        return []


def format_similar_patterns_for_context(patterns: List[Dict]) -> str:
    """Format similar patterns into a readable string for Gemini context."""
    if not patterns:
        return ""
    lines = []
    win_count = sum(1 for p in patterns if p['outcome'] == 'WIN')
    loss_count = sum(1 for p in patterns if p['outcome'] == 'LOSS')
    lines.append(f"Top {len(patterns)} similar historical setups: {win_count} WIN, {loss_count} LOSS")
    for i, p in enumerate(patterns, 1):
        lines.append(
            f"  {i}. {p['symbol']} {p['direction']} [{p['session']}/{p['timeframe']}] "
            f"→ {p['outcome']} | PnL={p['result_pnl']:.4f} | RR={p['risk_reward']:.2f} | sim={p['similarity']:.2f}"
        )
    return "\n".join(lines)


# ── Stats ──────────────────────────────────────────────────────────────

def get_rag_stats() -> Dict:
    """Return basic stats about the pattern memory."""
    try:
        with _rag_lock:
            conn = _get_conn()
            row = conn.execute("""
                SELECT COUNT(*) as total,
                       SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END) as wins,
                       SUM(CASE WHEN outcome='LOSS' THEN 1 ELSE 0 END) as losses
                FROM pattern_embeddings
            """).fetchone()
            conn.close()
        return {'total': row['total'], 'wins': row['wins'], 'losses': row['losses']}
    except Exception as e:
        logger.error(f"[RAG] get_rag_stats error: {e}")
        return {'total': 0, 'wins': 0, 'losses': 0}
