# Запуск на своём сервере (Timeweb Cloud и т.п.)

Сервер: Ubuntu 22.04/24.04, от 1 ГБ RAM (лучше 2 ГБ), локация в Европе (Амстердам) —
из России не работают Gemini и часть сервисов.

1. Подключиться к серверу по SSH (на телефоне — приложение Termius) и выполнить:
   `curl -fsSL https://raw.githubusercontent.com/kinotrailer360-create/VKino/main/deploy/install.sh | sudo bash`
   Скрипт поставит Docker, скачает бота и создаст `/opt/vkino/deploy/.env`.
2. `nano /opt/vkino/deploy/.env` — вставить переменные из Railway (Variables → Raw Editor):
   `BOT_TOKEN`, `KINOPOISK_TOKEN`, `TMDB_API_KEY`, `GEMINI_API_KEY`, `RESERVE_AI_KEY`, `ADMIN_IDS` и др.
   `DATABASE_URL`, `PORT`, `RAILWAY_*` не нужны (скрипт их уберёт). Сохранить: Ctrl+O, Enter, Ctrl+X.
3. Перенос базы с Railway (один раз): в Railway у Postgres → Variables скопировать `DATABASE_PUBLIC_URL`, затем
   `curl -fsSL https://raw.githubusercontent.com/kinotrailer360-create/VKino/main/deploy/install.sh | sudo RAILWAY_DB_URL='сюда-ссылку' bash`
   Без переноса: та же команда, что в п.1.
4. Остановить бота на Railway (иначе два бота будут спорить за один токен).

Обновление до новой версии — команда из п.1. Логи: `cd /opt/vkino/deploy && docker compose logs -f --tail 50 bot`.
Мини-приложение открывается по адресу `https://<IP-через-дефисы>.sslip.io` (HTTPS-сертификат Caddy получает сам).

## Ударения в озвучке (RUAccent, по желанию)

Нейросеть RUAccent ставит ударения во всех словах озвучки (кобуре́, забира́ть, наизу́сть, омографы вроде
за́мок/замо́к); имена и свой словарь (/say) важнее неё. Ей нужно около 2 ГБ оперативной памяти — сначала
проверьте `free -h`: в строке Mem в колонке available должно быть не меньше 3 ГБ.

Включить: в `deploy/.env` строка `COMPOSE_PROFILES=accent` (если уже есть `x` — `COMPOSE_PROFILES=x,accent`),
затем `cd /opt/vkino/deploy && docker compose up -d --build`. Первый запуск скачивает модель (~500 МБ, пара
минут). Проверка: `docker compose logs --tail 20 accent` — должно быть `ready`.

## Словарь ударений и кэш озвучки (работают сами)

Без сервиса RUAccent бот использует открытый словарь ударений RUAccent (3,2 млн словоформ): при первой
озвучке скачивает его (21 МБ) и строит базу `stress.db` в томе `data` — ~100 МБ на диске, памяти почти не
нужно. Омографы (за́мок/замо́к) словарь не трогает. Выключить: `STRESS_DICT=0` в `.env`.

Озвученные фразы хранятся в том же томе (`voice_cache`): перемонтаж выпуска заново оплачивает только новые и
исправленные фразы.

## База данных: миграции, копии, откат

- Новые таблицы создаются только **нумерованными миграциями** (`shorts/migrations.py`, таблица `schema_version`): по порядку, один раз, с контрольной суммой (изменённая применённая миграция — бот не стартует миграции). Две копии бота одновременно не применят их дважды (блокировка PostgreSQL).
- Миграции только **добавляют** таблицы; старые данные (`shorts_settings`: `posts_meta`, `posts_queue`, `posts_history`) не удаляются, и две недели бот пишет и туда — откат на прошлую версию ничего не теряет.
- Перед каждым автообновлением `update.sh` делает копию базы `/opt/vkino-backups/pre-update-<версия>.dump` (5 последних); не получилось — обновление откладывается.
- Откат версии: `update.sh` делает сам, если новая версия не запустилась. Восстановить базу из копии (крайний случай):
  `cd /opt/vkino/deploy && docker compose exec -T db pg_restore -U vkino -d vkino --clean < /opt/vkino-backups/pre-update-<версия>.dump`
