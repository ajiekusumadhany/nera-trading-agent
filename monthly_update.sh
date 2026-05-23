#!/bin/bash
# Skrip untuk update data dan backtest bulanan secara otomatis

# Dapatkan direktori skrip ini dijalankan
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"

echo "========================================="
echo "Memulai Update Bulanan: $(date)"
echo "========================================="

# Jalankan update data dan tunggu sampai selesai
echo "[1/2] Menjalankan download/update data..."
# Menjalankan di foreground dan menunggu selesai
python3 "$DIR/backtest_data.py" > "$DIR/monthly_data_update.log" 2>&1
echo "Update data selesai."

# Jalankan backtest dan tunggu sampai selesai
echo "[2/2] Menjalankan backtest 3 bulan..."
# Menjalankan di foreground dan menunggu selesai
python3 "$DIR/backtester.py" > "$DIR/monthly_backtest.log" 2>&1
echo "Backtest selesai."

echo "========================================="
echo "Update Bulanan Selesai: $(date)"
echo "========================================="
