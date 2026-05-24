"""
api_server.py - REST API backend untuk dashboard web
Jalankan: python3 api_server.py
"""

import json
import hmac
import hashlib
import time
import logging
import threading
import requests
from urllib.parse import urlencode
from http.server import HTTPServer, BaseHTTPRequestHandler
from config import API_KEY, API_SECRET, BINANCE_BASE_URL

logger = logging.getLogger(__name__)

# ── In-memory state (diupdate oleh scanner) ──────────────────────────
_state = {
    'signals':        [],   # List of recent signals
    'trades':         [],   # List of executed trades
    'scan_count':     0,
    'last_scan':      None,
    'stats': {
        'total_trades': 0,
        'win_trades':   0,
        'loss_trades':  0,
        'total_pnl':    0.0,
        'win_rate':     0.0,
    }
}
_state_lock = threading.Lock()

# ── Binance data cache (diupdate background thread setiap 10 detik) ───
_CACHE_TTL = 10   # detik
_cache = {
    'balance':     {'balance': 0, 'available': 0, 'unrealized': 0},
    'positions':   [],
    'algo_orders': [],
}
_cache_lock = threading.Lock()

# ── Node graph state (diupdate tiap scan selesai) ─────────────────────
_nodes = []   # list of node dicts
_edges = []   # list of edge dicts
_node_lock = threading.Lock()


def update_nodes(nodes: list, edges: list):
    with _node_lock:
        global _nodes, _edges
        _nodes = nodes
        _edges = edges


def update_state(key, value):
    with _state_lock:
        _state[key] = value


def append_signal(signal_dict):
    with _state_lock:
        _state['signals'].insert(0, signal_dict)
        _state['signals'] = _state['signals'][:100]  # Keep last 100


def append_trade(trade_dict):
    with _state_lock:
        _state['trades'].insert(0, trade_dict)
        _state['trades'] = _state['trades'][:50]


# ── Binance data helpers ──────────────────────────────────────────────

