#!/bin/bash
# Skrip untuk menjalankan backtest 3 bulan terakhir
# Jalankan skrip ini dari direktori utama proyek

echo "Starting 3-month backtest update..."
python3 backtester.py > backtest_update.log 2>&1 &
echo "Backtest started in the background. Check backtest_update.log for progress."
