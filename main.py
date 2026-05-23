"""
main.py - Entry point NERA QUANT Trading AI
============================================
Jalankan: python3 main.py
"""

import logging
import sys
import threading
import time
import colorlog
from scanner import NeraScanner
from api_server import start_server
import database

DASHBOARD_PORT = 8000


def setup_logging():
    """Setup colored logging ke console dan file."""
    console_handler = colorlog.StreamHandler()
    console_handler.setFormatter(colorlog.ColoredFormatter(
        '%(log_color)s%(asctime)s [%(levelname)s]%(reset)s %(message)s',
        datefmt='%H:%M:%S',
        log_colors={
            'DEBUG':    'cyan',
            'INFO':     'green',
            'WARNING':  'yellow',
            'ERROR':    'red',
            'CRITICAL': 'red,bg_white',
        }
    ))
    file_handler = logging.FileHandler('nera_quant.log', encoding='utf-8')
    file_handler.setFormatter(logging.Formatter(
        '%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    ))
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)
    logging.getLogger('urllib3').setLevel(logging.WARNING)
    logging.getLogger('requests').setLevel(logging.WARNING)


def _run_weekly_retrospective_loop():
    """
    Background thread: jalankan AI weekly retrospective setiap 7 hari.
    Pertama kali dijalankan setelah 7 hari uptime, lalu setiap 7 hari berikutnya.
    """
    logger = logging.getLogger('retrospective')
    SEVEN_DAYS = 7 * 24 * 3600
    logger.info("[Retrospective] Scheduler started — akan berjalan setiap 7 hari.")
    time.sleep(SEVEN_DAYS)
    while True:
        try:
            from analytics_engine import run_weekly_ai_retrospective
            logger.info("[Retrospective] Menjalankan AI Weekly Retrospective...")
            run_weekly_ai_retrospective()
        except Exception as e:
            logger.error(f"[Retrospective] Error: {e}")
        time.sleep(SEVEN_DAYS)


def main():
    setup_logging()
    logger = logging.getLogger(__name__)

    logger.info("╔══════════════════════════════════════════╗")
    logger.info("║       NERA QUANT - Trading AI v1.0       ║")
    logger.info("║   Monte Carlo Probability Engine         ║")
    logger.info("║   Top 50 Binance Pairs Scanner           ║")
    logger.info("╚══════════════════════════════════════════╝")

    # Start dashboard web server di background thread
    web_thread = threading.Thread(
        target=start_server,
        args=(DASHBOARD_PORT,),
        daemon=True
    )
    web_thread.start()
    logger.info(f"Dashboard: http://localhost:{DASHBOARD_PORT}")

    # Start database sync thread (trade history dari Binance)
    db_thread = threading.Thread(target=database.run_sync_loop, daemon=True)
    db_thread.start()
    logger.info("Database sync: started")

    # Start weekly AI retrospective scheduler
    retro_thread = threading.Thread(target=_run_weekly_retrospective_loop, daemon=True)
    retro_thread.start()
    logger.info("Weekly retrospective scheduler: started")

    try:
        scanner = NeraScanner()
        scanner.run_forever()
    except KeyboardInterrupt:
        logger.info("Shutdown requested. Goodbye!")
        sys.exit(0)
    except Exception as e:
        logger.critical(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == '__main__':
    main()
