# Гол Дуэ ПСЖ — Барселона (1:0, 38'): монтаж 720x960, 30 fps, склейки по долям трека 140 BPM
# Источники: src0 — трибуна высоко сбоку (720x1280, подпись автора внизу y>880 — не попадает в кадр, лого заблюрено),
#            src1 — у ворот (576x768 → 720x960), src2 — трибуна с флагами (576x1024, подпись сверху обрезана → 720x960)
import subprocess, sys, os, json
W=os.path.dirname(os.path.abspath(__file__)); R='/home/user/VKino/branding'
SRCS=[f'{W}/src0.mp4',f'{W}/src1.mp4',f'{W}/src2.mp4']
B=60/140; FPS=30
FONT='/usr/share/fonts/opentype/inter/InterDisplay-Black.otf'
FONTI='/usr/share/fonts/opentype/inter/InterDisplay-BlackItalic.otf'
GRADE='eq=saturation=1.1:contrast=1.1:brightness=0.0,unsharp=5:5:0.6,vignette=PI/5'
NAVY='0x0A1F44'; RED='0xDA291C'; GOLD='0xEDBB00'
def fr(beat): return round(beat*B*FPS)
def run(cmd):
    r=subprocess.run(cmd,capture_output=True,text=True)
    if r.returncode: print(' '.join(cmd)[:300]); print(r.stderr[-2000:]); sys.exit(1)
def txt(t,size,y,font=FONTI,extra=''):
    return (f"drawtext=fontfile={font}:text='{t}':fontsize={size}:fontcolor=white:"
            f"borderw=5:bordercolor=black@0.85:x=(w-tw)/2:y={y}{extra}")
PLATE=("drawbox=x=(iw-320)/2:y=70:w=320:h=62:color=0x111111@0.92:t=fill,"
       f"drawbox=x=(iw-320)/2:y=126:w=320:h=6:color={RED}@1:t=fill,"
       f"drawtext=fontfile={FONTI}:text='D.DOUÉ':fontsize=44:fontcolor=white:x=(w-tw)/2+14:y=80,"
       f"drawtext=fontfile={FONT}:text='14':fontsize=28:fontcolor={RED}:x=(w-320)/2+12:y=86")
SCORE=(f"drawbox=x=(iw-420)/2:y=60:w=420:h=56:color=0x0B0B14@0.9:t=fill,"
       f"drawbox=x=(iw-420)/2:y=112:w=210:h=5:color={NAVY}@1:t=fill,drawbox=x=iw/2:y=112:w=210:h=5:color={RED}@1:t=fill,"
       f"drawtext=fontfile={FONT}:text='ПСЖ  1\\:0  БАРСЕЛОНА':fontsize=30:fontcolor=white:x=(w-420)/2+18:y=72,"
       f"drawtext=fontfile={FONT}:text='38’':fontsize=30:fontcolor={GOLD}:x=(w+420)/2-58:y=72")
WIDE="crop=640:853:40:20,scale=720:960:flags=lanczos,setsar=1"     # src0 целиком, без подписи
ZA="crop=480:640:240:100,scale=720:960:flags=lanczos,setsar=1"     # src0 крупно: Дуэ, трое защитников, ворота
def shake(a): return (f"scale=792:1056,crop=720:960:x='36+{a}*sin(t*71)*exp(-t*2.2)':"
                      f"y='48+{a}*cos(t*53)*exp(-t*2.2)'")
Z=lambda z,cx,cy,dz=0: f"zoompan=z='{z}+{dz}*on':d=1:x='{cx}-{cx}/zoom':y='{cy}-{cy}/zoom':s=720x960:fps=30"
MI="minterpolate=fps=30:mi_mode=mci:mc_mode=aobmc"
WF="fade=in:st=0:d=0.1:color=white"
segs=[]
def seg(b0,b1,ss,vf,speed=1.0,src=0): segs.append((b0,b1,ss,speed,vf,src))

