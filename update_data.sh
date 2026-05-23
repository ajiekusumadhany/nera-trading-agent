#!/bin/bash
# Skrip untuk men-download/memperbarui data 3 bulan terakhir
# Jalankan skrip ini dari direktori utama proyek

echo "Starting 3-month data download/update..."
python3 backtest_data.py > data_update.log 2>&1 &
echo "Data download started in the background. Check data_update.log for progress."
