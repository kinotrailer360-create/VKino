# Гол Эспарта ПСЖ — Барселона (1:1, 55'): монтаж 720x960, 30 fps, склейки по долям трека 140 BPM. Без дымки/тумана.
# Источники (нормализованы в 720x960): src0 — трибуна сбоку у углового (широкий), src1 — трибуна сбоку вплотную (лого заблюрено, подпись сверху обрезана),
#            src2 — трибуна высоко за воротами (мяч в сетке через сетку)
import subprocess, sys, os, json
W=os.path.dirname(os.path.abspath(__file__)); R='/home/user/VKino/branding'
SRCS=[f'{W}/src0.mp4',f'{W}/src1.mp4',f'{W}/src2.mp4']
B=60/140; FPS=30
FONT='/usr/share/fonts/opentype/inter/InterDisplay-Black.otf'
FONTI='/usr/share/fonts/opentype/inter/InterDisplay-BlackItalic.otf'
GRADE='eq=saturation=0.82:contrast=1.08:brightness=-0.03:gamma_g=0.97,unsharp=5:5:0.6,vignette=PI/5'
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
       f"drawtext=fontfile={FONTI}:text='X.ESPART':fontsize=44:fontcolor=white:x=(w-tw)/2:y=80")
SCORE=(f"drawbox=x=(iw-420)/2:y=60:w=420:h=56:color=0x0B0B14@0.9:t=fill,"
       f"drawbox=x=(iw-420)/2:y=112:w=210:h=5:color={BLU}@1:t=fill,drawbox=x=iw/2:y=112:w=210:h=5:color={GAR}@1:t=fill,"
       f"drawtext=fontfile={FONT}:text='ПСЖ  1\\:1  БАРСЕЛОНА':fontsize=30:fontcolor=white:x=(w-420)/2+18:y=72,"
       f"drawtext=fontfile={FONT}:text='55’':fontsize=30:fontcolor={GOLD}:x=(w+420)/2-58:y=72")
def shake(a): return (f"scale=792:1056,crop=720:960:x='36+{a}*sin(t*71)*exp(-t*2.2)':"
                      f"y='48+{a}*cos(t*53)*exp(-t*2.2)'")
Z=lambda z,cx,cy,dz=0: f"zoompan=z='{z}+{dz}*on':d=1:x='{cx}-{cx}/zoom':y='{cy}-{cy}/zoom':s=720x960:fps=30"
MI="minterpolate=fps=30:mi_mode=mci:mc_mode=aobmc"
WF="fade=in:st=0:d=0.1:color=white"
segs=[]
def seg(b0,b1,ss,vf,speed=1.0,src=0): segs.append((b0,b1,ss,speed,vf,src))

FREEZE_T=1.7   # кадр разбора (src1): Эспарт с мячом, защитник ПСЖ летит в подкат
def ring_seg(out,src,ss,sp,keys,n,zoom,extra=''):
    """slow-mo + кольцо на мяче по ключевым точкам (время источника, x, y) + плавный зум"""
    kt=[((t-ss)/sp,x,y) for t,x,y in keys]
    def pw(i):
        e=str(kt[-1][i])
        for (t0,*a),(t1,*b) in reversed(list(zip(kt,kt[1:]))):
            e=f"if(lt(t,{t1:.3f}),{a[i-1]}+({b[i-1]}-{a[i-1]})*(t-{t0:.3f})/{t1-t0:.3f},{e})"
        return e
    run(['ffmpeg','-v','error','-y','-ss',str(ss),'-t',str(n/FPS*sp+0.3),'-i',SRCS[src],'-loop','1','-i',f'{W}/ring.png','-filter_complex',
         f"[0:v]setpts=(PTS-STARTPTS)/{sp},fps=30,{MI}[v];[1]format=rgba,fade=out:st={kt[-1][0]+0.05:.3f}:d=0.15:alpha=1[r];"
         f"[v][r]overlay=x='{pw(1)}-40':y='{pw(2)}-40':enable='between(t,{kt[0][0]:.3f},{kt[-1][0]+0.2:.3f})',"
         f"{zoom},{GRADE}{extra},format=yuv420p",
         '-an','-frames:v',str(n),'-r','30','-c:v','libx264','-crf','16','-preset','medium',out])