FREEZE_T=18.2   # кадр разбора (src0): Дуэ с мячом между тремя игроками «Барсы»
def prebuilt():
    run(['ffmpeg','-v','error','-y','-ss',str(FREEZE_T),'-i',SRCS[0],'-vf',f"{ZA},{GRADE}",'-frames:v','1',f'{W}/freeze.png'])
    n=fr(18)-fr(10); dur=n/FPS
    spec={'duration':dur,'elements':[
        {'type':'beam','pos':[296,535],'start':0.25,'label':'ДУЭ'},
        {'type':'zone','poly':[[40,380],[380,380],[390,470],[30,470]],'start':0.9,'label':'ТРОЕ'},
        {'type':'arrow','pts':[[300,525],[300,290]],'start':2.0,'curve':0.1}]}
    json.dump(spec,open(f'{W}/tspec.json','w'),ensure_ascii=False)
    os.makedirs(f'{W}/tele',exist_ok=True)
    run(['python3',f'{R}/telestrate.py',f'{W}/freeze.png',f'{W}/tele',f'{W}/tspec.json'])
    run(['ffmpeg','-v','error','-y','-loop','1','-i',f'{W}/freeze.png','-framerate','30','-i',f'{W}/tele/t%04d.png','-filter_complex',
         f"[0]scale=720:960,setsar=1,format=rgba[f];[f][1]overlay=format=auto,"
         f"drawbox=x=24:y=60:w=150:h=50:color={GOLD}@1:t=fill,"
         f"drawtext=fontfile={FONT}:text='РАЗБОР':fontsize=32:fontcolor=black:x=38:y=68,"
         f"drawtext=fontfile={FONTI}:text='1 ПРОТИВ 3 — И УДАР':fontsize=40:fontcolor={GOLD}:borderw=4:bordercolor=black:x=(w-tw)/2:y=800:enable='gte(t\\,2.6)',format=yuv420p",
         '-frames:v',str(n),'-r','30','-c:v','libx264','-crf','16','-preset','medium',f'{W}/tseg.mp4'])
    # ВЛЁТ: у ворот, slow-mo 0.4x, плавный зум к ближней штанге, кольцо на мяче до пересечения линии
    n=fr(23)-fr(20); sp=0.4; ss=5.5
    keys=[(5.55,240,480),(5.65,165,510),(5.75,125,563),(5.85,120,625),(5.95,92,660)]   # мяч в кадре 720x960 src1
    kt=[((t-ss)/sp,x,y) for t,x,y in keys]
    def pw(i):   # кусочно-линейная интерполяция координаты i по времени выхода
        e=str(kt[-1][i])
        for (t0,*a),(t1,*b) in reversed(list(zip(kt,kt[1:]))):
            e=f"if(lt(t,{t1:.3f}),{a[i-1]}+({b[i-1]}-{a[i-1]})*(t-{t0:.3f})/{t1-t0:.3f},{e})"
        return e
    run(['ffmpeg','-v','error','-y','-ss',str(ss),'-t',str(n/FPS*sp+0.3),'-i',SRCS[1],'-loop','1','-i',f'{W}/ring.png','-filter_complex',
         f"[0:v]setpts=(PTS-STARTPTS)/{sp},fps=30,{MI}[v];[1]format=rgba,fade=out:st={kt[-1][0]+0.05:.3f}:d=0.15:alpha=1[r];"
         f"[v][r]overlay=x='{pw(1)}-40':y='{pw(2)}-40':enable='between(t,{kt[0][0]:.3f},{kt[-1][0]+0.2:.3f})',"
         f"{Z(1.0,200,600,0.004)},{GRADE},format=yuv420p",
         '-an','-frames:v',str(n),'-r','30','-c:v','libx264','-crf','16','-preset','medium',f'{W}/dseg.mp4'])
    # КИНО-повтор: удар и мяч в воротах (src0, крупно)
    run(['ffmpeg','-v','error','-y','-ss','18.2','-t','1.2','-i',SRCS[0],'-an','-c:v','libx264','-crf','14',f'{W}/cine_in.mp4'])
    d=(fr(38)-fr(34))/FPS
    run(['bash',f'{R}/cinematic.sh',f'{W}/cine_in.mp4',f'{W}/cine.mp4',f"setpts=PTS/0.45,fps=30,{ZA}",str(d)])
    # карточка в дыму: Дуэ танцует перед камерой (src1)
    run(['ffmpeg','-v','error','-y','-ss','10.3','-t','1.4','-i',SRCS[1],'-an','-c:v','libx264','-crf','14',f'{W}/card_in.mp4'])
    d=(fr(51)-fr(46))/FPS
    run(['bash',f'{R}/cinematic.sh',f'{W}/card_in.mp4',f'{W}/card_raw.mp4',
         "setpts=PTS/0.6,fps=30,crop=480:640:120:200,scale=720:960:flags=lanczos,setsar=1",str(d)])
    run(['ffmpeg','-v','error','-y','-i',f'{W}/card_raw.mp4','-vf',
         f"{txt('ДЕЗИРЕ ДУЭ',76,690,FONTI,':alpha=min(1\\,t/0.4)')},"
         f"drawtext=fontfile={FONT}:text='38’ · 1\\:0 · ПСЖ — БАРСЕЛОНА':fontsize=32:fontcolor={GOLD}:borderw=3:bordercolor=black:x=(w-tw)/2:y=785:alpha='min(1,max(0,(t-0.25)/0.4))',format=yuv420p",
         '-c:v','libx264','-crf','16','-preset','medium',f'{W}/card.mp4'])

