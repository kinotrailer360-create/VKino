# Гол Роналду за «Аль-Наср»: монтаж 720x960, 30 fps, склейки по долям трека 140 BPM (на основе autobot/templates/build_template.py)
# Источники (после delogo): src0 — трибуна высоко сбоку (широкий), src1 — трибуна сбоку (широкий), src2 — у поля вплотную
import subprocess, sys, os, json
W=os.path.dirname(os.path.abspath(__file__)); R='/home/user/VKino/branding'
SRCS=[f'{W}/src0.mp4',f'{W}/src1.mp4',f'{W}/src2.mp4']
B=60/140; FPS=30
FONT='/usr/share/fonts/opentype/inter/InterDisplay-Black.otf'
FONTI='/usr/share/fonts/opentype/inter/InterDisplay-BlackItalic.otf'
GRADE='eq=saturation=1.3:contrast=1.12:brightness=0.01,unsharp=5:5:0.6,vignette=PI/5'
def fr(beat): return round(beat*B*FPS)
def run(cmd):
    r=subprocess.run(cmd,capture_output=True,text=True)
    if r.returncode: print(' '.join(cmd)[:300]); print(r.stderr[-2000:]); sys.exit(1)
def txt(t,size,y,font=FONTI,extra=''):
    return (f"drawtext=fontfile={font}:text='{t}':fontsize={size}:fontcolor=white:"
            f"borderw=5:bordercolor=black@0.85:x=(w-tw)/2:y={y}{extra}")
PLATE=("drawbox=x=(iw-360)/2:y=70:w=360:h=62:color=0x111111@0.92:t=fill,"
       "drawbox=x=(iw-360)/2:y=126:w=360:h=6:color=0xFFD200@1:t=fill,"
       f"drawtext=fontfile={FONTI}:text='C.RONALDO':fontsize=44:fontcolor=white:x=(w-tw)/2+14:y=80,"
       f"drawtext=fontfile={FONT}:text='7':fontsize=28:fontcolor=0xFFD200:x=(w-360)/2+14:y=86")
def crop(y,h=960): return f"crop=720:{h}:0:{y},scale=720:960:flags=lanczos,setsar=1"
def shake(a): return (f"scale=792:1056,crop=720:960:x='36+{a}*sin(t*71)*exp(-t*2.2)':"
                      f"y='48+{a}*cos(t*53)*exp(-t*2.2)'")
Z=lambda z,cx,cy,dz=0: f"zoompan=z='{z}+{dz}*on':d=1:x='{cx}-{cx}/zoom':y='{cy}-{cy}/zoom':s=720x960:fps=30"
MI="minterpolate=fps=30:mi_mode=mci:mc_mode=aobmc"
WF="fade=in:st=0:d=0.1:color=white"
segs=[]
def seg(b0,b1,ss,vf,speed=1.0,src=0): segs.append((b0,b1,ss,speed,vf,src))

