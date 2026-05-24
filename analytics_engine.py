"""
analytics_engine.py - Decision Intelligence Analytics Engine for NERA QUANT

Reads from trade_intelligence table and computes:
  - Per-pair statistics and personality profiles
  - Per-session win rates
  - Per-setup win rates
  - Per-hour UTC heat map
  - Adaptive risk recommendations
"""

import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

import database as db

logger = logging.getLogger(__name__)


# ── Pair Personality Engine ────────────────────────────────────────────

def compute_pair_stats() -> List[Dict]:
    """
    Compute win rate, avg RR, best session, best timeframe per pair.
    Writes results to pair_stats table.
    Returns list of computed stats.
    """
    conn = db.get_conn()
    try:
        rows = conn.execute("""
            SELECT
                symbol,
                COUNT(*) AS total,
                SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END) AS wins,
                SUM(CASE WHEN outcome='LOSS' THEN 1 ELSE 0 END) AS losses,
                AVG(risk_reward) AS avg_rr,
                AVG(COALESCE(result_rr_achieved, 0)) AS avg_rr_achieved,
                AVG(COALESCE(trade_duration_mins, 0)) AS avg_duration,
                AVG(COALESCE(atr_pct, 0)) AS avg_atr_pct
            FROM trade_intelligence
            WHERE outcome IN ('WIN','LOSS','BE')
            GROUP BY symbol
        """).fetchall()

        results = []
        now_str = datetime.now(timezone.utc).isoformat()

        for r in rows:
            symbol     = r['symbol']
            total      = r['total']
            wins       = r['wins'] or 0
            losses     = r['losses'] or 0
            win_rate   = round(wins / total, 4) if total > 0 else 0.0
            avg_rr     = round(r['avg_rr'] or 0, 4)
            avg_rr_ach = round(r['avg_rr_achieved'] or 0, 4)
            avg_dur    = round(r['avg_duration'] or 0, 1)
            avg_atr    = round(r['avg_atr_pct'] or 0, 6)

            # Find best session for this pair
            best_session = _best_dimension(conn, symbol, 'session')
            best_tf      = _best_dimension(conn, symbol, 'timeframe')

            # Adaptive risk recommendation:
            # win_rate >= 65% → 2% | 50–65% → 1% | < 50% → 0.5%
            if win_rate >= 0.65:
                rec_risk = 0.02
            elif win_rate >= 0.50:
                rec_risk = 0.01
            else:
                rec_risk = 0.005

            conn.execute("""
                INSERT INTO pair_stats
                    (symbol, total_trades, win_trades, loss_trades, win_rate,
                     avg_rr, avg_rr_achieved, avg_duration_mins, best_session,
                     best_timeframe, avg_atr_pct, recommended_risk_pct, last_updated)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(symbol) DO UPDATE SET
                    total_trades=excluded.total_trades,
                    win_trades=excluded.win_trades,
                    loss_trades=excluded.loss_trades,
                    win_rate=excluded.win_rate,
                    avg_rr=excluded.avg_rr,
                    avg_rr_achieved=excluded.avg_rr_achieved,
                    avg_duration_mins=excluded.avg_duration_mins,
                    best_session=excluded.best_session,
                    best_timeframe=excluded.best_timeframe,
                    avg_atr_pct=excluded.avg_atr_pct,
                    recommended_risk_pct=excluded.recommended_risk_pct,
                    last_updated=excluded.last_updated
            """, (
                symbol, total, wins, losses, win_rate,
                avg_rr, avg_rr_ach, avg_dur, best_session,
                best_tf, avg_atr, rec_risk, now_str
            ))

            results.append({
                'symbol': symbol, 'total': total, 'wins': wins,
                'win_rate': win_rate, 'avg_rr': avg_rr,
                'best_session': best_session, 'best_timeframe': best_tf,
                'recommended_risk_pct': rec_risk,
            })

        conn.commit()
        logger.info(f"[Analytics] Pair stats updated: {len(results)} pairs")
        return results

    except Exception as e:
        logger.error(f"[Analytics] compute_pair_stats error: {e}")
        return []
    finally:
        conn.close()


