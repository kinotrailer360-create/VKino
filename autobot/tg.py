# Мини-клиент Telegram Bot API для ночной сборки FootyVibe360 (без сервера: забираем getUpdates раз в ночь).
# Токен берётся ТОЛЬКО из переменной окружения TELEGRAM_BOT_TOKEN — никогда не хранить его в репозитории.
# Команды:
#   python3 tg.py pull  <папка>        — скачать новые видео/подписи в <папка>/<дата>/, записать manifest.json, подтвердить апдейты
#   python3 tg.py send-video <chat_id> <файл> [подпись]
#   python3 tg.py send-text  <chat_id> <текст или @файл.md>
#   python3 tg.py me                   — проверка токена
import os, sys, json, time, urllib.request, urllib.parse, mimetypes, uuid, datetime, re
TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
if not TOKEN and 'seo-preview' not in sys.argv: sys.exit('Нет переменной TELEGRAM_BOT_TOKEN')
API = f'https://api.telegram.org/bot{TOKEN}/'; FILE = f'https://api.telegram.org/file/bot{TOKEN}/'
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE); STATE = os.path.join(HERE, 'state.json')

def call(method, params=None, files=None, timeout=60):
    if files:
        b = uuid.uuid4().hex; body = b''
        for k, v in (params or {}).items():
            body += f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
        for k, path in files.items():
            body += (f'--{b}\r\nContent-Disposition: form-data; name="{k}"; filename="{os.path.basename(path)}"\r\n'
                     f'Content-Type: {mimetypes.guess_type(path)[0] or "application/octet-stream"}\r\n\r\n').encode()
            body += open(path, 'rb').read() + b'\r\n'
        body += f'--{b}--\r\n'.encode()
        req = urllib.request.Request(API + method, data=body, headers={'Content-Type': f'multipart/form-data; boundary={b}'})
    else:
        req = urllib.request.Request(API + method, data=urllib.parse.urlencode(params or {}).encode())
    with urllib.request.urlopen(req, timeout=timeout) as r:
        res = json.loads(r.read())
    if not res.get('ok'): raise RuntimeError(res)
    return res['result']

def load_state():
    return json.load(open(STATE)) if os.path.exists(STATE) else {'offset': 0, 'chats': []}

def pull(out_root):
    st = load_state(); ups = call('getUpdates', {'offset': st['offset'], 'timeout': 0, 'allowed_updates': '["message"]'})
    day = datetime.datetime.utcnow().strftime('%Y-%m-%d'); out = os.path.join(out_root, day); os.makedirs(out, exist_ok=True)
    man_path = os.path.join(out, 'manifest.json'); man = json.load(open(man_path)) if os.path.exists(man_path) else []
    for u in ups:
        st['offset'] = u['update_id'] + 1; m = u.get('message') or {}
        chat = m.get('chat', {}).get('id')
        if chat and chat not in st['chats']: st['chats'].append(chat)
        media = m.get('video') or m.get('document') or m.get('animation')
        item = {'update_id': u['update_id'], 'chat_id': chat, 'date': m.get('date'),
                'caption': m.get('caption') or m.get('text') or '', 'media_group': m.get('media_group_id'),
                'forward_from': (m.get('forward_origin') or {}).get('type'),
                'media_meta': {k: (m.get('video') or m.get('document') or m.get('animation') or {}).get(k) for k in ('duration', 'width', 'height', 'file_size', 'file_name')}}
        if media:
            try:
                f = call('getFile', {'file_id': media['file_id']})
                ext = os.path.splitext(f['file_path'])[1] or '.mp4'
                dst = os.path.join(out, f"{u['update_id']}{ext}")
                urllib.request.urlretrieve(FILE + f['file_path'], dst); item['file'] = dst
            except Exception as e:
                item['error'] = f'не скачалось (лимит Bot API 20 МБ?): {e}'
        elif re.search(r'instagram\.com/(?:reels?|p)/[A-Za-z0-9_-]+', item['caption']):
            # ссылка на Instagram Reel → скачать через ig_dl.py (публичные рилсы, без входа)
            import ig_dl, io, contextlib
            for url in re.findall(r'https?://(?:www\.)?instagram\.com/(?:reels?|p)/[A-Za-z0-9_-]+', item['caption']):
                try:
                    buf = io.StringIO()
                    with contextlib.redirect_stdout(buf): ig_dl.dl(url, out)
                    line = buf.getvalue().strip(); path, _, cap = line.partition(' | ')
                    dst = os.path.join(out, f"{u['update_id']}_{os.path.basename(path)}"); os.replace(path, dst)
                    item.setdefault('files', []).append(dst); item['file'] = dst; item['ig_caption'] = cap
                except BaseException as e:
                    item['error'] = f'instagram не скачался: {e}'
        man.append(item)
    json.dump(man, open(man_path, 'w'), ensure_ascii=False, indent=1)
    json.dump(st, open(STATE, 'w'))
    print(json.dumps({'folder': out, 'new_updates': len(ups), 'files': sum(1 for i in man if 'file' in i)}, ensure_ascii=False))

