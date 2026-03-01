#!/bin/bash
# Установка Python 3.10 на сервере, если apt не даёт пакет (нет PPA или не Ubuntu).
# Запуск: на сервере в каталоге проекта: bash install_python310_server.sh

set -e
PYVER=3.10.13
PYENV_ROOT="${PYENV_ROOT:-$HOME/.pyenv}"
PROJECT_DIR="${1:-$(pwd)}"

echo "==> Установка зависимостей для сборки Python..."
apt update -qq
apt install -y -qq build-essential libssl-dev zlib1g-dev libbz2-dev \
  libreadline-dev libsqlite3-dev curl libncursesw5-dev xz-utils tk-dev \
  libxml2-dev libxmlsec1-dev libffi-dev liblzma-dev

if command -v python3.10 &>/dev/null; then
  echo "==> python3.10 уже есть в системе, создаём venv..."
  cd "$PROJECT_DIR"
  rm -rf venv
  python3.10 -m venv venv
  ./venv/bin/pip install -r requirements.txt
  echo "Готово. Дальше: systemctl restart smm_bot"
  exit 0
fi

if [[ -x "$PYENV_ROOT/versions/$PYVER/bin/python" ]]; then
  echo "==> Python $PYVER уже установлен через pyenv."
  PYTHON="$PYENV_ROOT/versions/$PYVER/bin/python"
else
  echo "==> Устанавливаем pyenv и Python $PYVER..."
  if [[ ! -d "$PYENV_ROOT" ]]; then
    curl -sL https://github.com/pyenv/pyenv-installer/raw/master/bin/pyenv-installer | bash
    export PYENV_ROOT="$HOME/.pyenv"
    export PATH="$PYENV_ROOT/bin:$PATH"
  else
    export PYENV_ROOT="$HOME/.pyenv"
    export PATH="$PYENV_ROOT/bin:$PATH"
  fi
  eval "$("$PYENV_ROOT/bin/pyenv" init -)"
  pyenv install -s "$PYVER"
  PYTHON="$PYENV_ROOT/versions/$PYVER/bin/python"
fi

echo "==> Создаём venv в $PROJECT_DIR..."
cd "$PROJECT_DIR"
rm -rf venv
"$PYTHON" -m venv venv
./venv/bin/pip install -r requirements.txt

echo "Готово. Дальше: systemctl restart smm_bot"
echo "Если юнит запускает python3, убедись что ExecStart использует: $PROJECT_DIR/venv/bin/python"
