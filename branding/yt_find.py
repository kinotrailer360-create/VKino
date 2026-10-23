# Поиск свежих видео на YouTube (за неделю): python3 yt_find.py "запрос1" "запрос2" → реальные ссылки youtu.be + канал + давность. Нужен домен www.youtube.com в allowlist.
import sys, re, json, urllib.request, urllib.parse
def search(q, n=8):
    url='https://www.youtube.com/results?search_query='+urllib.parse.quote(q)+'&sp=EgIIAg%253D%253D'  # upload date: this week
    h=urllib.request.urlopen(urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0','Accept-Language':'ru,en'}),timeout=20).read().decode()
    m=re.search(r'var ytInitialData = (\{.*?\});</script>',h)
    d=json.loads(m.group(1)); out=[]
    def walk(o):
        if isinstance(o,dict):
            for k in ('videoRenderer','reelItemRenderer'):
                if k in o:
                    v=o[k]; t=''.join(r.get('text','') for r in v.get('title',{}).get('runs',[])) or v.get('headline',{}).get('simpleText','')
                    out.append((v.get('videoId'),t,v.get('publishedTimeText',{}).get('simpleText',''),''.join(r.get('text','') for r in v.get('ownerText',{}).get('runs',[]))))
            for x in o.values(): walk(x)
        elif isinstance(o,list):
            for x in o: walk(x)
    walk(d); seen=set(); res=[]
    for r in out:
        if r[0] and r[0] not in seen: seen.add(r[0]); res.append(r)
    return res[:n]
for q in sys.argv[1:]:
    print('##',q)
    for vid,t,p,o in search(q): print(f'https://youtu.be/{vid} | {t} | {o} | {p}')
