#!/bin/bash
# ============================================================
# NERA QUANT - Stop Script
# ============================================================

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PIDFILE="$DIR/nera_quant.pid"

# Kill via PID file
if [ -f "$PIDFILE" ]; then
    PID=$(cat "$PIDFILE")
    if kill -0 "$PID" 2>/dev/null; then
        kill "$PID"
        echo "🛑 Stopped PID $PID"
    fi
    rm -f "$PIDFILE"
fi

# Kill semua proses main.py yang masih jalan (bersihkan proses lama)
LEFTOVER=$(pgrep -f "python3.*main.py" 2>/dev/null)
if [ -n "$LEFTOVER" ]; then
    echo "🧹 Membersihkan proses lama: $LEFTOVER"
    kill $LEFTOVER 2>/dev/null
    sleep 1
    kill -9 $LEFTOVER 2>/dev/null
fi

echo "✅ Semua proses NERA QUANT dihentikan"