def _best_dimension(conn, symbol: str, column: str) -> Optional[str]:
    """Find the dimension (session/timeframe) with highest win rate for a pair."""
    try:
        rows = conn.execute(f"""
            SELECT {column},
                   COUNT(*) AS total,
                   SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END) AS wins
            FROM trade_intelligence
            WHERE outcome IN ('WIN','LOSS','BE') AND symbol=? AND {column} IS NOT NULL
            GROUP BY {column}
            HAVING total >= 3
            ORDER BY (1.0*wins/total) DESC
            LIMIT 1
        """, (symbol,)).fetchone()
        return rows[column] if rows else None
    except Exception:
        return None


# ── Session Stats ──────────────────────────────────────────────────────

def compute_session_stats() -> List[Dict]:
    """
    Compute win rate per (session, timeframe) combination.
    Writes to session_stats table.
    """
    conn = db.get_conn()
    try:
        rows = conn.execute("""
            SELECT session, timeframe,
                   COUNT(*) AS total,
                   SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END) AS wins,
                   AVG(COALESCE(result_rr_achieved, 0)) AS avg_rr
            FROM trade_intelligence
            WHERE outcome IN ('WIN','LOSS','BE') AND session IS NOT NULL
            GROUP BY session, timeframe
        """).fetchall()

        results = []
        now_str = datetime.now(timezone.utc).isoformat()

        for r in rows:
            total    = r['total']
            wins     = r['wins'] or 0
            win_rate = round(wins / total, 4) if total > 0 else 0.0
            avg_rr   = round(r['avg_rr'] or 0, 4)

            conn.execute("""
                INSERT INTO session_stats (session, timeframe, total_trades, win_trades, win_rate, avg_rr, last_updated)
                VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(session, timeframe) DO UPDATE SET
                    total_trades=excluded.total_trades,
                    win_trades=excluded.win_trades,
                    win_rate=excluded.win_rate,
                    avg_rr=excluded.avg_rr,
                    last_updated=excluded.last_updated
            """, (r['session'], r['timeframe'], total, wins, win_rate, avg_rr, now_str))

            results.append({'session': r['session'], 'timeframe': r['timeframe'],
                            'total': total, 'wins': wins, 'win_rate': win_rate})

        conn.commit()
        logger.info(f"[Analytics] Session stats updated: {len(results)} rows")
        return results

    except Exception as e:
        logger.error(f"[Analytics] compute_session_stats error: {e}")
        return []
    finally:
        conn.close()


# ── Setup Stats ────────────────────────────────────────────────────────

def compute_setup_stats() -> List[Dict]:
    """
    Compute win rate per setup_type (INSTANT, SMC_OB_PULLBACK, OI_DIVERGENCE, PENDING_TRIGGER).
    Writes to setup_stats table.
    """
    conn = db.get_conn()
    try:
        rows = conn.execute("""
            SELECT setup_type,
                   COUNT(*) AS total,
                   SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END) AS wins,
                   AVG(COALESCE(result_rr_achieved, 0)) AS avg_rr
            FROM trade_intelligence
            WHERE outcome IN ('WIN','LOSS','BE') AND setup_type IS NOT NULL
            GROUP BY setup_type
        """).fetchall()

        results = []
        now_str = datetime.now(timezone.utc).isoformat()

        for r in rows:
            total    = r['total']
            wins     = r['wins'] or 0
            win_rate = round(wins / total, 4) if total > 0 else 0.0
            avg_rr   = round(r['avg_rr'] or 0, 4)

            conn.execute("""
                INSERT INTO setup_stats (setup_type, total_trades, win_trades, win_rate, avg_rr, last_updated)
                VALUES (?,?,?,?,?,?)
                ON CONFLICT(setup_type) DO UPDATE SET
                    total_trades=excluded.total_trades,
                    win_trades=excluded.win_trades,
                    win_rate=excluded.win_rate,
                    avg_rr=excluded.avg_rr,
                    last_updated=excluded.last_updated
            """, (r['setup_type'], total, wins, win_rate, avg_rr, now_str))

            results.append({'setup_type': r['setup_type'], 'total': total,
                            'wins': wins, 'win_rate': win_rate})

        conn.commit()
        logger.info(f"[Analytics] Setup stats updated: {len(results)} setups")
        return results

    except Exception as e:
        logger.error(f"[Analytics] compute_setup_stats error: {e}")
        return []
    finally:
        conn.close()


# ── OI vs Price Change Stats ───────────────────────────────────────────

