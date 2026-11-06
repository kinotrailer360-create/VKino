#!/usr/bin/env bash
# Ночной бэкап базы бота (пользователи, избранное, история постов, настройки, словари /say и /title,
# вход в YouTube). Ставит install.sh в cron: каждую ночь в 03:47. Копии лежат в /opt/vkino-backups 14 дней,
# свежая копия уходит админу в личку бота — на случай, если сервер пропадёт целиком.
# Восстановить: docker compose exec -T db pg_restore -U vkino -d vkino --clean < /opt/vkino-backups/<файл>.dump
set -uo pipefail
DIR=/opt/vkino
OUT=/opt/vkino-backups
mkdir -p "$OUT" && chmod 700 "$OUT"
cd "$DIR/deploy" || exit 1
file="$OUT/vkino_$(date +%F).dump"
if ! docker compose exec -T db pg_dump -U vkino -Fc vkino > "$file.tmp" || [ ! -s "$file.tmp" ]; then
  rm -f "$file.tmp"
  echo "$(date '+%F %T') бэкап не удался"
  status="❌ Ночной бэкап базы не удался — проверь: tail -20 /var/log/vkino-backup.log"
else
  mv "$file.tmp" "$file"
  find "$OUT" -name 'vkino_*.dump' -mtime +14 -delete
  echo "$(date '+%F %T') бэкап готов: $file ($(du -h "$file" | cut -f1))"
  status=""
fi

token=$(grep -E '^BOT_TOKEN=' .env | head -1 | cut -d= -f2- | tr -d "\"' \r")
admins=$(grep -E '^ADMIN_IDS=' .env | head -1 | cut -d= -f2- | tr -d "\"' \r")
admin=${admins%%,*}
[ -n "$token" ] && [ -n "$admin" ] || exit 0
if [ -n "$status" ]; then
  curl -fsS -m 20 "https://api.telegram.org/bot${token}/sendMessage" --data-urlencode "chat_id=${admin}" \
    --data-urlencode "text=${status}" >/dev/null 2>&1 || true
elif [ "$(date +%u)" = "1" ] && [ "$(stat -c %s "$file")" -lt 45000000 ]; then
  # раз в неделю (понедельник) — копия в Telegram: если сервер умрёт, база не потеряется
  curl -fsS -m 120 -F "chat_id=${admin}" -F "document=@${file}" \
    -F "caption=🗄 Еженедельный бэкап базы бота. Храни этот чат закрытым (в базе ID пользователей)." \
    "https://api.telegram.org/bot${token}/sendDocument" >/dev/null 2>&1 || true
fi
