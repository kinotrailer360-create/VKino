# Бесплатный подбор ключей из подсказок поиска YouTube.
# Для каждого запроса берёт подсказки к самому запросу и к «запрос + буква» (ru + en),
# считает, сколько раз фраза встретилась (чем чаще — тем популярнее).
# Использование: python3 yt_suggest.py "месси гол" "messi goal" [--top 40]
import sys, json, urllib.request, urllib.parse, collections, time
args = [a for a in sys.argv[1:] if not a.startswith('--')]
top = int(sys.argv[sys.argv.index('--top') + 1]) if '--top' in sys.argv else 40
if '--top' in sys.argv: args.remove(str(top))
def suggest(q, hl):
    u = 'https://suggestqueries.google.com/complete/search?client=firefox&ds=yt&hl=%s&q=%s' % (hl, urllib.parse.quote(q))
    try:
        with urllib.request.urlopen(u, timeout=10) as r: return json.loads(r.read().decode('utf-8', 'ignore'))[1]
    except Exception: return []
for seed in args:
    ru = any('а' <= c.lower() <= 'я' for c in seed); hl = 'ru' if ru else 'en'
    letters = 'абвгдежзиклмнопрстуфхцчшэюя' if ru else 'abcdefghijklmnopqrstuvwxyz'
    cnt = collections.Counter(); first = suggest(seed, hl)
    for i, s in enumerate(first): cnt[s] += 3 + (10 - i) * 0.3      # прямые подсказки весомее
    for ch in letters:
        for i, s in enumerate(suggest(seed + ' ' + ch, hl)): cnt[s] += 1 + (10 - i) * 0.1
        time.sleep(0.05)
    print(f'\n=== {seed} ({len(cnt)} фраз) ===')
    for s, w in cnt.most_common(top): print(f'{w:5.1f}  {s}')
