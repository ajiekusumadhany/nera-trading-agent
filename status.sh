#!/bin/bash
# ============================================================
# NERA QUANT - Status Script
# ============================================================

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PIDFILE="$DIR/nera_quant.pid"
LOGFILE="$DIR/nera_quant.log"

if [ -f "$PIDFILE" ]; then
    PID=$(cat "$PIDFILE")
    if kill -0 "$PID" 2>/dev/null; then
        echo "✅ NERA QUANT RUNNING (PID: $PID)"
        echo ""
        echo "── Last 20 log lines ──────────────────────────"
        tail -20 "$LOGFILE"
    else
        echo "❌ NERA QUANT STOPPED (PID file ada tapi proses mati)"
        rm -f "$PIDFILE"
    fi
else
    echo "❌ NERA QUANT NOT RUNNING"
fi
