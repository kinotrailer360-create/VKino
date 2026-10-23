# Скачать публичное видео TikTok по ссылке без входа:
#   python3 tt_dl.py <url> [папка]  → <папка>/tt_<id>.mp4 ; печатает путь и подпись
# Нужны домены: www.tiktok.com, *.tiktok.com, *.tiktokcdn.com, *.tiktokcdn-us.com
import sys, re, os, json, urllib.request, http.cookiejar
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36'
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
def get(u, ref=None):
    h = {'User-Agent': UA}
    if ref: h['Referer'] = ref
    return op.open(urllib.request.Request(u, headers=h), timeout=60).read()
def dl(url, out='.'):
    page = get(url).decode('utf-8', 'ignore')  # короткие vm.tiktok.com раскрываются редиректом
    m = re.search(r'id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>', page, re.S)
    it = json.loads(m.group(1))['__DEFAULT_SCOPE__'].get('webapp.video-detail', {}).get('itemInfo', {}).get('itemStruct', {}) if m else {}
    v = it.get('video', {}); src = v.get('playAddr') or v.get('downloadAddr')
    if not src: raise SystemExit(f'{url}: видео не найдено (приватное/удалено/фото)')
    os.makedirs(out, exist_ok=True); p = os.path.join(out, f"tt_{it.get('id', 'video')}.mp4")
    open(p, 'wb').write(get(src, 'https://www.tiktok.com/'))
    print(p, '|', it.get('author', {}).get('uniqueId', ''), ' ', it.get('desc', '')[:150])
if __name__ == '__main__':
    dl(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else '.')
