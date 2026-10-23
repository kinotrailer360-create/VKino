# Скачать публичный Instagram Reel по ссылке/коду без входа (через embed-страницу):
#   python3 ig_dl.py <url|код> [папка]   → <папка>/<код>.mp4 ; печатает путь и подпись
import sys, re, os, json, urllib.request
UA = 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148'
def get(u): return urllib.request.urlopen(urllib.request.Request(u, headers={'User-Agent': UA}), timeout=60).read()
def dl(arg, out='.'):
    code = re.search(r'(?:reel|reels|p)/([A-Za-z0-9_-]+)', arg); code = code.group(1) if code else arg.strip('/')
    h = get(f'https://www.instagram.com/reel/{code}/embed/captioned/').decode('utf-8', 'ignore')
    m = re.search(r'video_url\\+"\s*:\s*\\+"(https:.*?\.mp4.*?)\\+"', h)
    if not m: raise SystemExit(f'{code}: видео не найдено (приватный пост или фото)')
    url = m.group(1).replace('\\\\', '\\').replace('\\/', '/').replace('\\u0026', '&')
    url = re.sub(r'\\+/', '/', url).replace('\\', '')
    cap = re.search(r'class="Caption"[^>]*>(.*?)</div>', h, re.S)
    os.makedirs(out, exist_ok=True); p = os.path.join(out, code + '.mp4')
    open(p, 'wb').write(get(url))
    print(p, '|', re.sub(r'<[^>]+>', ' ', cap.group(1)).strip()[:150] if cap else '')
if __name__ == '__main__':
    for a in sys.argv[1:2]: dl(a, sys.argv[2] if len(sys.argv) > 2 else '.')