def _signed_get(path, params=None):
    params = params or {}
    params['timestamp'] = int(time.time() * 1000)
    params['recvWindow'] = 60000
    query = urlencode(params)
    sig = hmac.new(API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
    url = f"{BINANCE_BASE_URL}{path}?{query}&signature={sig}"
    resp = requests.get(url, headers={'X-MBX-APIKEY': API_KEY}, timeout=10)
    if resp.status_code != 200:
        try:
            err = resp.json()
            code = err.get('code', resp.status_code)
            msg  = err.get('msg', resp.text)
            raise Exception(f"Binance API error {code}: {msg}")
        except ValueError:
            raise Exception(f"HTTP {resp.status_code}: {resp.text}")
    return resp.json()


def _fetch_balance():
    """Fetch balance dari Binance (dipanggil dari background cache thread)."""
    data = _signed_get('/fapi/v2/balance')
    if not isinstance(data, list):
        raise Exception(f"Expected list from /fapi/v2/balance, got {type(data).__name__}")
    for a in data:
        if a.get('asset') == 'USDT':
            wallet_balance = float(a.get('balance', 0))
            available      = float(a.get('availableBalance', 0))
            positions = _signed_get('/fapi/v2/positionRisk')
            if not isinstance(positions, list):
                raise Exception(f"Expected list from /fapi/v2/positionRisk, got {type(positions).__name__}")
            unrealized = sum(
                float(p.get('unRealizedProfit', 0))
                for p in positions
                if float(p.get('positionAmt', 0)) != 0
            )
            return {'balance': wallet_balance, 'available': available, 'unrealized': unrealized}
    return {'balance': 0, 'available': 0, 'unrealized': 0}


def _fetch_positions():
    """Fetch posisi terbuka dari Binance (dipanggil dari background cache thread)."""
    data = _signed_get('/fapi/v2/positionRisk')
    if not isinstance(data, list):
        raise Exception(f"Expected list from /fapi/v2/positionRisk, got {type(data).__name__}")
    return [
        {
            'symbol':      p['symbol'],
            'side':        'LONG' if float(p['positionAmt']) > 0 else 'SHORT',
            'qty':         abs(float(p['positionAmt'])),
            'entry':       float(p['entryPrice']),
            'mark':        float(p['markPrice']),
            'pnl':         float(p['unRealizedProfit']),
            'pnl_pct':     (float(p['unRealizedProfit']) / float(p['isolatedWallet'])) * 100
                           if float(p.get('isolatedWallet', 0)) > 0 else 0,
            'leverage':    int(p['leverage']),
            'margin':      float(p.get('isolatedWallet', 0)),
            'liq_price':   float(p['liquidationPrice']),
            'margin_type': p.get('marginType', 'isolated'),
        }
        for p in data if float(p.get('positionAmt', 0)) != 0
    ]


def _fetch_algo_orders():
    """Fetch open algo orders dari Binance (dipanggil dari background cache thread)."""
    data = _signed_get('/fapi/v1/openAlgoOrders')
    return data if isinstance(data, list) else []


def run_cache_updater():
    """
    Background thread: update cache balance, positions, dan algo orders
    dari Binance setiap _CACHE_TTL detik.
    Ini mencegah dashboard membanjiri Binance API setiap 2 detik.
    """
    logger.info(f"[Cache] Background cache updater dimulai (interval={_CACHE_TTL}s)")
    while True:
        try:
            balance = _fetch_balance()
            with _cache_lock:
                _cache['balance'] = balance
        except Exception as e:
            logger.error(f"Balance error: {e}")

        try:
            positions = _fetch_positions()
            with _cache_lock:
                _cache['positions'] = positions
        except Exception as e:
            logger.error(f"Positions error: {e}")

        try:
            algo_orders = _fetch_algo_orders()
            with _cache_lock:
                _cache['algo_orders'] = algo_orders
        except Exception as e:
            logger.error(f"Algo orders error: {e}")

        time.sleep(_CACHE_TTL)


def get_balance():
    """Kembalikan balance dari cache."""
    with _cache_lock:
        return _cache['balance'].copy()


def get_positions():
    """Kembalikan posisi terbuka dari cache."""
    with _cache_lock:
        return list(_cache['positions'])


def get_algo_orders():
    """Kembalikan open algo orders dari cache."""
    with _cache_lock:
        return list(_cache['algo_orders'])


# ── HTTP Handler ──────────────────────────────────────────────────────

class DashboardHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        pass  # Suppress default access log

    def do_GET(self):
        path = self.path.split('?')[0]

        if path == '/':
            self._serve_file('dashboard.html', 'text/html')
        elif path == '/api/state':
            try:
                import scanner
                import database as db
                cb = scanner.CircuitBreaker()
                is_paused, risk_mult, cb_reason = cb.check()
                consecutive_losses = db.get_consecutive_losses()
            except Exception as e:
                is_paused, risk_mult, cb_reason = False, 1.0, f"Error: {e}"
                consecutive_losses = 0

            self._json({
                **_state,
                'balance':   get_balance(),
                'positions': get_positions(),
                'algo_orders': get_algo_orders(),
                'timestamp': int(time.time() * 1000),
                'circuit_breaker': {
                    'is_paused': is_paused,
                    'risk_multiplier': risk_mult,
                    'reason': cb_reason,
                    'consecutive_losses': consecutive_losses
                }
            })
        elif path == '/api/balance':
            self._json(get_balance())
        elif path == '/api/positions':
            self._json(get_positions())
        elif path == '/api/nodes':
            with _node_lock:
                self._json({'nodes': _nodes, 'edges': _edges})
        elif path == '/api/pair-stats':
            import database as db
            symbol = None
            if '?' in self.path:
                from urllib.parse import parse_qs
                params = parse_qs(self.path.split('?')[1])
                symbol = params.get('symbol', [None])[0]
            self._json(db.get_pair_stats(symbol))
        elif path == '/api/session-stats':
            import database as db
            self._json(db.get_session_stats())
        elif path == '/api/setup-stats':
            import database as db
            self._json(db.get_setup_stats())
        elif path == '/api/oi-price-stats':
            import database as db
            self._json(db.get_oi_price_stats())
        elif path == '/api/ticker':
            try:
                import requests
                resp = requests.get('https://fapi.binance.com/fapi/v1/ticker/24hr', timeout=5)
                if resp.status_code == 200:
                    self._json(resp.json())
                else:
                    self._json([])
            except Exception as e:
                logger.error(f"Error fetching ticker: {e}")
                self._json([])
        elif path == '/api/hourly-stats':
            import database as db
            self._json(db.get_hourly_stats())
        elif path == '/api/backtest-stats':
            import sqlite3
            import os
            try:
                db_path = os.path.join(os.path.dirname(__file__), 'pair_statistics.db')
                if os.path.exists(db_path):
                    conn = sqlite3.connect(db_path)
                    rows = conn.execute("SELECT * FROM pair_stats ORDER BY win_rate DESC").fetchall()
                    cols = ['symbol', 'total_trades', 'wins', 'losses', 'win_rate', 'profit_factor', 'last_updated']
                    res = [dict(zip(cols, r)) for r in rows]
                    conn.close()
                    self._json(res)
                else:
                    self._json([])
            except Exception as e:
                self._json({'error': str(e)})
        elif path.startswith('/api/intelligence/'):
            parts = path.split('/')
            symbol = parts[-1] if len(parts) > 3 else ''
            import database as db
            self._json(db.get_intelligence_by_symbol(symbol))
        else:
            self.send_response(404)
            self.end_headers()

    def _json(self, data):
        body = json.dumps(data, default=str).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Content-Length', len(body))
        self.end_headers()
        self.wfile.write(body)

    def _serve_file(self, filename, content_type):
        try:
            with open(filename, 'rb') as f:
                body = f.read()
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', len(body))
            self.end_headers()
            self.wfile.write(body)
        except FileNotFoundError:
            self.send_response(404)
            self.end_headers()


def start_server(port=8000):
    # Mulai background cache updater thread
    cache_thread = threading.Thread(target=run_cache_updater, daemon=True)
    cache_thread.start()

    for attempt_port in range(port, port + 10):
        try:
            server = HTTPServer(('0.0.0.0', attempt_port), DashboardHandler)
            logger.info(f"Dashboard server running on http://0.0.0.0:{attempt_port}")
            server.serve_forever()
            return
        except OSError:
            logger.warning(f"Port {attempt_port} sudah dipakai, coba {attempt_port + 1}...")
    logger.error("Tidak bisa bind ke port manapun (8000-8009), dashboard tidak jalan.")


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    start_server()
