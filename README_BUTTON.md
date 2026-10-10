VKino — кнопка «Смотреть HDRezka»

Что изменено:
1. В карточке найденного фильма/сериала появляется отдельная кнопка:
   ▶️ Смотреть HDRezka
2. Бот ищет страницу по названию, альтернативному названию и году.
3. Найденная ссылка сохраняется в таблице hdrezka_cache.
4. При следующих открытиях карточки используется кэш — повторный поиск не нужен.
5. Для будущих релизов кнопка не показывается.
6. Старая кнопка плеера переименована в «▶️ Смотреть в VKino».
7. Все остальные функции VKino сохранены.

Что загрузить в GitHub:
- bot.py
- hdrezka_provider.py
- requirements.txt

Railway Variables менять не нужно, если уже есть:
HDREZKA_ENABLED=true
HDREZKA_MIRROR=https://hdrezka8benxe.org
HDREZKA_DEFAULT_VOICE=Дубляж
HDREZKA_MAX_VOICES=8
HDREZKA_REFRESH_MINUTES=30

После загрузки:
Railway -> VKino -> Update available / Deploy latest commit.
Проверить Deploy Logs.

Тест:
Telegram -> /start -> Найти фильм или сериал -> Интерстеллар.
В карточке должна появиться «▶️ Смотреть HDRezka», если совпадение найдено.
