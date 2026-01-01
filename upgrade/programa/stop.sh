#!/data/data/com.termux/files/usr/bin/bash

APP_DIR="$HOME/storage/shared/Android/media/com.termux/programa"
cd "$APP_DIR" || exit 1

# Tenta matar o server.py iniciado pelo start.sh
pkill -f "python3 server.py" 2>/dev/null || echo "nada para parar"

echo "[Servidor] parado"