# --- готовые вставки -------------------------------------------------------
FREEZE_T=3.86   # кадр разбора: мяч летит к Роналду (src2)
def prebuilt():
    # РАЗБОР: стоп-кадр + телестрация
    run(['ffmpeg','-v','error','-y','-ss',str(FREEZE_T),'-i',SRCS[2],'-vf',f"{crop(160)},{GRADE}",'-frames:v','1',f'{W}/freeze.png'])
    dur=(fr(18)-fr(10))/FPS
    spec={'duration':dur,'elements':[
        {'type':'beam','pos':[425,640],'start':0.25,'label':'РОНАЛДУ'},
        {'type':'lens','pos':[390,430],'r':95,'zoom':1.9,'start':0.8,'at':[580,190]},
        {'type':'arrow','pts':[[715,560],[512,548]],'start':1.5,'curve':0.08},
        {'type':'arrow','pts':[[500,575],[30,470]],'start':2.25,'curve':-0.12}]}
    json.dump(spec,open(f'{W}/tspec.json','w'),ensure_ascii=False)
    os.makedirs(f'{W}/tele',exist_ok=True)
    run(['python3',f'{R}/telestrate.py',f'{W}/freeze.png',f'{W}/tele',f'{W}/tspec.json'])
    n=fr(18)-fr(10)
    run(['ffmpeg','-v','error','-y','-loop','1','-i',f'{W}/freeze.png','-framerate','30','-i',f'{W}/tele/t%04d.png','-filter_complex',
         f"[0]scale=720:960,setsar=1,format=rgba[f];[f][1]overlay=format=auto,"
         f"drawbox=x=24:y=60:w=150:h=50:color=0xFFD200@1:t=fill,"
         f"drawtext=fontfile={FONT}:text='РАЗБОР':fontsize=32:fontcolor=black:x=38:y=68,"
         f"drawtext=fontfile={FONTI}:text='В КАСАНИЕ':fontsize=40:fontcolor=0xFFD200:borderw=4:bordercolor=black:x=(w-tw)/2:y=840:enable='gte(t\\,2.6)',format=yuv420p",
         '-frames:v',str(n),'-r','30','-c:v','libx264','-crf','16','-preset','medium',f'{W}/tseg.mp4'])
    # КИНО-повтор: широкий план, удар → сетка (src0)
    run(['ffmpeg','-v','error','-y','-ss','4.95','-t','1.2','-i',SRCS[0],'-an','-c:v','libx264','-crf','14',f'{W}/cine_in.mp4'])
    d=(fr(38)-fr(34))/FPS
    run(['bash',f'{R}/cinematic.sh',f'{W}/cine_in.mp4',f'{W}/cine.mp4',
         f"setpts=PTS/0.45,fps=30,crop=540:720:0:300,scale=720:960:flags=lanczos,setsar=1",str(d)])
    # карточка в дыму: Роналду кричит после гола (src2)
    run(['ffmpeg','-v','error','-y','-ss','21.7','-t','1.4','-i',SRCS[2],'-an','-c:v','libx264','-crf','14',f'{W}/card_in.mp4'])
    d=(fr(51)-fr(46))/FPS
    run(['bash',f'{R}/cinematic.sh',f'{W}/card_in.mp4',f'{W}/card_raw.mp4',
         f"setpts=PTS/0.6,fps=30,{crop(200)}",str(d)])
    run(['ffmpeg','-v','error','-y','-i',f'{W}/card_raw.mp4','-vf',
         f"{txt('КРИШТИАНУ РОНАЛДУ',54,700,FONTI,':alpha=min(1\\,t/0.4)')},"
         f"drawtext=fontfile={FONT}:text='ГОЛ В КАСАНИЕ · АЛЬ-НАСР':fontsize=32:fontcolor=0xFFD200:borderw=3:bordercolor=black:x=(w-tw)/2:y=785:alpha='min(1,max(0,(t-0.25)/0.4))',format=yuv420p",
         '-c:v','libx264','-crf','16','-preset','medium',f'{W}/card.mp4'])

# --- таймлайн ---------------------------------------------------------------
# A: розыгрыш на фланге (широкий)
seg(0,6,0.2,f"{crop(100)},{Z(1.1,360,500,0.001)}",src=0)
# B: Роналду занимает позицию у штрафной — заканчивается на кадре разбора
seg(6,10,FREEZE_T-(fr(10)-fr(6))/FPS,f"{crop(160)},{PLATE}",src=2)
# T: РАЗБОР
seg(10,18,0,f"FILE:{W}/tseg.mp4")
# B2: удар в касание — слоу-мо вплотную
seg(18,20,FREEZE_T,f"{crop(160)},{Z(1.0,450,560,0.006)},{MI}",speed=0.45,src=2)
# D: полёт мяча — слоу-мо с широкой трибуны
seg(20,23,5.0,f"{crop(200)},{Z(1.35,260,330,0.004)},{MI},{WF}",speed=0.39,src=0)
# E: ГОЛ на дроп — мяч в сетке
seg(23,25,4.96,f"{crop(300)},{shake(26)},"
    f"{txt('ГОООЛ',150,330,FONTI,':enable=lt(t\\,0.75)')},fade=in:st=0:d=0.3:color=white",speed=0.4,src=2)
# R: тот же гол с других трибун
seg(25,28,2.95,f"crop=465:620:0:80,scale=720:960:flags=lanczos,setsar=1,{Z(1.05,330,450,0.003)},{MI},{WF}",speed=0.583,src=1)
seg(28,31,3.72,f"{crop(160)},{Z(1.15,470,560,0.003)},{MI},{WF}",speed=0.583,src=2)
seg(31,34,4.95,f"{crop(200)},{Z(1.6,200,330,0.003)},{MI},{WF}",speed=0.583,src=0)
# КИНО-повтор
seg(34,38,0,f"FILE:{W}/cine.mp4")
# F: празднование
for b0,ss,cr,z,src in ((38,9.6,crop(200),1.1,2),(40,11.0,crop(200),1.15,2),(42,10.0,crop(250),1.25,0),(44,22.0,crop(200),1.05,2)):
    seg(b0,b0+2,ss,f"{cr},{shake(14)},zoompan=z='{z}+0.003*on':d=1:x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2':s=720x960:fps=30,{WF}",src=src)
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