import html, re
def seo_messages(md_path):
    """Разбирает наш SEO-.md и собирает аккуратные сообщения для Telegram (HTML, копирование по нажатию)."""
    t = open(md_path, encoding='utf-8').read(); E = html.escape
    head = re.search(r'^#\s*SEO\s*[—-]\s*«?(.+?)»?\s*\(', t, re.M)
    name = head.group(1) if head else os.path.basename(md_path)
    def sec(title):
        m = re.search(r'^##\s*' + title + r'.*?$(.*?)(?=^##\s|\Z)', t, re.M | re.S); return m.group(1) if m else ''
    def ticks(block): return re.findall(r'`([^`\n]+)`', block)
    def fence(block):
        m = re.search(r'```\w*\n(.*?)```', block, re.S); return m.group(1).strip() if m else ''
    nm = sec('Название'); titles = ticks(nm)
    en = next((x for line in nm.splitlines() if 'англ' in line.lower() or 'english' in line.lower() for x in ticks(line)), None)
    ru = [x for x in titles if x != en]
    warn = [re.sub(r'[*`]', '', l).strip() for l in t.splitlines() if l.strip().startswith('⚠️')]
    pub = sec('Публикация'); zak = next((x for l in pub.splitlines() if 'Закреп' in l for x in ticks(l)), None)
    # каждое копируемое — отдельным сообщением: (текст, что копирует кнопка)
    def item(label, body): return (label + '\n<pre>' + E(body) + '</pre>', body)
    msgs = [(f'🎬 <b>Ролик готов: {E(name)}</b>' + ('\n\n' + '\n'.join(E(w) for w in warn) if warn else ''), None)]
    if ru:
        msgs.append(item('📌 <b>Название</b>', ru[0]))
        for i, x in enumerate(ru[1:3], 1): msgs.append(item(f'🔁 <b>Запасное название {i}</b>', x))
        if en: msgs.append(item('🇬🇧 <b>Перевод названия</b> (Studio → Переводы → English)', en))
    d = fence(sec('Описание'))
    if d: msgs.append(item('📝 <b>Описание</b>', d))
    tg_ = fence(sec('Теги'))
    if tg_: msgs.append(item('🏷 <b>Теги</b>', tg_))
    if zak: msgs.append(item('📍 <b>Закреплённый комментарий</b>', zak))
    msgs.append(('🖼 Обложка — первый кадр ролика (ползунок обложки в начало).', None))
    return msgs

cmd = sys.argv[1] if len(sys.argv) > 1 else 'me'
if cmd == 'seo-preview':
    [print(m, '| copy-btn:' if c and len(c) <= 256 else '', '\n-----') for m, c in seo_messages(sys.argv[2])]; sys.exit()
if cmd == 'me': print(call('getMe'))
elif cmd == 'pull': pull(sys.argv[2])
elif cmd == 'send-video':
    print(call('sendVideo', {'chat_id': sys.argv[2], 'caption': (sys.argv[4] if len(sys.argv) > 4 else '')[:1024],
                             'supports_streaming': 'true'}, files={'video': sys.argv[3]}, timeout=600)['message_id'])
elif cmd == 'send-seo':
    for m, c in seo_messages(sys.argv[3]):
        d = {'chat_id': sys.argv[2], 'text': m, 'parse_mode': 'HTML'}
        # кнопка «Копировать» (Bot API copy_text, до 256 символов); длинное — копируется нажатием на блок
        if c and len(c) <= 256: d['reply_markup'] = json.dumps({'inline_keyboard': [[{'text': '📋 Копировать', 'copy_text': {'text': c}}]]})
        call('sendMessage', d)
    print('seo sent')
elif cmd == 'send-text':
    t = sys.argv[3]; t = open(t[1:]).read() if t.startswith('@') else t
    for i in range(0, len(t), 4000): call('sendMessage', {'chat_id': sys.argv[2], 'text': t[i:i + 4000]})
    print('sent')
