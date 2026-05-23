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
    Compute win rate per setup_type (INSTANT, SMC_OB_PULLBACK, PENDING_TRIGGER).
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


# ── Pair Personality ───────────────────────────────────────────────────

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
    logger.info("[Analytics] Full analytics update complete.")
