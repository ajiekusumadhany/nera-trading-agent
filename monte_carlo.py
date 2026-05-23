"""
monte_carlo.py - Monte Carlo simulation untuk probabilitas open posisi
Simulasi ribuan skenario harga untuk menentukan confidence level sinyal
"""

import numpy as np
import pandas as pd
import logging
from typing import Dict, Tuple, Optional
from dataclasses import dataclass
from config import MC_SIMULATIONS, MC_CONFIDENCE_THRESHOLD, HTF_STRICT_GATEKEEPER, HTF_REQUIRE_BOTH_CONFIRM
from market_context import get_htf_bias

logger = logging.getLogger(__name__)


@dataclass
class SimulationResult:
    """Hasil Monte Carlo simulation untuk satu pair."""
    symbol:           str
    direction:        str        # 'LONG' | 'SHORT' | 'NEUTRAL'
    confidence:       float      # 0.0 - 1.0
    win_probability:  float      # Probabilitas profit
    expected_return:  float      # Expected return (%)
    risk_reward:      float      # Risk/Reward ratio
    entry_price:      float
    take_profit:      float
    stop_loss:        float
    simulations_run:  int
    profitable_paths: int
    signal_score:     float      # Composite score dari indicators
    tp_multiplier:    float = 2.5  # ATR multiplier yang dipakai
    sl_multiplier:    float = 1.5
    indicator_breakdown: dict = None
    timeframe:        str = '15m'
    bull_ob_top:      float = 0.0
    bull_ob_bot:      float = 0.0
    bear_ob_top:      float = 0.0
    bear_ob_bot:      float = 0.0
    atr:              float = 0.0
    atr_pct:          float = 0.0
    rsi:              float = 50.0
    bb_pct:           float = 0.5
    macd_cross:       int = 0
    vol_spike:        int = 0
    htf_bias:         str = 'NEUTRAL'
    oi_change:        float = 0.0
    funding_rate:     float = 0.0