def compute_oi_price_stats() -> List[Dict]:
    """
    Compute win rate per kombinasi OI change bucket × trade direction.

    OI Buckets (berdasarkan oi_change % saat entry):
      STRONG_RISE : oi_change >= +2%
      RISE        : +0.5% <= oi_change < +2%
      FLAT        : -0.5% < oi_change < +0.5%
      DROP        : -2% < oi_change <= -0.5%
      STRONG_DROP : oi_change <= -2%

    Insight yang dihasilkan:
      - OI naik kuat + LONG → apakah ini bullish confirmation atau bull trap?
      - OI turun kuat + SHORT → apakah ini bearish confirmation atau short squeeze?
      - Juga menyertakan avg mc_win_prob saat entry untuk validasi Monte Carlo accuracy.

    Writes to oi_price_stats table.
    """
    conn = db.get_conn()
    try:
        rows = conn.execute("""
            SELECT
                direction,
                oi_change,
                mc_win_prob,
                result_rr_achieved,
                outcome,
                CASE
                    WHEN oi_change >= 0.02  THEN 'STRONG_RISE'
                    WHEN oi_change >= 0.005 THEN 'RISE'
                    WHEN oi_change > -0.005 THEN 'FLAT'
                    WHEN oi_change > -0.02  THEN 'DROP'
                    ELSE                         'STRONG_DROP'
                END AS oi_bucket
            FROM trade_intelligence
            WHERE outcome IN ('WIN','LOSS','BE')
              AND oi_change IS NOT NULL
              AND direction IN ('LONG','SHORT')
        """).fetchall()

        if not rows:
            logger.info("[Analytics] OI price stats: no data yet")
            return []

        # Aggregate per (oi_bucket, direction)
        from collections import defaultdict
        buckets: dict = defaultdict(lambda: {
            'total': 0, 'wins': 0,
            'rr_sum': 0.0, 'mc_prob_sum': 0.0, 'oi_sum': 0.0
        })

        for r in rows:
            key = (r['oi_bucket'], r['direction'])
            b = buckets[key]
            b['total'] += 1
            if r['outcome'] == 'WIN':
                b['wins'] += 1
            b['rr_sum']      += float(r['result_rr_achieved'] or 0)
            b['mc_prob_sum'] += float(r['mc_win_prob'] or 0)
            b['oi_sum']      += float(r['oi_change'] or 0)

        results = []
        now_str = datetime.now(timezone.utc).isoformat()

        for (oi_bucket, direction), b in buckets.items():
            total    = b['total']
            wins     = b['wins']
            win_rate = round(wins / total, 4) if total > 0 else 0.0
            avg_rr   = round(b['rr_sum'] / total, 4) if total > 0 else 0.0
            avg_mc   = round(b['mc_prob_sum'] / total, 4) if total > 0 else 0.0
            avg_oi   = round(b['oi_sum'] / total, 6) if total > 0 else 0.0

            conn.execute("""
                INSERT INTO oi_price_stats
                    (oi_bucket, price_direction, total_trades, win_trades,
                     win_rate, avg_rr, avg_mc_win_prob, avg_oi_change, last_updated)
                VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(oi_bucket, price_direction) DO UPDATE SET
                    total_trades=excluded.total_trades,
                    win_trades=excluded.win_trades,
                    win_rate=excluded.win_rate,
                    avg_rr=excluded.avg_rr,
                    avg_mc_win_prob=excluded.avg_mc_win_prob,
                    avg_oi_change=excluded.avg_oi_change,
                    last_updated=excluded.last_updated
            """, (oi_bucket, direction, total, wins, win_rate, avg_rr, avg_mc, avg_oi, now_str))

            results.append({
                'oi_bucket': oi_bucket, 'direction': direction,
                'total': total, 'wins': wins, 'win_rate': win_rate,
                'avg_mc_win_prob': avg_mc,
            })

        conn.commit()
        logger.info(f"[Analytics] OI price stats updated: {len(results)} buckets")
        return results

    except Exception as e:
        logger.error(f"[Analytics] compute_oi_price_stats error: {e}")
        return []
    finally:
        conn.close()




