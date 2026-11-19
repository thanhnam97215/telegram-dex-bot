#!/bin/bash
# Cai / cai lai service dexbot (systemd). Ban goc cua unit nam o day
# (~/workspace/telegram-dex-bot/dexbot.service) vi /etc khong ton tai
# qua cac lan thay VM; chay lai script nay sau khi VM bi thay de khoi
# phuc service.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
UNIT="$DIR/dexbot.service"
if [ ! -s "$DIR/config.json" ]; then
  echo "config.json chua co, dung lai"
  exit 1
fi
if ! python3 -c "import json; c=json.load(open('$DIR/config.json')); assert (c.get('telegram_token') or c.get('telegram_credential')) and c.get('chat_id')" 2>/dev/null; then
  echo "config.json chua co token/credential Telegram hoac chat_id, dung lai (chua khoi dong service)"
  exit 1
fi
sudo cp "$UNIT" /etc/systemd/system/dexbot.service
sudo systemctl daemon-reload
sudo systemctl enable dexbot.service
if systemctl is-active --quiet dexbot.service; then
  sudo systemctl restart dexbot.service
else
  sudo systemctl start dexbot.service
fi
sleep 2
systemctl is-active dexbot.service
systemctl status dexbot.service --no-pager | head -12
