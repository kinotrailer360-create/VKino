# VKino 🎬

Telegram-бот `@vkino_bot` для поиска фильмов и сериалов на русском языке.

## Что умеет

- поиск фильмов и сериалов через ПоискКино API, включая будущие премьеры;
- карточки с постером, годом, жанрами, описанием и рейтингами Кинопоиска и IMDb;
- трейлеры (TMDB / YouTube) во встроенном Mini App;
- новинки, популярное, «Скоро», подборки и случайная рекомендация;
- напоминания о премьерах;
- бесплатные открытые фильмы Blender Studio;
- полные фильмы с официальных каналов Rutube во встроенном плеере;
- Mini App-плеер для источников, добавленных вручную или через API провайдера
  (выбор озвучки и качества, продолжение просмотра, следующая серия);
- избранное, история, статистика и профиль;
- админка (`/admin`) и рассылка (`/broadcast текст`).

## Переменные окружения

Никогда не добавляйте `.env` и реальные токены в GitHub.

Обязательные:

```env
BOT_TOKEN=...
KINOPOISK_TOKEN=...
# необязательно: второй ключ poiskkino (бот сам переключится, когда первый упрётся в лимит)
KINOPOISK_TOKEN_2=...
# необязательно: запасной Кинопоиск — kinopoiskapiunofficial.tech (поиск и карточки, когда ключи на лимите)
KP_UNOFFICIAL_KEY=...
DATABASE_URL=postgresql://user:password@host:5432/dbname
ADMIN_IDS=803444545
```

Необязательные:

```env
WEBAPP_URL=https://your-domain        # на Railway берётся из RAILWAY_PUBLIC_DOMAIN
TMDB_API_KEY=...                      # трейлеры из TMDB
YOUTUBE_API_KEY=...                   # поиск трейлеров на YouTube
VIDEO_PROVIDER_API_URL=https://legal-provider.example/api/catalog
VIDEO_PROVIDER_API_TOKEN=...
VIDEO_PROVIDER_SYNC_SECONDS=900
PORT=8080                             # порт Mini App, Railway задаёт сам
RUTUBE_ENABLED=true                   # поиск фильмов на Rutube
RUTUBE_OFFICIAL_AUTHORS=123,456       # доп. ID каналов Rutube, считающихся официальными
```

## Видеопровайдер

Если задан `VIDEO_PROVIDER_API_URL`, бот периодически забирает каталог в формате:

```json
{
  "items": [
    {
      "kinopoisk_id": 12345,
      "season": null,
      "episode": null,
      "voice": "Official dub",
      "quality": 1080,
      "url": "https://cdn.example/video/master.m3u8",
      "type": "hls",
      "active": true
    }
  ]
}
```

`quality` — одно из 360, 480, 720, 1080; ссылки принимаются только `https://`.
Используйте только источники, которые вы вправе распространять.

## Rutube

При открытии карточки фильма бот ищет полный фильм на Rutube и показывает кнопку
«▶️ Смотреть бесплатно (Rutube)». Берутся только видео, которые Rutube помечает как
официальные, или с каналов из `RUTUBE_OFFICIAL_AUTHORS`; трейлеры, обзоры и ролики
короче 40 минут отбрасываются. Результат кэшируется в таблице `rutube_cache` на 7 дней.

## Рассылка

`/broadcast текст` поддерживает HTML-разметку Telegram (`<b>`, `<i>`, `<a href="...">`).
Сначала бот присылает предпросмотр админу; если разметка неверная, рассылка не начнётся.
Символы `<`, `>`, `&` в обычном тексте пишите как `&lt;`, `&gt;`, `&amp;`.

## Запуск локально

Нужен PostgreSQL.

```bash
pip install -r requirements.txt
python bot.py
```

## Railway

Репозиторий готов к Docker-деплою на Railway. Подключите PostgreSQL
(переменная `DATABASE_URL`) и добавьте `BOT_TOKEN`, `KINOPOISK_TOKEN`,
`ADMIN_IDS` в Variables.