class MonteCarloEngine:
    """
    Engine simulasi Monte Carlo untuk analisis probabilistik.

    Cara kerja:
    1. Ambil distribusi return historis dari OHLCV data
    2. Simulasikan N path harga ke depan (Geometric Brownian Motion + fat tails)
    3. Hitung probabilitas harga mencapai TP sebelum SL
    4. Combine dengan technical signal score
    5. Output confidence level untuk keputusan open posisi
    """

    def __init__(self, n_simulations: int = MC_SIMULATIONS):
        self.n_simulations = n_simulations

    def run(
        self,
        symbol: str,
        df: pd.DataFrame,
        features: Dict,
        timeframe: str = '15m',
        funding_rate: float = 0.0,
        htf_features: Optional[Dict] = None,
        oi_change: float = 0.0
    ) -> Optional[SimulationResult]:
        """
        Jalankan full Monte Carlo analysis untuk satu pair.

        Args:
            symbol:       Trading pair symbol
            df:           OHLCV DataFrame dengan indicators
            features:     Dict dari TechnicalIndicators.get_signal_features()
            funding_rate: Current funding rate
            htf_features: Dict dari HTF TechnicalIndicators
            oi_change:    Persentase perubahan Open Interest terakhir

        Returns:
            SimulationResult atau None jika data tidak cukup
        """
        if df is None or len(df) < 30:
            return None

        try:
            # ── Step 1: Hitung signal score dari indicators ───────────
            signal_score, direction, agreement_pct, breakdown = self._compute_signal_score(
                features, funding_rate, htf_features, oi_change
            )

            if direction == 'NEUTRAL':
                return SimulationResult(
                    symbol=symbol, direction='NEUTRAL',
                    confidence=0.0, win_probability=0.0,
                    expected_return=0.0, risk_reward=0.0,
                    entry_price=features['price'],
                    take_profit=0.0, stop_loss=0.0,
                    simulations_run=0, profitable_paths=0,
                    signal_score=signal_score,
                    indicator_breakdown=breakdown,
                    timeframe=timeframe
                )

            # ── Step 2: Estimasi parameter distribusi return ──────────
            returns = df['close'].pct_change().dropna()
            sigma   = returns.std()

            # mu = 0: simulasi path harga NETRAL tanpa bias arah.
            # Tujuan: win_probability murni mencerminkan jarak TP vs SL
            # terhadap volatilitas, bukan tren historis.
            # Arah trading ditentukan sepenuhnya oleh signal_score (indikator teknikal).
            # Jika mu diambil dari drift historis, saat market bullish LONG selalu
            # menang lebih sering meski indikatornya lemah — ini bias yang tidak adil.
            mu = 0.0

            # ── Step 3: Tentukan TP/SL berdasarkan SMC / ATR adaptif ──────────
            atr     = df['atr'].iloc[-1]
            price   = features['price']
            atr_pct = features['atr_pct']

            # Multiplier dinamis berdasarkan kondisi market
            tp_mult, sl_mult = self._calc_dynamic_multipliers(features, df)

            tp_distance = atr * tp_mult
            sl_distance = atr * sl_mult

            # Import SMC settings
            from config import SMC_MODE
            
            use_smc_levels = False
            if SMC_MODE:
                bull_ob_bot = features.get('bull_ob_bot', 0.0)
                bull_ob_top = features.get('bull_ob_top', 0.0)
                bear_ob_top = features.get('bear_ob_top', 0.0)
                bear_ob_bot = features.get('bear_ob_bot', 0.0)
                
                if direction == 'LONG' and bull_ob_bot > 0.0:
                    # Place SL below OB bottom with a 0.5 * ATR buffer to prevent getting stopped out by liquidity sweeps
                    stop_loss = bull_ob_bot - (0.5 * atr)
                    # Enforce minimum stop loss distance of sl_distance
                    if price - stop_loss < sl_distance:
                        stop_loss = price - sl_distance
                    
                    # Place TP at the bottom of the Bearish OB (resistance zone), to exit reliably
                    take_profit = bear_ob_bot * 0.999 if (bear_ob_bot > price) else (price + tp_distance)
                    use_smc_levels = True
                elif direction == 'SHORT' and bear_ob_top > 0.0:
                    # Place SL above OB top with a 0.5 * ATR buffer to prevent getting stopped out by liquidity sweeps
                    stop_loss = bear_ob_top + (0.5 * atr)
                    if stop_loss - price < sl_distance:
                        stop_loss = price + sl_distance
                    
                    # Place TP at the top of the Bullish OB (support zone), to exit reliably
                    take_profit = bull_ob_top * 1.001 if (bull_ob_top > 0.0 and bull_ob_top < price) else (price - tp_distance)
                    use_smc_levels = True

            if not use_smc_levels:
                if direction == 'LONG':
                    take_profit = price + tp_distance
                    stop_loss   = price - sl_distance
                else:  # SHORT
                    take_profit = price - tp_distance
                    stop_loss   = price + sl_distance

            # Verifikasi jarak valid
            sl_dist = abs(price - stop_loss)
            tp_dist = abs(take_profit - price)
            if sl_dist <= 0:
                sl_dist = atr * 1.0
                stop_loss = (price - sl_dist) if direction == 'LONG' else (price + sl_dist)
            if tp_dist <= 0:
                tp_dist = atr * 2.0
                take_profit = (price + tp_dist) if direction == 'LONG' else (price - tp_dist)

            # Batasi minimal R/R ratio ke 1.5 untuk SMC (Sangat Agresif)
            if (tp_dist / sl_dist) < 1.5:
                take_profit = (price + sl_dist * 2.0) if direction == 'LONG' else (price - sl_dist * 2.0)
                tp_dist = abs(take_profit - price)

            risk_reward = tp_dist / sl_dist

            # ── Step 4: Monte Carlo simulation ───────────────────────
            if timeframe == '5m':
                n_steps = 25
            elif timeframe == '15m':
                n_steps = 40
            elif timeframe == '30m':
                n_steps = 50
            elif timeframe == '1h':
                n_steps = 60
            else:
                n_steps = 40

            smc_levels = {
                'bull_ob_top': features.get('bull_ob_top', 0.0),
                'bull_ob_bot': features.get('bull_ob_bot', 0.0),
                'bear_ob_top': features.get('bear_ob_top', 0.0),
                'bear_ob_bot': features.get('bear_ob_bot', 0.0),
                'fvg_top':     features.get('fvg_top', 0.0),
                'fvg_bot':     features.get('fvg_bot', 0.0),
                'fvg_dir':     features.get('fvg_dir', 0),
            }

            win_prob, expected_ret, profitable_paths = self._simulate_paths(
                price=price,
                mu=mu,
                sigma=sigma,
                take_profit=take_profit,
                stop_loss=stop_loss,
                direction=direction,
                n_steps=n_steps,
                smc_levels=smc_levels,
            )

            # ── Step 5: Hitung composite confidence ──────────────────
            # signal_score dan agreement_pct bisa double-count (nilai sama).
            # Pakai bobot: win_prob (60%) + signal_score (40%) — lebih jujur.
            # Confidence di-cap keras di 0.92 agar tidak muncul angka 100%.
            confidence = (win_prob * 0.60) + (signal_score * 0.40)
            confidence = min(confidence, 0.92)   # hard cap

            # Penalti jika funding rate berlawanan dengan arah
            if direction == 'LONG' and funding_rate > 0.001:
                confidence *= 0.90  # Funding positif = bearish pressure
            elif direction == 'SHORT' and funding_rate < -0.001:
                confidence *= 0.90

            # Cap akhir setelah penalti
            confidence = min(confidence, 0.92)

            logger.debug(
                f"{symbol} | {direction} | conf={confidence:.2%} | "
                f"win_prob={win_prob:.2%} | score={signal_score:.2f} | agreement={agreement_pct:.2%}"
            )

            return SimulationResult(
                symbol=symbol,
                direction=direction,
                confidence=round(confidence, 4),
                win_probability=round(win_prob, 4),
                expected_return=round(expected_ret * 100, 2),
                risk_reward=round(risk_reward, 2),
                entry_price=price,
                take_profit=round(take_profit, 6),
                stop_loss=round(stop_loss, 6),
                simulations_run=self.n_simulations,
                profitable_paths=profitable_paths,
                signal_score=round(signal_score, 4),
                tp_multiplier=tp_mult,
                sl_multiplier=sl_mult,
                indicator_breakdown=breakdown,
                timeframe=timeframe,
                bull_ob_top=round(features.get('bull_ob_top', 0.0), 6),
                bull_ob_bot=round(features.get('bull_ob_bot', 0.0), 6),
                bear_ob_top=round(features.get('bear_ob_top', 0.0), 6),
                bear_ob_bot=round(features.get('bear_ob_bot', 0.0), 6),
                atr=round(features.get('atr', 0.0), 6),
                atr_pct=round(features.get('atr_pct', 0.0), 6),
                rsi=round(features.get('rsi', 50.0), 2),
                bb_pct=round(features.get('bb_pct', 0.5), 4),
                macd_cross=int(features.get('macd_cross', 0)),
                vol_spike=int(features.get('vol_spike', 0)),
                htf_bias=get_htf_bias(htf_features),
                oi_change=round(oi_change, 6),
                funding_rate=round(funding_rate, 6)
            )

        except Exception as e:
            logger.error(f"Monte Carlo error for {symbol}: {e}")
            return None

    def _calc_dynamic_multipliers(
        self,
        features: Dict,
        df: pd.DataFrame,
    ) -> tuple:
        """
        Hitung TP/SL multiplier secara adaptif berdasarkan kondisi market.

        Kondisi yang dipertimbangkan:
        1. Kekuatan trend (EMA alignment + MACD)
        2. Volatilitas relatif (ATR% vs rata-rata historis)
        3. Bollinger Band width (squeeze vs expansion)
        4. RSI posisi (extreme vs middle)

        Returns:
            (tp_multiplier, sl_multiplier)
        """
        # Base multiplier
        tp_mult = 2.5
        sl_mult = 1.5

        # ── 1. Trend strength adjustment ─────────────────────────────
        # Trending kuat → biarkan TP lebih jauh
        ema_trend  = features.get('ema_trend', 0)    # -1, 0, 1
        macd_hist  = features.get('macd_hist', 0)
        macd_pos   = features.get('macd_positive', 0)

        if ema_trend == 0:
            trend_score = 0
        elif (ema_trend == 1 and macd_pos == 1) or (ema_trend == -1 and macd_pos == -1):
            trend_score = 2
        else:
            trend_score = 1
        # trend_score: 0 = flat, 1 = weak, 2 = strong

        if trend_score >= 2:
            tp_mult += 0.5   # trending kuat → TP lebih jauh
            sl_mult -= 0.1   # SL sedikit lebih ketat
        elif trend_score == 0:
            tp_mult -= 0.4   # sideways → TP lebih dekat
            sl_mult -= 0.2   # SL lebih ketat juga

        # ── 2. Volatilitas relatif ────────────────────────────────────
        # Bandingkan ATR sekarang vs rata-rata ATR historis
        atr_pct = features.get('atr_pct', 0)
        try:
            atr_mean = df['atr_pct'].rolling(50).mean().iloc[-1]
            atr_ratio = atr_pct / atr_mean if atr_mean > 0 else 1.0
        except Exception:
            atr_ratio = 1.0

        if atr_ratio > 1.5:
            # Volatilitas tinggi → SL lebih lebar supaya tidak kena noise
            sl_mult += 0.3
            tp_mult += 0.3
        elif atr_ratio < 0.7:
            # Volatilitas rendah → SL lebih ketat
            sl_mult -= 0.2

        # ── 3. Bollinger Band width (squeeze detection) ───────────────
        bb_width = features.get('bb_width', 0)
        try:
            bb_width_mean = df['bb_width'].rolling(50).mean().iloc[-1]
            bb_ratio = bb_width / bb_width_mean if bb_width_mean > 0 else 1.0
        except Exception:
            bb_ratio = 1.0

        if bb_ratio < 0.7:
            # BB squeeze → breakout imminent, TP lebih jauh
            tp_mult += 0.4
        elif bb_ratio > 1.5:
            # BB expansion → sudah bergerak jauh, TP lebih konservatif
            tp_mult -= 0.3

        # ── 4. RSI extreme adjustment ─────────────────────────────────
        rsi = features.get('rsi', 50)
        if rsi < 25 or rsi > 75:
            # RSI sangat extreme → potensi reversal kuat, TP lebih jauh
            tp_mult += 0.3
        elif 45 <= rsi <= 55:
            # RSI di tengah → tidak ada momentum jelas, TP lebih konservatif
            tp_mult -= 0.2

        # ── Clamp ke range yang masuk akal ───────────────────────────
        tp_mult = max(1.5, min(4.5, tp_mult))   # TP: 1.5x - 4.5x ATR
        sl_mult = max(0.8, min(2.5, sl_mult))   # SL: 0.8x - 2.5x ATR

        # Pastikan RR ratio minimal 1.2 (TP harus lebih besar dari SL)
        if tp_mult / sl_mult < 1.2:
            tp_mult = sl_mult * 1.2

        return round(tp_mult, 2), round(sl_mult, 2)

    def _simulate_paths(
        self,
        price: float,
        mu: float,
        sigma: float,
        take_profit: float,
        stop_loss: float,
        direction: str,
        n_steps: int = 40,
        smc_levels: dict = None,
    ) -> Tuple[float, float, int]:
        """
        Simulasikan N path harga menggunakan GBM (Geometric Brownian Motion).
        Jika SMC_MODE aktif, jalur harga akan dipengaruhi oleh:
        - FVG Gravity: tarikan magnet ke arah inefisiensi FVG yang belum terisi.
        - OB Elastic Barrier: pantulan harga (75% bounce rate) ketika menyentuh Order Block.
        """
        np.random.seed(None)  # Fresh seed setiap run

        # Ambil konfigurasi SMC
        from config import SMC_MODE
        
        # Ekstrak level SMC jika tersedia
        smc_levels = smc_levels or {}
        bull_ob_top = smc_levels.get('bull_ob_top', 0.0)
        bull_ob_bot = smc_levels.get('bull_ob_bot', 0.0)
        bear_ob_top = smc_levels.get('bear_ob_top', 0.0)
        bear_ob_bot = smc_levels.get('bear_ob_bot', 0.0)
        fvg_top = smc_levels.get('fvg_top', 0.0)
        fvg_bot = smc_levels.get('fvg_bot', 0.0)
        fvg_dir = smc_levels.get('fvg_dir', 0)

        # Inisialisasi paths matrix: shape (n_simulations, n_steps)
        price_paths = np.zeros((self.n_simulations, n_steps))
        price_paths[:, 0] = price

        is_long = (direction == 'LONG')

        # Simulasi step-by-step secara efisien
        for t in range(1, n_steps):
            # standard normal random array
            rand_z = np.random.normal(0, 1, self.n_simulations)
            
            # Fat tail: 5% chance pergerakan besar (simulasi crash/pump mendadak)
            fat_tail = np.random.random(self.n_simulations) < 0.05
            rand_z = np.where(fat_tail, np.random.normal(0, 3, self.n_simulations), rand_z)

            prev_price = price_paths[:, t-1]
            step_mu = np.full(self.n_simulations, mu)

            # A. FVG Gravity: tarikan magnet ke arah FVG
            if SMC_MODE:
                if is_long and fvg_dir == -1 and fvg_bot > price:
                    # Bearish FVG di atas bertindak sebagai magnet penarik naik
                    fvg_center = (fvg_top + fvg_bot) / 2.0
                    pull = 0.03 * (fvg_center - prev_price) / prev_price
                    step_mu += np.clip(pull, 0.0, 0.015)
                elif not is_long and fvg_dir == 1 and fvg_top < price:
                    # Bullish FVG di bawah bertindak sebagai magnet penarik turun
                    fvg_center = (fvg_top + fvg_bot) / 2.0
                    pull = 0.03 * (fvg_center - prev_price) / prev_price
                    step_mu += np.clip(pull, -0.015, 0.0)

            # Hitung log return
            log_returns = (step_mu - 0.5 * sigma ** 2) + sigma * rand_z
            next_price = prev_price * np.exp(log_returns)

            # B. OB Elastic Barrier (Support/Resistance)
            if SMC_MODE:
                if is_long and bull_ob_top > 0.0:
                    # Jika harga LONG menyentuh bagian atas Bullish OB, ada 45% peluang memantul naik
                    entered_ob = (prev_price > bull_ob_top) & (next_price <= bull_ob_top) & (next_price >= bull_ob_bot)
                    bounce = np.random.random(self.n_simulations) < 0.45
                    bounce_shock = np.abs(np.random.normal(1.5, 0.5, self.n_simulations)) * sigma
                    next_price = np.where(entered_ob & bounce, bull_ob_top * np.exp(bounce_shock), next_price)
                elif not is_long and bear_ob_bot > 0.0:
                    # Jika harga SHORT menyentuh bagian bawah Bearish OB, ada 45% peluang memantul turun
                    entered_ob = (prev_price < bear_ob_bot) & (next_price >= bear_ob_bot) & (next_price <= bear_ob_top)
                    bounce = np.random.random(self.n_simulations) < 0.45
                    bounce_shock = -np.abs(np.random.normal(1.5, 0.5, self.n_simulations)) * sigma
                    next_price = np.where(entered_ob & bounce, bear_ob_bot * np.exp(bounce_shock), next_price)

            price_paths[:, t] = next_price

        # ── Evaluasi hasil TP / SL ─────────────────────────────────
        if is_long:
            tp_hit = price_paths >= take_profit
            sl_hit = price_paths <= stop_loss
        else:
            tp_hit = price_paths <= take_profit
            sl_hit = price_paths >= stop_loss

        _INF = n_steps + 1
        tp_any   = tp_hit.any(axis=1)
        tp_first = np.where(tp_any, np.argmax(tp_hit, axis=1), _INF)

        sl_any   = sl_hit.any(axis=1)
        sl_first = np.where(sl_any, np.argmax(sl_hit, axis=1), _INF)

        tp_wins  = tp_any & (tp_first < sl_first)
        sl_loses = sl_any & (sl_first <= tp_first)

        final_prices = price_paths[:, -1]
        if is_long:
            tp_ret   = (take_profit - price) / price
            sl_ret   = (stop_loss   - price) / price
            open_ret = (final_prices - price) / price
        else:
            tp_ret   = (price - take_profit) / price
            sl_ret   = (price - stop_loss)   / price
            open_ret = (price - final_prices) / price

        returns = np.where(tp_wins, tp_ret,
                  np.where(sl_loses, sl_ret, open_ret))

        profitable_paths = int(tp_wins.sum())
        win_probability = profitable_paths / self.n_simulations
        expected_return = float(returns.mean())

        return win_probability, expected_return, profitable_paths

    def _compute_signal_score(
        self,
        features: Dict,
        funding_rate: float,
        htf_features: Optional[Dict] = None,
        oi_change: float = 0.0
    ) -> Tuple[float, str, float, dict]:
        """
        Hitung composite signal score dari semua indicators dengan konfirmasi trend HTF (1h) dan Open Interest.

        Scoring system:
        - Setiap indicator memberikan vote LONG (+1), SHORT (-1), atau NEUTRAL (0)
        - Score = weighted average dari semua votes
        - Direction ditentukan dari net score
        - Sinyal di-filter secara ketat berdasarkan Higher Timeframe (1h) Trend

        Returns:
            (score 0.0-1.0, direction 'LONG'|'SHORT'|'NEUTRAL', agreement_pct 0.0-1.0)
        """
        long_votes = 0.0
        short_votes = 0.0
        active_indicators = 0

        # Helper untuk menambahkan vote
        def add_vote(weight: float, vote: int):
            nonlocal long_votes, short_votes, active_indicators
            if vote == 1:
                long_votes += weight
                active_indicators += 1
            elif vote == -1:
                short_votes += weight
                active_indicators += 1

        # ── 1. Trend signals (EMA 9/21/50 + Price above EMA50) ───────────
        ema_trend = features.get('ema_trend', 0)
        add_vote(2.0, ema_trend)

        above_ema50 = features.get('above_ema50', 0)
        add_vote(1.5, above_ema50)

        # ── 2. MACD (Hist + Cross + Line) ────────────────────────────────
        macd_cross = features.get('macd_cross', 0)
        add_vote(2.0, macd_cross)

        macd_pos = features.get('macd_positive', 0)
        add_vote(1.0, macd_pos)

        # ── 3. Momentum & Divergence (RSI + Stoch) ────────────────────────
        rsi_signal = features.get('rsi_signal', 0)
        add_vote(1.5, rsi_signal)

        stoch_cross = features.get('stoch_cross', 0)
        add_vote(1.5, stoch_cross)

        # Divergences (RSI & MACD) — weight tinggi karena akurasi sangat tinggi
        rsi_div = features.get('rsi_div', 0)
        add_vote(2.0, rsi_div)

        macd_div = features.get('macd_div', 0)
        add_vote(1.5, macd_div)

        # ── 4. Bollinger Bands (Squeeze vs Rebound) ──────────────────────
        bb_signal = features.get('bb_signal', 0)
        add_vote(1.0, bb_signal)

        # ── 5. Volume Confirmation ───────────────────────────────────────
        vol_spike = features.get('vol_spike', 0)
        is_bullish = features.get('is_bullish', 0)
        if vol_spike:
            add_vote(1.5, 1 if is_bullish else -1)

        # ── 6. Funding Rate bias ──────────────────────────────────────────
        if funding_rate < -0.0005:
            add_vote(0.5, 1)  # negative funding is bullish
        elif funding_rate > 0.0005:
            add_vote(0.5, -1) # positive funding is bearish

        # ── 7. Open Interest (OI) Change Confirmation ─────────────────────
        # Jika OI naik signifikan (> 1.5%), itu memvalidasi trend harga saat ini
        if oi_change > 0.015:
            add_vote(1.0, 1 if is_bullish else -1)

        # ── 7.5. Smart Money Concepts (SMC) Confluence ───────────────────
        from config import SMC_MODE
        bos = 0
        choch = 0
        bull_ob_top = 0.0
        bull_ob_bot = 0.0
        bear_ob_top = 0.0
        bear_ob_bot = 0.0
        fvg_dir = 0
        
        if SMC_MODE:
            # BOS & CHoCH votes
            bos = features.get('bos', 0)
            choch = features.get('choch', 0)
            
            if bos == 1:
                add_vote(3.0, 1)   # Bullish BOS (Strong weight!)
            elif bos == -1:
                add_vote(3.0, -1)  # Bearish BOS
                
            if choch == 1:
                add_vote(3.0, 1)   # Bullish CHoCH (Trend Reversal!)
            elif choch == -1:
                add_vote(3.0, -1)  # Bearish CHoCH
                
            # OB Retest Entry votes
            bull_ob_top = features.get('bull_ob_top', 0.0)
            bull_ob_bot = features.get('bull_ob_bot', 0.0)
            bear_ob_top = features.get('bear_ob_top', 0.0)
            bear_ob_bot = features.get('bear_ob_bot', 0.0)
            price = features.get('price', 0.0)
            
            if bull_ob_bot > 0.0 and bull_ob_bot <= price <= bull_ob_top:
                add_vote(2.5, 1)   # Price inside Bullish OB (Retest)
            if bear_ob_top > 0.0 and bear_ob_bot <= price <= bear_ob_top:
                add_vote(2.5, -1)  # Price inside Bearish OB (Retest)
                
            # FVG Imbalance attraction
            fvg_dir = features.get('fvg_dir', 0)
            if fvg_dir == 1:
                add_vote(1.5, 1)   # Attracted to fill bullish gap
            elif fvg_dir == -1:
                add_vote(1.5, -1)  # Attracted to fill bearish gap

        # ── Hitung hasil voting awal ──────────────────────────────────────
        net_votes = long_votes - short_votes
        total_weight = long_votes + short_votes

        if total_weight > 0:
            if net_votes > 0:
                direction = 'LONG'
                score = long_votes / total_weight
                agreement_pct = long_votes / total_weight
            elif net_votes < 0:
                direction = 'SHORT'
                score = short_votes / total_weight
                agreement_pct = short_votes / total_weight
            else:
                direction = 'NEUTRAL'
                score = 0.0
                agreement_pct = 0.0
        else:
            direction = 'NEUTRAL'
            score = 0.0
            agreement_pct = 0.0

        # Normalize score
        score = min(max(score, 0.0), 1.0)

        # Prevent single-indicator inflation: if active_indicators < 3, scale down the score
        if active_indicators < 3 and direction != 'NEUTRAL':
            participation_ratio = active_indicators / 3.0
            score *= participation_ratio

        # Filter minimum composite score
        if score < 0.35:
            direction = 'NEUTRAL'

        # ── 8. Higher Timeframe (HTF) 1h Trend Filter (CRITICAL) ─────────
        if htf_features and direction != 'NEUTRAL':
            htf_ema_trend   = htf_features.get('ema_trend', 0)
            htf_above_ema50 = htf_features.get('above_ema50', 0)

            # Tentukan apakah HTF trend berpotensi berlawanan
            long_vs_htf_bear  = (direction == 'LONG'  and htf_ema_trend == -1)
            short_vs_htf_bull = (direction == 'SHORT' and htf_ema_trend ==  1)

            # ── A. Strict Gatekeeper (blok mutlak) ─────────────────────────
            if HTF_STRICT_GATEKEEPER:
                # Blok 100% jika EMA trend berlawanan
                if long_vs_htf_bear:
                    logger.info(
                        f"[HTF Strict] ❌ LONG DIBLOK — HTF 1H Bearish (ema_trend=-1). "
                        f"No counter-trend trades allowed."
                    )
                    direction = 'NEUTRAL'
                    score = 0.0
                elif short_vs_htf_bull:
                    logger.info(
                        f"[HTF Strict] ❌ SHORT DIBLOK — HTF 1H Bullish (ema_trend=+1). "
                        f"No counter-trend trades allowed."
                    )
                    direction = 'NEUTRAL'
                    score = 0.0
                # Jika HTF sideways (ema_trend==0), cek posisi harga vs EMA50
                elif htf_ema_trend == 0 and direction != 'NEUTRAL':
                    if HTF_REQUIRE_BOTH_CONFIRM:
                        # Mode ketat: harus aligned dengan EMA50 juga
                        if direction == 'LONG' and htf_above_ema50 == -1:
                            logger.info(
                                f"[HTF Strict] ❌ LONG DIBLOK — HTF sideways tapi harga "
                                f"di bawah 1H EMA50 (HTF_REQUIRE_BOTH_CONFIRM)."
                            )
                            direction = 'NEUTRAL'
                            score = 0.0
                        elif direction == 'SHORT' and htf_above_ema50 == 1:
                            logger.info(
                                f"[HTF Strict] ❌ SHORT DIBLOK — HTF sideways tapi harga "
                                f"di atas 1H EMA50 (HTF_REQUIRE_BOTH_CONFIRM)."
                            )
                            direction = 'NEUTRAL'
                            score = 0.0
                    else:
                        # Mode lunak: hanya kurangi score jika HTF sideways
                        if direction == 'LONG' and htf_above_ema50 == -1:
                            logger.debug(f"HTF Filter: LONG diturunkan karena harga di bawah 1h EMA50")
                            score *= 0.8
                        elif direction == 'SHORT' and htf_above_ema50 == 1:
                            logger.debug(f"HTF Filter: SHORT diturunkan karena harga di atas 1h EMA50")
                            score *= 0.8

            # ── B. Mode Lunak (legacy, HTF_STRICT_GATEKEEPER=False) ─────────
            else:
                if long_vs_htf_bear:
                    logger.debug(f"HTF Filter: LONG dibatalkan karena HTF bearish trend (ema_trend=-1)")
                    direction = 'NEUTRAL'
                    score = 0.0
                elif short_vs_htf_bull:
                    logger.debug(f"HTF Filter: SHORT dibatalkan karena HTF bullish trend (ema_trend=1)")
                    direction = 'NEUTRAL'
                    score = 0.0
                elif htf_ema_trend == 0:
                    if direction == 'LONG' and htf_above_ema50 == -1:
                        logger.debug(f"HTF Filter: LONG diturunkan karena harga di bawah 1h EMA50")
                        score *= 0.8
                    elif direction == 'SHORT' and htf_above_ema50 == 1:
                        logger.debug(f"HTF Filter: SHORT diturunkan karena harga di atas 1h EMA50")
                        score *= 0.8

        breakdown = {
            'ema_trend': ema_trend,
            'above_ema50': above_ema50,
            'macd_cross': macd_cross,
            'macd_pos': macd_pos,
            'rsi_signal': rsi_signal,
            'stoch_cross': stoch_cross,
            'rsi_div': rsi_div,
            'macd_div': macd_div,
            'bb_signal': bb_signal,
            'vol_spike': 1 if (vol_spike and is_bullish) else (-1 if (vol_spike and not is_bullish) else 0),
            'funding_rate': funding_rate,
            'oi_change': oi_change,
            'htf_ema_trend': htf_features.get('ema_trend', 0) if htf_features else 0,
            'htf_above_ema50': htf_features.get('above_ema50', 0) if htf_features else 0,
            'bos': bos,
            'choch': choch,
            'fvg_dir': fvg_dir,
            'bull_ob_top': bull_ob_top,
            'bear_ob_top': bear_ob_top,
        }

        return score, direction, agreement_pct, breakdown
