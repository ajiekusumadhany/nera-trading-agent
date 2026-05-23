import os
import logging
from typing import Optional
from config import GEMINI_API_KEY
from google import genai
from PIL import Image

logger = logging.getLogger(__name__)

# Initialize client
try:
    client = genai.Client(api_key=GEMINI_API_KEY)
except Exception as e:
    logger.error(f"Failed to initialize Gemini Client: {e}")
    client = None

def ask_gemini_text(prompt: str, context: str = "") -> str:
    """Send text and context to Gemini 2.5 Pro for analysis."""
    if not client:
        return "ERROR: Gemini Client not initialized."
    try:
        full_prompt = f"Context: {context}\n\nTask: {prompt}" if context else prompt
        response = client.models.generate_content(
            model='gemini-2.5-pro',
            contents=full_prompt,
        )
        return response.text
    except Exception as e:
        logger.error(f"Gemini text error: {e}")
        return f"ERROR: {e}"

def ask_gemini_vision(prompt: str, image_path: str) -> str:
    """Send an image and prompt to Gemini 2.5 Pro for visual analysis."""
    if not client:
        return "ERROR: Gemini Client not initialized."
    try:
        if not os.path.exists(image_path):
            return f"ERROR: Image not found at {image_path}"
            
        img = Image.open(image_path)
        response = client.models.generate_content(
            model='gemini-2.5-pro',
            contents=[prompt, img]
        )
        return response.text
    except Exception as e:
        logger.error(f"Gemini vision error: {e}")
        return f"ERROR: {e}"


# ─────────────────────────────────────────────────────────────────────
# Feature 1: Multi-Analyst Debate (Bull vs Bear)
# ─────────────────────────────────────────────────────────────────────

def ask_gemini_debate(
    symbol: str,
    direction: str,
    context_str: str,
    chart_path: Optional[str] = None,
    similar_patterns_str: str = "",
) -> dict:
    """
    Run a bull-vs-bear analyst debate via two separate Gemini calls.
    Returns {'bull': str, 'bear': str, 'verdict': 'APPROVE'|'REJECT', 'reasoning': str}

    The bull analyst argues FOR the trade, the bear analyst argues AGAINST.
    Final verdict: APPROVE only if bull wins (bear fails to find a strong reason to reject).
    """
    if not client:
        return {'bull': 'ERROR', 'bear': 'ERROR', 'verdict': 'APPROVE', 'reasoning': 'Gemini unavailable, defaulting to APPROVE'}

    rag_context = f"\n\nHistorical similar setups:\n{similar_patterns_str}" if similar_patterns_str else ""

    base_context = (
        f"You are analyzing a {direction} trade on {symbol}.\n"
        f"Setup details: {context_str}{rag_context}\n"
    )

    # ── Bull Analyst ──────────────────────────────────────────────────
    bull_prompt = (
        f"{base_context}"
        f"You are the BULL ANALYST. Your job is to find the strongest reasons WHY this {direction} trade SHOULD be taken. "
        f"Focus on: trend alignment, momentum, SMC confluence, risk/reward. "
        f"Be concise (3-4 sentences). Gunakan Bahasa Indonesia. End with: BULL_VERDICT: STRONG | MODERATE | WEAK"
    )

    # ── Bear Analyst ──────────────────────────────────────────────────
    bear_prompt = (
        f"{base_context}"
        f"You are the BEAR ANALYST (skeptic). Your job is to find the strongest reasons WHY this {direction} trade SHOULD BE REJECTED. "
        f"Focus on: counter-trend risks, weak confluence, poor R/R, news risk, overextension. "
        f"Be concise (3-4 sentences). Gunakan Bahasa Indonesia. End with: BEAR_VERDICT: STRONG | MODERATE | WEAK"
    )

    try:
        if chart_path and os.path.exists(chart_path):
            img = Image.open(chart_path)
            bull_resp = client.models.generate_content(
                model='gemini-2.5-pro',
                contents=[bull_prompt, img]
            ).text
            bear_resp = client.models.generate_content(
                model='gemini-2.5-pro',
                contents=[bear_prompt, img]
            ).text
        else:
            bull_resp = client.models.generate_content(
                model='gemini-2.5-pro', contents=bull_prompt
            ).text
            bear_resp = client.models.generate_content(
                model='gemini-2.5-pro', contents=bear_prompt
            ).text

        # Parse verdicts
        bull_strength = 'WEAK'
        bear_strength = 'WEAK'
        for line in bull_resp.upper().split('\n'):
            if 'BULL_VERDICT:' in line:
                if 'STRONG' in line:
                    bull_strength = 'STRONG'
                elif 'MODERATE' in line:
                    bull_strength = 'MODERATE'
        for line in bear_resp.upper().split('\n'):
            if 'BEAR_VERDICT:' in line:
                if 'STRONG' in line:
                    bear_strength = 'STRONG'
                elif 'MODERATE' in line:
                    bear_strength = 'MODERATE'

        # Decision logic:
        # REJECT if bear is STRONG and bull is not STRONG
        # APPROVE otherwise
        if bear_strength == 'STRONG' and bull_strength != 'STRONG':
            verdict = 'REJECT'
            reasoning = f"Bear analyst raised strong objections (bear={bear_strength}, bull={bull_strength})"
        else:
            verdict = 'APPROVE'
            reasoning = f"Bull case prevails (bull={bull_strength}, bear={bear_strength})"

        logger.info(f"[Debate] {symbol} {direction} | Bull={bull_strength} Bear={bear_strength} → {verdict}")
        return {
            'bull': bull_resp,
            'bear': bear_resp,
            'verdict': verdict,
            'reasoning': reasoning,
            'bull_strength': bull_strength,
            'bear_strength': bear_strength,
        }

    except Exception as e:
        logger.error(f"[Debate] Gemini debate error: {e}")
        return {'bull': f'ERROR: {e}', 'bear': f'ERROR: {e}', 'verdict': 'APPROVE', 'reasoning': 'Error in debate, defaulting to APPROVE'}


# ─────────────────────────────────────────────────────────────────────
# Feature 4: L3 Meta-Feedback (Gemini evaluates its own CIO quality)
# ─────────────────────────────────────────────────────────────────────

def ask_gemini_meta_eval(
    symbol: str,
    direction: str,
    cio_verdict: str,
    outcome: str,
    bull_reasoning: str = "",
    bear_reasoning: str = "",
) -> str:
    """
    L3 meta-feedback: evaluate whether the CIO debate verdict was correct
    given the actual trade outcome.

    Returns a short evaluation string stored in trade_intelligence.meta_feedback.
    """
    if not client:
        return "ERROR: Gemini unavailable"
    try:
        prompt = (
            f"You are a meta-evaluator for an AI trading system's CIO approval process.\n\n"
            f"Trade: {symbol} {direction}\n"
            f"CIO Verdict: {cio_verdict}\n"
            f"Actual Outcome: {outcome}\n"
            f"Bull Analyst said: {bull_reasoning[:300]}\n"
            f"Bear Analyst said: {bear_reasoning[:300]}\n\n"
            f"Was the CIO verdict correct? What did the analysts miss or get right? "
            f"Give a 2-3 sentence evaluation. Gunakan Bahasa Indonesia. End with: META_QUALITY: GOOD | ACCEPTABLE | POOR"
        )
        resp = client.models.generate_content(
            model='gemini-2.5-pro', contents=prompt
        ).text
        logger.info(f"[MetaEval] {symbol} {direction} | CIO={cio_verdict} Outcome={outcome}")
        return resp
    except Exception as e:
        logger.error(f"[MetaEval] Error: {e}")
        return f"ERROR: {e}"
