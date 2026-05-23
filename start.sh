#!/bin/bash
# ============================================================
# NERA QUANT - Start Script
# ============================================================

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PIDFILE="$DIR/nera_quant.pid"
LOGFILE="$DIR/nera_quant.log"

# Cek apakah sudah jalan
if [ -f "$PIDFILE" ]; then
    PID=$(cat "$PIDFILE")
    if kill -0 "$PID" 2>/dev/null; then
        echo "⚠️  NERA QUANT sudah jalan (PID: $PID)"
        echo "   Gunakan: bash stop.sh untuk menghentikan"
        exit 1
    else
        rm -f "$PIDFILE"
    fi
fi

echo "🚀 Starting NERA QUANT..."
nohup python3 "$DIR/main.py" >> "$LOGFILE" 2>&1 &
PID=$!
echo $PID > "$PIDFILE"
echo "✅ Jalan di background (PID: $PID)"
echo "📄 Log: tail -f $LOGFILE"
