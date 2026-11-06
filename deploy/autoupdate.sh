#!/usr/bin/env bash
# Автообновление: вызывается по расписанию (cron, каждые 5 минут — ставит install.sh).
# Есть новая версия в GitHub и бот ничего не собирает — обновляем через install.sh и пишем админам в Telegram.
# Журнал: /var/log/vkino-update.log
set -uo pipefail
DIR=/opt/vkino
cd "$DIR" || exit 0
exec 9>/var/lock/vkino-update.lock
flock -n 9 || exit 0  # прошлое обновление ещё идёт

git fetch -q origin main 2>/dev/null || exit 0
[ "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)" ] && exit 0

cd "$DIR/deploy"
# идёт сборка ролика — перезапуск её оборвёт; попробуем через 5 минут
running=$(docker compose exec -T db psql -U vkino -d vkino -tAc \
  "SELECT 1 FROM shorts_settings WHERE key = 'running'" 2>/dev/null | tr -d '[:space:]')
since=/var/lib/vkino-update-wait
if [ "$running" = "1" ]; then
  [ -f "$since" ] || date +%s > "$since"
  # отметка «идёт сборка» висит дольше 40 минут — сборка давно прервалась, не ждём вечно
  if [ $(( $(date +%s) - $(cat "$since") )) -lt 2400 ]; then
    echo "$(date '+%F %T') новая версия есть, но идёт сборка ролика — жду"
    exit 0
  fi
fi
rm -f "$since"

old=$(git -C "$DIR" rev-parse --short HEAD)
echo "$(date '+%F %T') обновляю с $old"
if bash "$DIR/deploy/install.sh" >/tmp/vkino-update.out 2>&1; then
  status="✅ Бот обновлён автоматически"
else
  status="❌ Автообновление не удалось — подробности: tail -50 /var/log/vkino-update.log"
fi
cat /tmp/vkino-update.out
new=$(git -C "$DIR" rev-parse --short HEAD)
# что нового — заголовки влитых изменений (без строк «Merge pull request»)
changes=$(git -C "$DIR" log --no-merges --format='• %s' "$old..$new" 2>/dev/null | head -8)
echo "$(date '+%F %T') $status ($old → $new)"

# сообщение админам: ключ и id — из .env (в git их нет)
token=$(grep -E '^BOT_TOKEN=' .env | head -1 | cut -d= -f2- | tr -d "\"' \r")
admins=$(grep -E '^ADMIN_IDS=' .env | head -1 | cut -d= -f2- | tr -d "\"' \r")
admins=${admins:-803444545}
[ -n "$token" ] || exit 0
for id in ${admins//,/ }; do
  curl -fsS -m 20 "https://api.telegram.org/bot${token}/sendMessage" \
    --data-urlencode "chat_id=${id}" \
    --data-urlencode "text=${status}
${changes}" >/dev/null 2>&1 || true
done
