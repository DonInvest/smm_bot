#!/bin/bash
# Быстрый коммит и пуш в GitHub (только отслеживаемые файлы; .env не попадёт).
cd "$(dirname "$0")"

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "❌ Не git-репозиторий."
  exit 1
fi

git add -A
if git diff --staged --quiet; then
  echo "Нет изменений для коммита."
  exit 0
fi

MSG="${1:-Update: $(date +%Y-%m-%d)}"
git commit -m "$MSG"
git push

echo "✅ Запушено в origin/main"