def get_pair_personality(symbol: str) -> Dict:
    """
    Return rich personality profile for a pair from pair_stats.
    Falls back to defaults if insufficient data.
    """
    from config import MIN_PAIR_TRADES_FOR_STATS, RISK_PER_TRADE

    rows = db.get_pair_stats(symbol)
    if not rows or rows[0].get('total_trades', 0) < MIN_PAIR_TRADES_FOR_STATS:
        return {
            'symbol':                symbol,
            'has_enough_data':       False,
            'recommended_risk_pct':  RISK_PER_TRADE,
            'win_rate':              None,
            'best_session':          None,
            'best_timeframe':        None,
            'avg_atr_pct':           None,
            'avoid_session':         None,
        }

    p = rows[0]

    # Determine session to avoid (lowest win rate)
    avoid_session = _find_worst_session(symbol)

    return {
        'symbol':               symbol,
        'has_enough_data':      True,
        'total_trades':         p.get('total_trades', 0),
        'win_rate':             p.get('win_rate', 0.0),
        'avg_rr':               p.get('avg_rr', 0.0),
        'avg_rr_achieved':      p.get('avg_rr_achieved', 0.0),
        'avg_duration_mins':    p.get('avg_duration_mins', 0.0),
        'best_session':         p.get('best_session'),
        'best_timeframe':       p.get('best_timeframe'),
        'avg_atr_pct':          p.get('avg_atr_pct', 0.0),
        'recommended_risk_pct': p.get('recommended_risk_pct', RISK_PER_TRADE),
        'avoid_session':        avoid_session,
    }


def _find_worst_session(symbol: str) -> Optional[str]:
    """Return session with lowest win rate for this pair (if < 40%)."""
    conn = db.get_conn()
    try:
        rows = conn.execute("""
            SELECT session,
                   COUNT(*) AS total,
                   SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END) AS wins
            FROM trade_intelligence
            WHERE outcome IN ('WIN','LOSS','BE') AND symbol=? AND session IS NOT NULL
            GROUP BY session
            HAVING total >= 3
            ORDER BY (1.0*wins/total) ASC
            LIMIT 1
        """, (symbol,)).fetchone()
        if rows and (rows['wins'] or 0) / rows['total'] < 0.40:
            return rows['session']
        return None
    except Exception:
        return None
    finally:
        conn.close()


# ── AI Retrospective ───────────────────────────────────────────────────

def run_weekly_ai_retrospective():
    """
    Tarik 50 trade terakhir, format ke string, kirim ke Gemini untuk dievaluasi.
    Kirim laporan CIO ke Telegram.
    """
    try:
        from config import ENABLE_AI_RETROSPECTIVE
        if not getattr(ENABLE_AI_RETROSPECTIVE, 'real', True): # fallback if not in config
            pass
        if not ENABLE_AI_RETROSPECTIVE:
            return
    except ImportError:
        return

    try:
        import gemini_client
        from notifier import TelegramNotifier
        import database as db
        
        conn = db.get_conn()
        rows = conn.execute("""
            SELECT symbol, direction, outcome, entry_time_utc, timeframe,
                   mc_confidence, signal_score, risk_reward, result_pnl, setup_type, pending_duration_mins
            FROM trade_intelligence
            WHERE outcome IN ('WIN','LOSS','BE')
            ORDER BY entry_time_utc DESC
            LIMIT 50
        """).fetchall()
        conn.close()

        if len(rows) < 10:
            logger.info("[Retrospective] Not enough trades for a meaningful AI retrospective (min 10).")
            return

        trade_list = []
        for r in rows:
            trade_list.append(
                f"[{r['entry_time_utc']}] {r['symbol']} {r['direction']} ({r['timeframe']}) - {r['setup_type']} | "
                f"Outcome: {r['outcome']} | PnL: {r['result_pnl']} | "
                f"Conf: {r['mc_confidence']} | Score: {r['signal_score']} | RR: {r['risk_reward']}"
            )
            
        trades_str = "\n".join(trade_list)
        
        context = "You are the Chief Data Scientist for Nera Quant trading bot. Review the following recent trades."
        prompt = f"Analyze these trades. Identify why the losing trades failed, what the common patterns are for wins, and suggest 2-3 specific parameter adjustments for our Monte Carlo/SMC system.\n\nTrades:\n{trades_str}"
        
        logger.info("[Retrospective] Sending trades to Gemini for evaluation...")
        analysis = gemini_client.ask_gemini_text(prompt, context)
        
        notifier = TelegramNotifier()
        notifier._send_message(
            f"🧠 *AI WEEKLY RETROSPECTIVE*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{analysis[:3500]}" # Telegram has a 4096 char limit
        )
        logger.info("[Retrospective] CIO Laporan evaluasi mingguan dikirim ke Telegram.")
        
    except Exception as e:
        logger.error(f"[Retrospective] Error running AI evaluation: {e}")


