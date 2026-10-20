# Мини-клиент Telegram Bot API для ночной сборки FootyVibe360 (без сервера: забираем getUpdates раз в ночь).
# Токен берётся ТОЛЬКО из переменной окружения TELEGRAM_BOT_TOKEN — никогда не хранить его в репозитории.
# Команды:
#   python3 tg.py pull  <папка>        — скачать новые видео/подписи в <папка>/<дата>/, записать manifest.json, подтвердить апдейты
#   python3 tg.py send-video <chat_id> <файл> [подпись]
#   python3 tg.py send-text  <chat_id> <текст или @файл.md>
#   python3 tg.py me                   — проверка токена
import os, sys, json, time, urllib.request, urllib.parse, mimetypes, uuid, datetime
TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
if not TOKEN and 'seo-preview' not in sys.argv: sys.exit('Нет переменной TELEGRAM_BOT_TOKEN')
API = f'https://api.telegram.org/bot{TOKEN}/'; FILE = f'https://api.telegram.org/file/bot{TOKEN}/'
HERE = os.path.dirname(os.path.abspath(__file__)); STATE = os.path.join(HERE, 'state.json')

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
                'forward_from': (m.get('forward_origin') or {}).get('type')}
        if media:
            try:
                f = call('getFile', {'file_id': media['file_id']})
                ext = os.path.splitext(f['file_path'])[1] or '.mp4'
                dst = os.path.join(out, f"{u['update_id']}{ext}")
                urllib.request.urlretrieve(FILE + f['file_path'], dst); item['file'] = dst
            except Exception as e:
                item['error'] = f'не скачалось (лимит Bot API 20 МБ?): {e}'
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
    msgs = [f'🎬 <b>Ролик готов: {E(name)}</b>' + ('\n\n' + '\n'.join(E(w) for w in warn) if warn else '')]
    if ru:
        m = '📌 <b>Название</b> (нажми — скопируется):\n<code>' + E(ru[0]) + '</code>'
        if len(ru) > 1: m += '\n\nЗапасные:\n' + '\n'.join('<code>' + E(x) + '</code>' for x in ru[1:3])
        if en: m += '\n\n🇬🇧 <b>Перевод названия</b> (Studio → Переводы → English):\n<code>' + E(en) + '</code>'
        msgs.append(m)
    d = fence(sec('Описание'))
    if d: msgs.append('📝 <b>Описание</b> (нажми — скопируется):\n<pre>' + E(d) + '</pre>')
    tg_ = fence(sec('Теги'))
    if tg_: msgs.append('🏷 <b>Теги</b>:\n<code>' + E(tg_) + '</code>')
    tail = ['🖼 Обложка — первый кадр ролика (ползунок обложки в начало).']
    if zak: tail.insert(0, '📍 <b>Закреплённый комментарий</b>:\n<code>' + E(zak) + '</code>')
    msgs.append('\n\n'.join(tail))
    return msgs

cmd = sys.argv[1] if len(sys.argv) > 1 else 'me'
if cmd == 'seo-preview':
    [print(m, '\n-----') for m in seo_messages(sys.argv[2])]; sys.exit()
if cmd == 'me': print(call('getMe'))
elif cmd == 'pull': pull(sys.argv[2])
elif cmd == 'send-video':
    print(call('sendVideo', {'chat_id': sys.argv[2], 'caption': (sys.argv[4] if len(sys.argv) > 4 else '')[:1024],
                             'supports_streaming': 'true'}, files={'video': sys.argv[3]}, timeout=600)['message_id'])
elif cmd == 'send-seo':
    for m in seo_messages(sys.argv[3]): call('sendMessage', {'chat_id': sys.argv[2], 'text': m, 'parse_mode': 'HTML'})
    print('seo sent')
elif cmd == 'send-text':
    t = sys.argv[3]; t = open(t[1:]).read() if t.startswith('@') else t
    for i in range(0, len(t), 4000): call('sendMessage', {'chat_id': sys.argv[2], 'text': t[i:i + 4000]})
    print('sent')
