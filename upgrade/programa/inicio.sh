#!/data/data/com.termux/files/usr/bin/sh
# Script principal do Painel (roda no Termux ou pelo Termux:Boot)

# Mantém o aparelho acordado
termux-wake-lock || true

# Pasta do projeto
APP_DIR="$HOME/storage/shared/Android/media/com.termux/programa"
cd "$APP_DIR" || exit 1

# Porta do painel
export PANEL_PORT=8080
PORT="$PANEL_PORT"

# ================= LOGS =================
mkdir -p logs

SERVER_LOG="logs/server.log"
ESP_LOG="logs/esp.log"

# Limpa logs antigos (sem acumular sujeira)
rm -f "$SERVER_LOG" "$ESP_LOG"
touch "$SERVER_LOG" "$ESP_LOG"


# ================= SERVIDOR =================
# Evita instância duplicada do servidor
pkill -f "server.py" 2>/dev/null || true

# Inicia o server.py mandando tudo para server.log (stdout + stderr)
python3 server.py >> "$SERVER_LOG" 2>&1 &

# Dá um tempo para o servidor subir
sleep 3

# Descobre IP da LAN (wlan0 ou eth0)
IP=$(ip -o -4 addr show wlan0 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -n1)
[ -z "$IP" ] && IP=$(ip -o -4 addr show eth0 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -n1)
[ -z "$IP" ] && IP="127.0.0.1"

echo "[Servidor] log: $SERVER_LOG"
echo "[Servidor] URL: http://$IP:$PORT/"

termux-toast "Servidor: http://$IP:$PORT" 2>/dev/null || true

exit 0
