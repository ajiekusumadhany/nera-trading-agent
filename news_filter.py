"""
news_filter.py - Macro News Blackout Filter untuk NERA QUANT
=============================================================
Fetches high-impact economic calendar events (CPI, FOMC, NFP, dll)
dari feed publik FairEconomy/ForexFactory dan menerapkan blackout window:
  - 30 menit SEBELUM berita High Impact
  - 15 menit SETELAH berita High Impact

Selama blackout aktif:
  - Tidak ada posisi baru yang dibuka
  - SL semua posisi aktif dipindah ke Breakeven (circuit breaker mode)
"""

import logging
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Optional

import requests

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Konfigurasi
# ──────────────────────────────────────────────────────────────────────────────
NEWS_CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"
HIGH_IMPACT_CURRENCIES = {"USD", "EUR", "GBP", "JPY", "CNY", "AUD", "CAD", "CHF"}
HIGH_IMPACT_LABELS     = {"High"}          # nilai dari tag <impact>
BLACKOUT_BEFORE_MINS   = 30               # menit sebelum berita
BLACKOUT_AFTER_MINS    = 15               # menit setelah berita
REFRESH_INTERVAL_SECS  = 3600             # refresh calendar setiap 1 jam
REQUEST_TIMEOUT_SECS   = 15


class NewsEvent:
    """Satu entri berita ekonomi berdampak tinggi."""
    __slots__ = ('title', 'currency', 'event_time', 'impact')

    def __init__(self, title: str, currency: str, event_time: datetime, impact: str):
        self.title      = title
        self.currency   = currency
        self.event_time = event_time   # UTC-aware datetime
        self.impact     = impact

    def __repr__(self):
        return (f"NewsEvent(title={self.title!r}, currency={self.currency}, "
                f"time={self.event_time.strftime('%Y-%m-%d %H:%M UTC')}, impact={self.impact})")


