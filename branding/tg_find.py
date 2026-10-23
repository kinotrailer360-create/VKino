# Поиск постов в открытых Telegram-каналах (без входа): python3 tg_find.py "запрос" [канал1 канал2 ...]
# Выдаёт ссылки t.me/<канал>/<id> + текст + есть ли видео. Пользователь открывает ссылку в Telegram и пересылает видео в бот.
import sys, re, html, urllib.request, urllib.parse
CHANNELS = ['zhfootballll', 'myachPRO']  # пополнять каналами с видео голов / с трибун
q = sys.argv[1]; chs = sys.argv[2:] or CHANNELS
for ch in chs:
    h = urllib.request.urlopen(f'https://t.me/s/{ch}?q=' + urllib.parse.quote(q), timeout=20).read().decode()
    for blk in h.split('tgme_widget_message_wrap')[1:]:
        post = re.search(r'data-post="([^"]+)"', blk)
        txt = re.search(r'tgme_widget_message_text[^>]*>(.*?)</div>', blk, re.S)
        when = re.search(r'datetime="([^"]+)"', blk)
        if not post: continue
        t = ' '.join(html.unescape(re.sub('<[^>]+>', ' ', txt.group(1) if txt else '')).split())
        vid = '🎬' if 'video' in blk else '  '
        print(f"{vid} https://t.me/{post.group(1)} | {(when.group(1)[:16] if when else '')} | {t[:110]}")
