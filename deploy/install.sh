#!/usr/bin/env bash
# Установка и обновление бота на чистом сервере Ubuntu одной командой:
#   curl -fsSL https://raw.githubusercontent.com/kinotrailer360-create/VKino/main/deploy/install.sh | sudo bash
# Повторный запуск — обновление до последней версии из GitHub (настройки и база сохраняются).
set -euo pipefail
REPO=https://github.com/kinotrailer360-create/VKino.git
DIR=/opt/vkino

if ! command -v docker >/dev/null 2>&1; then
  echo "== Ставлю Docker"
  curl -fsSL https://get.docker.com | sh
fi
command -v git >/dev/null 2>&1 || { apt-get update -qq && apt-get install -y -qq git; }

# файл подкачки: на сервере с 1–2 ГБ памяти монтаж видео без него зависает или падает
if [ -z "$(swapon --show --noheadings 2>/dev/null)" ] && [ ! -f /swapfile ]; then
  echo "== Создаю файл подкачки 2 ГБ"
  if fallocate -l 2G /swapfile 2>/dev/null || dd if=/dev/zero of=/swapfile bs=1M count=2048 status=none; then
    chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile \
      && { grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab; } \
      || { swapoff /swapfile 2>/dev/null; rm -f /swapfile; echo "   подкачку не включить (так бывает на некоторых VPS) — продолжаю без неё"; }
  fi
fi

if [ -d "$DIR/.git" ]; then
  echo "== Обновляю код"
  git -C "$DIR" pull --ff-only
else
  echo "== Скачиваю бота"
  git clone --depth 1 "$REPO" "$DIR"
fi
cd "$DIR/deploy"

if [ ! -f .env ]; then
  IP=$(curl -fsS4 https://api.ipify.org)
  {
    echo "# Адрес для мини-приложения: бесплатное имя по IP сервера (можно заменить на свой домен)"
    echo "DOMAIN=${IP//./-}.sslip.io"
    echo "POSTGRES_PASSWORD=$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 24)"
    echo "SHORTS_DIR=/tmp/kinomir_shorts"
    echo
    echo "# ↓ Вставьте сюда переменные из Railway (Variables → Raw Editor), кроме DATABASE_URL и PORT"
  } > .env
  chmod 600 .env
  echo
  echo "Создан файл $DIR/deploy/.env — добавьте в него ключи (BOT_TOKEN и др.):"
  echo "  nano $DIR/deploy/.env"
  echo "и запустите эту команду ещё раз."
  exit 0
fi

grep -q '^BOT_TOKEN=.\+' .env || { echo "В .env нет BOT_TOKEN — откройте: nano $DIR/deploy/.env"; exit 1; }
# переменные Railway, которые здесь не нужны или задаются сами
sed -i -E '/^(DATABASE_URL|DATABASE_PUBLIC_URL|PORT|RAILWAY_[A-Z_]*)=/d' .env

echo "== Запускаю базу"
docker compose up -d db
if [ -n "${RAILWAY_DB_URL:-}" ]; then
  echo "== Переношу базу с Railway"
  docker compose exec -T db sh -c 'until pg_isready -U vkino; do sleep 1; done'
  docker run --rm postgres:17-alpine pg_dump "$RAILWAY_DB_URL" --no-owner --no-acl \
    | docker compose exec -T db psql -q -U vkino vkino
fi

echo "== Собираю и запускаю бота (первый раз 5–10 минут)"
docker compose up -d --build
docker image prune -f >/dev/null
echo
echo "Готово. Логи бота:  cd $DIR/deploy && docker compose logs -f --tail 50 bot"
