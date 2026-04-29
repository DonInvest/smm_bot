#!/bin/bash
# Копирует .env на сервер smm_bot. Запускай на Mac из папки проекта:
#   cd /Users/donmm/Desktop/smm_bot
#   ./copy_env_to_server.sh
# Пароль спросит — введи пароль от root@94.156.122.124

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/.env"
SERVER="root@94.156.122.124"
REMOTE_PATH="/root/smm_bot/.env"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Файл .env не найден: $ENV_FILE"
  exit 1
fi

echo "Копирую .env на сервер ${SERVER}..."
scp "$ENV_FILE" "${SERVER}:${REMOTE_PATH}"
echo "Готово. На сервере выполни: systemctl restart smm_bot"