# --- таймлайн ---------------------------------------------------------------
# A: атака ПСЖ (широкий)
seg(0,6,15.0,f"{WIDE},{Z(1.1,360,400,0.001)}",src=0)
# B: Дуэ с мячом у штрафной — заканчивается на кадре разбора
seg(6,10,FREEZE_T-(fr(10)-fr(6))/FPS,f"{ZA},{PLATE}",src=0)
# T: РАЗБОР
seg(10,18,0,f"FILE:{W}/tseg.mp4")
# B2: обыгрыш вплотную — слоу-мо
seg(18,20,4.6,f"{Z(1.1,360,480,0.004)},{MI}",speed=0.5,src=1)
# D: удар — слоу-мо (крупно)
seg(20,23,0,f"FILE:{W}/dseg.mp4")   # влёт: слоу-мо у ворот + кольцо на мяче (prebuilt)
# E: ГОЛ на дроп — мяч в сетке (у ворот)
seg(23,25,6.3,f"{Z(1.35,340,580,0.004)},{shake(20)},{SCORE},"
    f"{txt('ГОООЛ',150,330,FONTI,':enable=lt(t\\,0.75)')},fade=in:st=0:d=0.3:color=white",speed=0.6,src=1)
# R: повторы с трибун
seg(25,28,18.3,f"{ZA},{Z(1.05,300,400,0.002)},{MI},{WF},{SCORE}",speed=0.583,src=0)
seg(28,31,6.25,f"{Z(1.5,340,580,0.003)},{MI},{WF},{SCORE}",speed=0.583,src=1)   # мяч в сетке крупно
seg(31,34,4.25,f"{Z(1.15,360,400,0.003)},{MI},{WF},{SCORE}",speed=0.583,src=2)
# КИНО-повтор
seg(34,38,0,f"FILE:{W}/cine.mp4")
# F: празднование — к камере, танец, партнёр запрыгивает, трибуны
for b0,ss,cr,z,src in ((38,8.0,"null",1.1,1),(40,10.4,"crop=480:640:120:200,scale=720:960:flags=lanczos,setsar=1",1.05,1),
                       (42,11.9,"null",1.1,1),(44,10.0,"null",1.1,2)):
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