# ── Run All ────────────────────────────────────────────────────────────

def run_all_analytics():
    """Run all analytics computations in sequence. Called by background loop."""
    logger.info("[Analytics] Running full analytics update...")
    compute_pair_stats()
    compute_session_stats()
    compute_setup_stats()
    compute_oi_price_stats()
    compute_auto_blacklist()       # Feature 3: Standing Orders
    logger.info("[Analytics] Full analytics update complete.")


# ─────────────────────────────────────────────────────────────────────
# Feature 2: ε-greedy Dynamic Setup Weighting
# ─────────────────────────────────────────────────────────────────────

def get_setup_weight(setup_type: str, epsilon: float = 0.10) -> float:
    """
    ε-greedy weight for a setup type based on historical win rate.

    With probability ε → return 1.0 (explore: treat all setups equally)
    With probability 1-ε → return win_rate-based weight (exploit)

    Weight scale:
      win_rate >= 0.65 → 1.20  (boost)
      win_rate >= 0.50 → 1.00  (neutral)
      win_rate >= 0.35 → 0.85  (slight penalty)
      win_rate <  0.35 → 0.70  (penalty)
      no data          → 1.00  (neutral, explore)

    OI_DIVERGENCE uses a stricter scale (reversal trades are riskier):
      win_rate >= 0.60 → 1.15
      win_rate >= 0.50 → 1.00
      win_rate >= 0.40 → 0.85
      win_rate <  0.40 → 0.65
    """
    import random
    if random.random() < epsilon:
        logger.debug(f"[ε-greedy] Exploring: setup_type={setup_type} → weight=1.0")
        return 1.0

    rows = db.get_setup_stats()
    for r in rows:
        if r.get('setup_type') == setup_type and r.get('total_trades', 0) >= 5:
            wr = r.get('win_rate', 0.0)

            # OI_DIVERGENCE: reversal strategy → threshold lebih ketat
            if setup_type == 'OI_DIVERGENCE':
                if wr >= 0.60:
                    weight = 1.15
                elif wr >= 0.50:
                    weight = 1.00
                elif wr >= 0.40:
                    weight = 0.85
                else:
                    weight = 0.65
            else:
                if wr >= 0.65:
                    weight = 1.20
                elif wr >= 0.50:
                    weight = 1.00
                elif wr >= 0.35:
                    weight = 0.85
                else:
                    weight = 0.70

            logger.debug(f"[ε-greedy] Exploit: setup_type={setup_type} win_rate={wr:.2f} → weight={weight}")
            return weight

    return 1.0  # No data → neutral


def get_timeframe_weight(timeframe: str, epsilon: float = 0.10) -> float:
    """
    ε-greedy weight for a timeframe based on session_stats win rate.
    Timeframes with higher historical win rate get boosted score multiplier.
    """
    import random
    if random.random() < epsilon:
        return 1.0

    rows = db.get_session_stats()
    tf_wins = 0
    tf_total = 0
    for r in rows:
        if r.get('timeframe') == timeframe and r.get('total_trades', 0) >= 3:
            tf_wins += r.get('win_trades', 0)
            tf_total += r.get('total_trades', 0)

    if tf_total < 5:
        return 1.0

    wr = tf_wins / tf_total
    if wr >= 0.60:
        return 1.15
    elif wr >= 0.45:
        return 1.00
    else:
        return 0.85


# ─────────────────────────────────────────────────────────────────────
# Feature 3: Standing Orders — Auto-Blacklist
# ─────────────────────────────────────────────────────────────────────

