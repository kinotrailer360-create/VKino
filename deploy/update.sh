#!/usr/bin/env bash
# Автообновление с проверкой и откатом: вызывается по расписанию (cron, каждые 5 минут — ставит install.sh).
# Есть новая версия в GitHub и бот ничего не собирает — обновляем через install.sh, ждём, что бот здоров, и
# пишем админам в Telegram; новая версия не запустилась — возвращаем прошлую.
# Журнал: /var/log/vkino-update.log
# Скрипт сначала копирует себя во /tmp: git pull во время обновления меняет этот файл, а bash читает скрипт
# по ходу работы — без копии выполнился бы обрывок новой версии.
if [ -z "${VKINO_UPDATE_COPY:-}" ]; then
  cp "$0" /tmp/vkino-update-run.sh && VKINO_UPDATE_COPY=1 exec bash /tmp/vkino-update-run.sh "$@"
fi
set -uo pipefail
DIR=/opt/vkino
cd "$DIR" || exit 0
exec 9>/var/lock/vkino-update.lock
flock -n 9 || exit 0  # прошлое обновление ещё идёт

git fetch -q origin main 2>/dev/null || exit 0
[ "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)" ] && exit 0
# эту версию уже откатывали (бот с ней не запустился) — ждём следующую
BAD=/var/lib/vkino-bad-commit
[ -f "$BAD" ] && [ "$(cat "$BAD")" = "$(git rev-parse origin/main)" ] && exit 0

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
old_full=$(git -C "$DIR" rev-parse HEAD)
echo "$(date '+%F %T') обновляю с $old"

# бот здоров: контейнер работает, не перезапускается и healthcheck «healthy» (бот раз в 30 с отмечается)
healthy() {
  local cid restarts state
  cid=$(docker compose ps -q bot 2>/dev/null) || return 1
  [ -n "$cid" ] || return 1
  restarts=$(docker inspect -f '{{.RestartCount}}' "$cid" 2>/dev/null || echo 99)
  for _ in $(seq 1 24); do  # до 4 минут
    state=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$cid" 2>/dev/null)
    [ "$(docker inspect -f '{{.RestartCount}}' "$cid" 2>/dev/null || echo 99)" = "$restarts" ] || return 1
    [ "$state" = "healthy" ] && return 0
    [ "$state" = "none" ] && [ "$(docker inspect -f '{{.State.Running}}' "$cid")" = "true" ] && sleep 60 && return 0
    sleep 10
  done
  return 1
}

if bash "$DIR/deploy/install.sh" >/tmp/vkino-update.out 2>&1 && healthy; then
  status="✅ Бот обновлён автоматически"
else
  # новая версия не запустилась — возвращаем прошлую, чтобы канал не остался без бота
  git -C "$DIR" rev-parse origin/main > "$BAD"
  echo "$(date '+%F %T') новая версия не запустилась — откатываю на $old" >> /tmp/vkino-update.out
  git -C "$DIR" reset -q --hard "$old_full"
  (cd "$DIR/deploy" && docker compose up -d --build bot) >>/tmp/vkino-update.out 2>&1 || true
  status="❌ Обновление не запустилось — вернул прошлую версию ($old), бот работает. Напиши ИИ-помощнику: «обновление откатилось»; подробности: tail -80 /var/log/vkino-update.log"
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
