# Гол Ямаля ПСЖ — Барселона (2:2, 79'): монтаж 720x960, 30 fps, склейки по долям трека 140 BPM
# Источники: src0 — трибуна высоко за линией (широкий), src1 — трибуна низко сбоку (вплотную). Оба 720x1280 с полями: картинка y=160..1120
import subprocess, sys, os, json
W=os.path.dirname(os.path.abspath(__file__)); R='/home/user/VKino/branding'
SRCS=[f'{W}/src0.mp4',f'{W}/src1.mp4']
B=60/140; FPS=30
FONT='/usr/share/fonts/opentype/inter/InterDisplay-Black.otf'
FONTI='/usr/share/fonts/opentype/inter/InterDisplay-BlackItalic.otf'
GRADE='eq=saturation=1.08:contrast=1.1:brightness=0.0,unsharp=5:5:0.6,vignette=PI/5'
GAR='0xA50044'; BLU='0x004D98'; GOLD='0xEDBB00'
def fr(beat): return round(beat*B*FPS)
def run(cmd):
    r=subprocess.run(cmd,capture_output=True,text=True)
    if r.returncode: print(' '.join(cmd)[:300]); print(r.stderr[-2000:]); sys.exit(1)
def txt(t,size,y,font=FONTI,extra=''):
    return (f"drawtext=fontfile={font}:text='{t}':fontsize={size}:fontcolor=white:"
            f"borderw=5:bordercolor=black@0.85:x=(w-tw)/2:y={y}{extra}")
PLATE=("drawbox=x=(iw-320)/2:y=70:w=320:h=62:color=0x111111@0.92:t=fill,"
       f"drawbox=x=(iw-320)/2:y=126:w=160:h=6:color={BLU}@1:t=fill,drawbox=x=iw/2:y=126:w=160:h=6:color={GAR}@1:t=fill,"
       f"drawtext=fontfile={FONTI}:text='L.YAMAL':fontsize=44:fontcolor=white:x=(w-tw)/2+14:y=80,"
       f"drawtext=fontfile={FONT}:text='10':fontsize=28:fontcolor={GOLD}:x=(w-320)/2+12:y=86")
# табло после гола
SCORE=(f"drawbox=x=(iw-420)/2:y=60:w=420:h=56:color=0x0B0B14@0.9:t=fill,"
       f"drawbox=x=(iw-420)/2:y=112:w=210:h=5:color={BLU}@1:t=fill,drawbox=x=iw/2:y=112:w=210:h=5:color={GAR}@1:t=fill,"
       f"drawtext=fontfile={FONT}:text='ПСЖ  2\\:2  БАРСЕЛОНА':fontsize=30:fontcolor=white:x=(w-420)/2+18:y=72,"
       f"drawtext=fontfile={FONT}:text='79’':fontsize=30:fontcolor={GOLD}:x=(w+420)/2-58:y=72")
def crop(y=160,h=960): return f"crop=720:{h}:0:{y},scale=720:960:flags=lanczos,setsar=1"
ZC="crop=405:540:255:340,scale=720:960:flags=lanczos,setsar=1"      # широкий план, крупно: Ямаль, вратарь, ворота
def shake(a): return (f"scale=792:1056,crop=720:960:x='36+{a}*sin(t*71)*exp(-t*2.2)':"
                      f"y='48+{a}*cos(t*53)*exp(-t*2.2)'")
Z=lambda z,cx,cy,dz=0: f"zoompan=z='{z}+{dz}*on':d=1:x='{cx}-{cx}/zoom':y='{cy}-{cy}/zoom':s=720x960:fps=30"
MI="minterpolate=fps=30:mi_mode=mci:mc_mode=aobmc"
WF="fade=in:st=0:d=0.1:color=white"
segs=[]
def seg(b0,b1,ss,vf,speed=1.0,src=0): segs.append((b0,b1,ss,speed,vf,src))

