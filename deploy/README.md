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