class NewsBlackoutFilter:
    """
    Singleton-style filter yang me-refresh daftar berita High Impact secara
    berkala di background thread dan menyediakan metode:

      is_blackout_active()  → bool
      get_next_event()      → Optional[NewsEvent]
    """

    def __init__(self):
        self._lock      = threading.Lock()
        self._events: List[NewsEvent] = []
        self._last_fetch: Optional[datetime] = None
        self._fetch_errors = 0
        # Fetch langsung saat startup (non-blocking — lewat thread)
        self._bg_thread = threading.Thread(
            target=self._refresh_loop, daemon=True, name="NewsFilter-Refresh"
        )
        self._bg_thread.start()
        logger.info("[NewsFilter] Background refresh thread dimulai.")

    # ──────────────────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────────────────

    def is_blackout_active(self) -> bool:
        """
        Kembalikan True jika sekarang berada di dalam window blackout
        (30 menit sebelum atau 15 menit setelah berita High Impact), atau jika Gemini mendeteksi FUD.
        """
        if getattr(self, '_fud_active', False):
            logger.info("[NewsFilter] 🚨 BLACKOUT AKTIF! Gemini mendeteksi FUD/Bearish Macro Bias dari X.com.")
            return True

        now = datetime.now(timezone.utc)
        with self._lock:
            for ev in self._events:
                window_start = ev.event_time - timedelta(minutes=BLACKOUT_BEFORE_MINS)
                window_end   = ev.event_time + timedelta(minutes=BLACKOUT_AFTER_MINS)
                if window_start <= now <= window_end:
                    logger.info(
                        f"[NewsFilter] 🚨 BLACKOUT AKTIF! Berita: '{ev.title}' ({ev.currency}) "
                        f"pukul {ev.event_time.strftime('%H:%M UTC')}"
                    )
                    return True
        return False

    def get_upcoming_events(self, within_minutes: int = 60) -> List[NewsEvent]:
        """Kembalikan daftar berita yang akan datang dalam X menit ke depan."""
        now = datetime.now(timezone.utc)
        cutoff = now + timedelta(minutes=within_minutes)
        with self._lock:
            return [
                ev for ev in self._events
                if now <= ev.event_time <= cutoff
            ]

    def get_next_event(self) -> Optional[NewsEvent]:
        """Kembalikan berita High Impact berikutnya (atau None)."""
        now = datetime.now(timezone.utc)
        with self._lock:
            future = [ev for ev in self._events if ev.event_time > now]
            return min(future, key=lambda e: e.event_time) if future else None

    def force_refresh(self):
        """Paksa refresh calendar sekarang (dari luar thread)."""
        self._fetch_calendar()

    # ──────────────────────────────────────────────────────────────
    # Internal
    # ──────────────────────────────────────────────────────────────

    def _refresh_loop(self):
        """Loop background yang me-refresh calendar setiap REFRESH_INTERVAL_SECS."""
        # Tunggu sebentar agar logging sudah siap
        time.sleep(3)
        while True:
            try:
                self._fetch_calendar()
                self._fetch_twitter_and_analyze()
            except Exception as e:
                logger.error(f"[NewsFilter] Error refresh loop: {e}")
            time.sleep(REFRESH_INTERVAL_SECS)

    def _fetch_twitter_and_analyze(self):
        try:
            from config import ENABLE_TWITTER_SCRAPE
            if not ENABLE_TWITTER_SCRAPE:
                return
        except ImportError:
            return

        try:
            from ntscraper import Nitter
            import gemini_client
            
            logger.info("[NewsFilter] Scraping X.com for macro sentiment...")
            scraper = Nitter(log_level=1)
            # Tier10k is a good source for crypto/macro news
            tweets = scraper.get_tweets("tier10k", mode='user', number=5)
            
            tweet_texts = []
            if tweets and 'tweets' in tweets:
                for t in tweets['tweets']:
                    tweet_texts.append(t['text'])
                    
            if not tweet_texts:
                logger.warning("[NewsFilter] No tweets found or scraper failed.")
                return
                
            context = "You are a Chief Economist AI. Analyze these latest financial tweets. If they contain massive FUD, war, high inflation, or crypto bans, reply with ONLY the word 'FUD'. If they are normal, reply 'NEUTRAL'. If very bullish, reply 'BULLISH'."
            prompt = "\n".join(tweet_texts)
            
            analysis = gemini_client.ask_gemini_text(prompt, context)
            logger.info(f"[NewsFilter] Gemini Twitter Analysis: {analysis}")
            
            with self._lock:
                self._macro_bias = analysis.strip().upper()
                if "FUD" in self._macro_bias:
                    self._fud_active = True
                else:
                    self._fud_active = False
                    
        except Exception as e:
            logger.error(f"[NewsFilter] Twitter scrape / Gemini error: {e}")

    def _fetch_calendar(self):
        """Fetch dan parse XML calendar dari FairEconomy."""
        try:
            logger.info("[NewsFilter] Fetching economic calendar...")
            resp = requests.get(
                NEWS_CALENDAR_URL,
                timeout=REQUEST_TIMEOUT_SECS,
                headers={"User-Agent": "NeraQuant/1.0"},
            )
            resp.raise_for_status()
            events = self._parse_xml(resp.text)
            with self._lock:
                self._events = events
                self._last_fetch = datetime.now(timezone.utc)
                self._fetch_errors = 0
            logger.info(
                f"[NewsFilter] ✅ Calendar diperbarui: {len(events)} berita High Impact ditemukan."
            )
            # Log 5 berita terdekat
            now = datetime.now(timezone.utc)
            upcoming = sorted(
                [e for e in events if e.event_time > now],
                key=lambda e: e.event_time
            )[:5]
            for ev in upcoming:
                delta = (ev.event_time - now).total_seconds() / 60
                logger.info(
                    f"  📅 {ev.title} ({ev.currency}) → {ev.event_time.strftime('%a %d %H:%M UTC')} "
                    f"[dalam {delta:.0f} menit]"
                )
        except requests.RequestException as e:
            self._fetch_errors += 1
            logger.warning(
                f"[NewsFilter] ⚠️ Gagal fetch calendar (percobaan ke-{self._fetch_errors}): {e}"
            )
        except Exception as e:
            self._fetch_errors += 1
            logger.error(f"[NewsFilter] Error parsing calendar: {e}")

    def _parse_xml(self, xml_text: str) -> List[NewsEvent]:
        """Parse XML ForexFactory/FairEconomy → list NewsEvent High Impact."""
        events = []
        root = ET.fromstring(xml_text)
        today = datetime.now(timezone.utc)

        for event_el in root.findall('event'):
            try:
                impact   = (event_el.findtext('impact') or '').strip()
                country  = (event_el.findtext('country') or '').strip().upper()
                title    = (event_el.findtext('title') or '').strip()
                date_str = (event_el.findtext('date') or '').strip()   # MM-DD-YYYY
                time_str = (event_el.findtext('time') or '').strip()   # HH:MMam / HH:MMpm

                # Filter impact dan currency
                if impact not in HIGH_IMPACT_LABELS:
                    continue
                if country not in HIGH_IMPACT_CURRENCIES:
                    continue

                # Parse tanggal + waktu
                event_time = self._parse_event_datetime(date_str, time_str, today.year)
                if event_time is None:
                    continue

                events.append(NewsEvent(
                    title=title,
                    currency=country,
                    event_time=event_time,
                    impact=impact,
                ))
            except Exception:
                continue

        return events

    def _parse_event_datetime(
        self, date_str: str, time_str: str, current_year: int
    ) -> Optional[datetime]:
        """
        Parse format ForexFactory:
          date_str: 'MM-DD-YYYY'  (misal '05-22-2026')
          time_str: 'H:MMam'/'H:MMpm' atau '12:30am' (timezone: GMT)
        Kembalikan UTC-aware datetime, atau None jika gagal parse.
        """
        try:
            # Parse date
            dt_date = datetime.strptime(date_str, "%m-%d-%Y")
        except ValueError:
            return None

        if not time_str or time_str.lower() in ('', 'all day', 'tentative', 'n/a'):
            # Waktu tidak pasti → asumsikan 00:00 UTC sebagai placeholder
            return datetime(
                dt_date.year, dt_date.month, dt_date.day, 0, 0,
                tzinfo=timezone.utc
            )

        # Coba berbagai format
        for fmt in ("%I:%M%p", "%I:%M %p", "%H:%M"):
            try:
                t = datetime.strptime(time_str.lower().strip(), fmt)
                return datetime(
                    dt_date.year, dt_date.month, dt_date.day,
                    t.hour, t.minute,
                    tzinfo=timezone.utc
                )
            except ValueError:
                continue

        # Gagal parse waktu — kembalikan None agar event di-skip
        return None


# ──────────────────────────────────────────────────────────────────────────────
# Module-level singleton agar hanya ada satu instance
# ──────────────────────────────────────────────────────────────────────────────
_news_filter_instance: Optional[NewsBlackoutFilter] = None
_instance_lock = threading.Lock()


def get_news_filter() -> NewsBlackoutFilter:
    """Kembalikan singleton NewsBlackoutFilter. Thread-safe."""
    global _news_filter_instance
    if _news_filter_instance is None:
        with _instance_lock:
            if _news_filter_instance is None:
                _news_filter_instance = NewsBlackoutFilter()
    return _news_filter_instance