def prebuilt():
    run(['ffmpeg','-v','error','-y','-ss',str(FREEZE_T),'-i',SRCS[1],'-vf',f"{GRADE}",'-frames:v','1',f'{W}/freeze.png'])
    n=fr(18)-fr(10); dur=n/FPS
    spec={'duration':dur,'elements':[
        {'type':'beam','pos':[112,462],'start':0.25,'label':'ЭСПАРТ'},
        {'type':'arrow','pts':[[230,320],[150,430]],'start':1.0,'curve':0.1},
        {'type':'arrow','pts':[[150,458],[700,330]],'start':1.9,'curve':-0.12}]}
    json.dump(spec,open(f'{W}/tspec.json','w'),ensure_ascii=False)
    os.makedirs(f'{W}/tele',exist_ok=True)
    run(['python3',f'{R}/telestrate.py',f'{W}/freeze.png',f'{W}/tele',f'{W}/tspec.json'])
    run(['ffmpeg','-v','error','-y','-loop','1','-i',f'{W}/freeze.png','-framerate','30','-i',f'{W}/tele/t%04d.png','-filter_complex',
         f"[0]scale=720:960,setsar=1,format=rgba[f];[f][1]overlay=format=auto,"
         f"drawbox=x=24:y=60:w=150:h=50:color={GOLD}@1:t=fill,"
         f"drawtext=fontfile={FONT}:text='РАЗБОР':fontsize=32:fontcolor=black:x=38:y=68,"
         f"drawtext=fontfile={FONTI}:text='УДАР РАНЬШЕ ПОДКАТА':fontsize=40:fontcolor={GOLD}:borderw=4:bordercolor=black:x=(w-tw)/2:y=800:enable='gte(t\\,2.4)',format=yuv420p",
         '-frames:v',str(n),'-r','30','-c:v','libx264','-crf','16','-preset','medium',f'{W}/tseg.mp4'])
    # СЛЕЖЕНИЕ ЗА МЯЧОМ (branding/balltrack.py): атака, ведение, удар, влёт; маркер пропадает при касании сетки (src1 ≈2.22)
    BT=['python3',f'{R}/balltrack.py']
    run(BT+[SRCS[2],f'{W}/bt_a.mp4',f'{W}/ptsA.json','--ss','0.2','--frames',str(fr(6)-fr(0)),'--zoom','1.4','--post',GRADE])
    nb=fr(10)-fr(6); run(BT+[SRCS[1],f'{W}/bt_b.mp4',f'{W}/ptsB.json','--ss',f'{FREEZE_T-nb/FPS*0.95:.3f}','--speed','0.95','--frames',str(nb),
        '--zoom','1.35','--post',f'{GRADE},{PLATE}'])
    run(BT+[SRCS[1],f'{W}/bt_b2.mp4',f'{W}/ptsB.json','--ss',str(FREEZE_T),'--speed','0.45','--frames',str(fr(20)-fr(18)),
        '--zoom','1.4','--pre',MI,'--post',GRADE,'--smooth','3'])
    run(BT+[SRCS[1],f'{W}/bt_d.mp4',f'{W}/ptsB.json','--ss','1.82','--speed','0.35','--frames',str(fr(23)-fr(20)),
        '--zoom','1.45','--pre',MI,'--post',GRADE,'--smooth','4','--marker-end','2.22'])
    # ГОЛ на дроп: из-за ворот мяч уже в сетке — маркер уже исчез (касание сетки), зум на мяч; шейк/флеш/ГОООЛ только тут
    run(['ffmpeg','-v','error','-y','-ss','4.02','-t','0.8','-i',SRCS[2],'-vf',
         f"setpts=(PTS-STARTPTS)/0.5,fps=30,{MI},{Z(1.6,205,615,0.004)},{GRADE},{shake(20)},{SCORE},"
         f"{txt('ГОООЛ',150,250,FONTI,':enable=lt(t\\,0.75)')},fade=in:st=0:d=0.3:color=white,format=yuv420p",
         '-an','-frames:v',str(fr(25)-fr(23)),'-r','30','-c:v','libx264','-crf','16','-preset','medium',f'{W}/eseg.mp4'])
    # КИНО-повтор (без тумана): из-за ворот, мяч в сетке
    run(['ffmpeg','-v','error','-y','-ss','3.7','-t','1.0','-i',SRCS[2],'-an','-c:v','libx264','-crf','14',f'{W}/cine_in.mp4'])
    d=(fr(38)-fr(34))/FPS
    run(['bash',f'{R}/cinematic.sh',f'{W}/cine_in.mp4',f'{W}/cine.mp4',f"setpts=PTS/0.45,fps=30,{Z(1.2,250,560,0.002)}",str(d)])
    # финальная карточка (без дыма): празднование у углового
    run(['ffmpeg','-v','error','-y','-ss','14.0','-t','1.4','-i',SRCS[1],'-an','-c:v','libx264','-crf','14',f'{W}/card_in.mp4'])
    d=(fr(51)-fr(46))/FPS
    run(['bash',f'{R}/cinematic.sh',f'{W}/card_in.mp4',f'{W}/card_raw.mp4',f"setpts=PTS/0.6,fps=30,{Z(1.1,400,450,0.002)}",str(d)])
    run(['ffmpeg','-v','error','-y','-i',f'{W}/card_raw.mp4','-vf',
         f"drawbox=x=0:y=660:w=iw:h=170:color=black@0.45:t=fill,"
         f"{txt('ХАВИ ЭСПАРТ',76,680,FONTI,':alpha=min(1\\,t/0.4)')},"
         f"drawtext=fontfile={FONT}:text='55’ · 1\\:1 · ПСЖ — БАРСЕЛОНА':fontsize=32:fontcolor={GOLD}:borderw=3:bordercolor=black:x=(w-tw)/2:y=780:alpha='min(1,max(0,(t-0.25)/0.4))',format=yuv420p",
         '-c:v','libx264','-crf','16','-preset','medium',f'{W}/card.mp4'])