FREEZE_T=3.0   # кадр разбора (src0): Ямаль в штрафной перед ударом
def prebuilt():
    run(['ffmpeg','-v','error','-y','-ss',str(FREEZE_T),'-i',SRCS[0],'-vf',f"{ZC},{GRADE}",'-frames:v','1',f'{W}/freeze.png'])
    n=fr(18)-fr(10); dur=n/FPS
    spec={'duration':dur,'elements':[
        {'type':'beam','pos':[330,600],'start':0.25,'label':'ЯМАЛЬ'},
        {'type':'zone','poly':[[470,520],[548,512],[548,430],[495,460]],'start':1.5,'label':None},
        {'type':'arrow','pts':[[345,600],[530,505]],'start':2.2,'curve':-0.15}]}
    json.dump(spec,open(f'{W}/tspec.json','w'),ensure_ascii=False)
    os.makedirs(f'{W}/tele',exist_ok=True)
    run(['python3',f'{R}/telestrate.py',f'{W}/freeze.png',f'{W}/tele',f'{W}/tspec.json'])
    run(['ffmpeg','-v','error','-y','-loop','1','-i',f'{W}/freeze.png','-framerate','30','-i',f'{W}/tele/t%04d.png','-filter_complex',
         f"[0]scale=720:960,setsar=1,format=rgba[f];[f][1]overlay=format=auto,"
         f"drawbox=x=24:y=60:w=150:h=50:color={GOLD}@1:t=fill,"
         f"drawtext=fontfile={FONT}:text='РАЗБОР':fontsize=32:fontcolor=black:x=38:y=68,"
         f"drawtext=fontfile={FONTI}:text='В БЛИЖНИЙ УГОЛ':fontsize=40:fontcolor={GOLD}:borderw=4:bordercolor=black:x=(w-tw)/2:y=800:enable='gte(t\\,2.7)',format=yuv420p",
         '-frames:v',str(n),'-r','30','-c:v','libx264','-crf','16','-preset','medium',f'{W}/tseg.mp4'])
    # КИНО-повтор: вплотную, обыгрыш → удар (src1)
    run(['ffmpeg','-v','error','-y','-ss','6.45','-t','1.1','-i',SRCS[1],'-an','-c:v','libx264','-crf','14',f'{W}/cine_in.mp4'])
    d=(fr(38)-fr(34))/FPS
    run(['bash',f'{R}/cinematic.sh',f'{W}/cine_in.mp4',f'{W}/cine.mp4',f"setpts=PTS/0.45,fps=30,{crop()}",str(d)])
    # карточка в дыму: Ямаль с поднятыми руками (src1)
    run(['ffmpeg','-v','error','-y','-ss','12.9','-t','1.5','-i',SRCS[1],'-an','-c:v','libx264','-crf','14',f'{W}/card_in.mp4'])
    d=(fr(51)-fr(46))/FPS
    run(['bash',f'{R}/cinematic.sh',f'{W}/card_in.mp4',f'{W}/card_raw.mp4',
         "setpts=PTS/0.6,fps=30,crop=480:640:200:160,scale=720:960:flags=lanczos,setsar=1",str(d)])
    run(['ffmpeg','-v','error','-y','-i',f'{W}/card_raw.mp4','-vf',
         f"{txt('ЛАМИН ЯМАЛЬ',72,690,FONTI,':alpha=min(1\\,t/0.4)')},"
         f"drawtext=fontfile={FONT}:text='79’ · 2\\:2 · ПСЖ — БАРСЕЛОНА':fontsize=32:fontcolor={GOLD}:borderw=3:bordercolor=black:x=(w-tw)/2:y=785:alpha='min(1,max(0,(t-0.25)/0.4))',format=yuv420p",
         '-c:v','libx264','-crf','16','-preset','medium',f'{W}/card.mp4'])

# --- таймлайн ---------------------------------------------------------------
# A: Ямаль ведёт мяч вдоль штрафной (вплотную)
seg(0,6,2.1,f"{crop()},{Z(1.1,360,480,0.001)}",src=1)
# B: вход в штрафную на защитника (широкий, крупно) — заканчивается на кадре разбора
seg(6,10,FREEZE_T-(fr(10)-fr(6))/FPS,f"{ZC},{PLATE}",src=0)
# T: РАЗБОР
seg(10,18,0,f"FILE:{W}/tseg.mp4")
# B2: обыгрыш вплотную — слоу-мо
seg(18,20,6.45,f"{crop()},{Z(1.1,360,520,0.005)},{MI}",speed=0.45,src=1)
# D: удар мимо вратаря — слоу-мо
seg(20,23,6.75,f"{crop()},{Z(1.2,300,560,0.004)},{MI},{WF}",speed=0.39,src=1)
# E: ГОЛ на дроп — мяч в сетке
seg(23,25,7.3,f"{crop()},{shake(26)},{SCORE},"
    f"{txt('ГОООЛ',150,330,FONTI,':enable=lt(t\\,0.75)')},fade=in:st=0:d=0.3:color=white",speed=0.6,src=1)
# R: тот же гол с другой трибуны и ещё раз вплотную
seg(25,28,3.05,f"{ZC},{Z(1.05,500,500,0.002)},{MI},{WF},{SCORE}",speed=0.583,src=0)
seg(28,31,6.3,f"{crop()},{Z(1.25,330,560,0.003)},{MI},{WF},{SCORE}",speed=0.583,src=1)
seg(31,34,3.2,f"{ZC},{Z(1.4,500,470,0.003)},{MI},{WF},{SCORE}",speed=0.5,src=0)
# КИНО-повтор
seg(34,38,0,f"FILE:{W}/cine.mp4")
# F: празднование — к фотографам, прыжок через камеры, руки вверх
for b0,ss,cr,z,src in ((38,10.5,crop(),1.1,1),(40,11.6,crop(),1.1,1),
                       (42,13.0,"crop=480:640:200:160,scale=720:960:flags=lanczos,setsar=1",1.05,1),(44,6.3,ZC,1.0,0)):
    seg(b0,b0+2,ss,f"{cr},{shake(14)},zoompan=z='{z}+0.003*on':d=1:x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2':s=720x960:fps=30,{WF},{SCORE}",src=src)
# H: карточка в дыму
seg(46,51,0,f"FILE:{W}/card.mp4")

if __name__=='__main__':
    prebuilt()
    parts=[]
    for i,(b0,b1,ss,speed,vf,src) in enumerate(segs):
        n=fr(b1)-fr(b0)
        if vf.startswith('FILE:'): parts.append(vf[5:]); continue
        p=f"{W}/nseg{i:02d}.mp4"; parts.append(p)
        pre=f"setpts=(PTS-STARTPTS)/{speed}," if speed!=1 else ""
        inp=['-ss',f'{ss:.3f}','-t',str(n/FPS*speed+0.3),'-i',SRCS[src]]
        run(['ffmpeg','-v','error','-y',*inp,'-vf',f"{pre}fps={FPS},{vf},{GRADE},format=yuv420p",'-an','-frames:v',str(n),'-r',str(FPS),
             '-c:v','libx264','-crf','16','-preset','medium',p])
        print('seg',i,n,'frames',flush=True)
    with open(f"{W}/nlist.txt",'w') as f: f.writelines(f"file '{p}'\n" for p in parts)
    run(['ffmpeg','-v','error','-y','-f','concat','-safe','0','-i',f"{W}/nlist.txt",'-c:v','libx264','-crf','16','-preset','medium','-pix_fmt','yuv420p','-r','30',f'{W}/edit_v.mp4'])
    print('done video')