def compute_auto_blacklist(
    min_trades: int = 15,
    max_win_rate: float = 0.35,
    session_min_trades: int = 8,
    session_max_win_rate: float = 0.30,
) -> List[Dict]:
    """
    Identify chronically underperforming pairs and pair+session combos.
    Writes results to auto_blacklist table.

    Rules:
      - PAIR blacklist: total_trades >= min_trades AND win_rate < max_win_rate
      - PAIR_SESSION blacklist: session trades >= session_min_trades AND win_rate < session_max_win_rate
    """
    blacklisted = []
    now_str = datetime.now(timezone.utc).isoformat()

    # ── Pair-level blacklist ──────────────────────────────────────────
    pair_rows = db.get_pair_stats()
    for p in pair_rows:
        total = p.get('total_trades', 0)
        wr    = p.get('win_rate', 1.0)
        sym   = p.get('symbol', '')
        if total >= min_trades and wr < max_win_rate:
            reason = f"win_rate={wr:.2f} over {total} trades (threshold={max_win_rate})"
            db.set_auto_blacklist(
                symbol=sym,
                reason=reason,
                blacklist_type='PAIR',
                win_rate=wr,
                total_trades=total,
            )
            blacklisted.append({'symbol': sym, 'type': 'PAIR', 'win_rate': wr, 'total': total})
            logger.warning(f"[AutoBlacklist] PAIR blacklisted: {sym} | {reason}")

    # ── Pair+Session blacklist ────────────────────────────────────────
    conn = db.get_conn()
    try:
        rows = conn.execute("""
            SELECT symbol, session,
                   COUNT(*) AS total,
                   SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END) AS wins
            FROM trade_intelligence
            WHERE outcome IN ('WIN','LOSS','BE') AND session IS NOT NULL
            GROUP BY symbol, session
            HAVING total >= ?
        """, (session_min_trades,)).fetchall()
    except Exception as e:
        logger.error(f"[AutoBlacklist] DB query error: {e}")
        rows = []
    finally:
        conn.close()

    for r in rows:
        total = r['total']
        wins  = r['wins'] or 0
        wr    = wins / total
        sym   = r['symbol']
        sess  = r['session']
        if wr < session_max_win_rate:
            reason = f"win_rate={wr:.2f} in session={sess} over {total} trades"
            db.set_auto_blacklist(
                symbol=sym,
                reason=reason,
                blacklist_type='PAIR_SESSION',
                session=sess,
                win_rate=wr,
                total_trades=total,
            )
            blacklisted.append({'symbol': sym, 'type': 'PAIR_SESSION', 'session': sess, 'win_rate': wr})
            logger.warning(f"[AutoBlacklist] PAIR_SESSION blacklisted: {sym} @ {sess} | {reason}")

    if blacklisted:
        logger.info(f"[AutoBlacklist] Total blacklisted entries: {len(blacklisted)}")
    return blacklisted


def get_blacklisted_symbols() -> set:
    """
    Return set of symbols that are fully blacklisted (PAIR type, active).
    Used by scanner to filter out symbols before analysis.
    """
    entries = db.get_active_blacklist()
    return {e['symbol'] for e in entries if e.get('blacklist_type') == 'PAIR'}


def get_blacklisted_pair_sessions() -> set:
    """
    Return set of (symbol, session) tuples that are blacklisted.
    Used by scanner to skip signals in bad sessions.
    """
    entries = db.get_active_blacklist()
    return {
        (e['symbol'], e['session'])
        for e in entries
        if e.get('blacklist_type') == 'PAIR_SESSION' and e.get('session', '') != ''
    }


# ─────────────────────────────────────────────────────────────────────
# Feature 4: L3 Meta-Feedback Loop
# ─────────────────────────────────────────────────────────────────────

def run_meta_feedback_loop(limit: int = 20):
    """
    L3 meta-feedback: for each recently closed trade with a CIO verdict
    but no meta_feedback yet, ask Gemini to evaluate whether the CIO
    debate was correct given the actual outcome.

    Stores result in trade_intelligence.meta_feedback.
    """
    try:
        import gemini_client

        trades = db.get_trades_for_meta_eval(limit=limit)
        if not trades:
            logger.debug("[MetaFeedback] No trades pending meta-evaluation.")
            return

        logger.info(f"[MetaFeedback] Running L3 meta-eval on {len(trades)} trades...")
        for t in trades:
            try:
                meta = gemini_client.ask_gemini_meta_eval(
                    symbol=t.get('symbol', ''),
                    direction=t.get('direction', ''),
                    cio_verdict=t.get('cio_verdict', ''),
                    outcome=t.get('outcome', ''),
                    bull_reasoning=t.get('cio_bull_reasoning', '') or '',
                    bear_reasoning=t.get('cio_bear_reasoning', '') or '',
                )
                db.save_meta_feedback(
                    trade_ref=t['trade_ref'],
                    meta_feedback=meta,
                )
                logger.info(f"[MetaFeedback] Saved for {t['trade_ref']}")
            except Exception as e:
                logger.error(f"[MetaFeedback] Error for {t.get('trade_ref')}: {e}")

    except Exception as e:
        logger.error(f"[MetaFeedback] run_meta_feedback_loop error: {e}")