# --- таймлайн ---------------------------------------------------------------
# A: атака «Барсы» (из-за ворот, высоко)
seg(0,6,0,f"FILE:{W}/bt_a.mp4")   # атака: камера за мячом
# B: Эспарт ведёт мяч к штрафной (вплотную) — заканчивается на кадре разбора
seg(6,10,0,f"FILE:{W}/bt_b.mp4")   # ведение: камера за мячом + плашка
# T: РАЗБОР
seg(10,18,0,f"FILE:{W}/tseg.mp4")
# B2: удар под подкатом — слоу-мо
seg(18,20,0,f"FILE:{W}/bt_b2.mp4")   # удар: камера за мячом, slow-mo
# D: ВЛЁТ — кольцо на мяче (prebuilt)
seg(20,23,0,f"FILE:{W}/bt_d.mp4")   # влёт: камера за мячом, slow-mo, маркер до сетки
# E: ГОЛ на дроп — мяч в сетке из-за ворот; шейк/флеш только здесь, после сетки
seg(23,25,0,f"FILE:{W}/eseg.mp4")   # мяч в сетке; шейк/флеш/ГОООЛ только тут, после сетки
# R: повторы — широкий, второй влёт из-за ворот, удар вплотную
seg(25,28,1.9,f"{Z(1.35,560,400,0.002)},{MI},{WF},{SCORE}",speed=0.583,src=0)
seg(28,31,3.6,f"{Z(1.25,220,560,0.003)},{MI},{WF},{SCORE}",speed=0.583,src=2)
seg(31,34,1.75,f"{Z(1.1,450,380,0.003)},{MI},{WF},{SCORE}",speed=0.583,src=1)
# КИНО-повтор
seg(34,38,0,f"FILE:{W}/cine.mp4")
# F: празднование у углового
for b0,ss,z,src in ((38,9.0,1.1,1),(40,12.2,1.15,1),(42,14.0,1.1,1),(44,16.0,1.1,1)):
    seg(b0,b0+2,ss,f"{shake(14)},zoompan=z='{z}+0.003*on':d=1:x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2':s=720x960:fps=30,{WF},{SCORE}",src=src)
# H: финальная карточка (без дыма)
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
