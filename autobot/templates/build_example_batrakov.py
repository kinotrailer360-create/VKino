# Гол Батракова Лилль — Галатасарай (2:2, 44'): комбинация + гол, повторы со всех ракурсов, без кино-режима: монтаж 720x960, 30 fps, склейки по долям трека 140 BPM. Без дымки, со слежением за мячом.
# Источники (720x960): src0 — трибуна, крупно за игрой (вся атака, навес, удар головой №83), src1 — высоко, широкий,
#            src2 — у углового (подпись автора стёрта), src3 — за воротами сквозь сетку (влёт; водяной знак стёрт)
import subprocess, sys, os, json
W=os.path.dirname(os.path.abspath(__file__)); R='/home/user/VKino/branding'
SRCS=[f'{W}/src0.mp4',f'{W}/src1.mp4',f'{W}/src2.mp4',f'{W}/src3.mp4']
B=60/140; FPS=30
FONT='/usr/share/fonts/opentype/inter/InterDisplay-Black.otf'
FONTI='/usr/share/fonts/opentype/inter/InterDisplay-BlackItalic.otf'
GRADE='eq=saturation=0.95:contrast=1.08:brightness=-0.02,unsharp=5:5:0.6,vignette=PI/5'
NAVY='0xA90432'; RED='0xFDB912'; GOLD='0xFDB912'   # Галатасарай: красный/жёлтый
def fr(beat): return round(beat*B*FPS)
def run(cmd):
    r=subprocess.run(cmd,capture_output=True,text=True)
    if r.returncode: print(' '.join(cmd)[:300]); print(r.stderr[-2000:]); sys.exit(1)
def txt(t,size,y,font=FONTI,extra=''):
    return (f"drawtext=fontfile={font}:text='{t}':fontsize={size}:fontcolor=white:"
            f"borderw=5:bordercolor=black@0.85:x=(w-tw)/2:y={y}{extra}")
PLATE=("drawbox=x=(iw-360)/2:y=70:w=360:h=62:color=0x111111@0.92:t=fill,"
       f"drawbox=x=(iw-360)/2:y=126:w=360:h=6:color={RED}@1:t=fill,"
       f"drawtext=fontfile={FONTI}:text='A.BATRAKOV':fontsize=40:fontcolor=white:x=(w-tw)/2+14:y=81,"
       f"drawtext=fontfile={FONT}:text='83':fontsize=28:fontcolor={RED}:x=(w-360)/2+12:y=87")
SCORE=(f"drawbox=x=(iw-540)/2:y=60:w=540:h=56:color=0x0B0B14@0.9:t=fill,"
       f"drawbox=x=(iw-540)/2:y=112:w=270:h=5:color={NAVY}@1:t=fill,drawbox=x=iw/2:y=112:w=270:h=5:color={RED}@1:t=fill,"
       f"drawtext=fontfile={FONT}:text='ЛИЛЛЬ  2\\:2  ГАЛАТАСАРАЙ':fontsize=29:fontcolor=white:x=(w-540)/2+18:y=73,"
       f"drawtext=fontfile={FONT}:text='44’':fontsize=29:fontcolor={GOLD}:x=(w+540)/2-62:y=73")
def shake(a): return (f"scale=792:1056,crop=720:960:x='36+{a}*sin(t*71)*exp(-t*2.2)':"
                      f"y='48+{a}*cos(t*53)*exp(-t*2.2)'")
Z=lambda z,cx,cy,dz=0: f"zoompan=z='{z}+{dz}*on':d=1:x='{cx}-{cx}/zoom':y='{cy}-{cy}/zoom':s=720x960:fps=30"
MI="minterpolate=fps=30:mi_mode=mci:mc_mode=aobmc"
WF="fade=in:st=0:d=0.1:color=white"
segs=[]
def seg(b0,b1,ss,vf,speed=1.0,src=0): segs.append((b0,b1,ss,speed,vf,src))

def prebuilt():
    # РАЗБОР: навес с фланга → Батраков (№83) открылся → удар головой
    run(['ffmpeg','-v','error','-y','-ss','9.65','-i',SRCS[0],'-vf',f"{GRADE}",'-frames:v','1',f'{W}/freeze.png'])
    n=fr(28)-fr(20); dur=n/FPS
    spec={'duration':dur,'elements':[
        {'type':'arrow','pts':[[420,30],[371,150]],'start':0.25,'curve':0.05},
        {'type':'beam','pos':[380,760],'start':0.9,'label':'БАТРАКОВ'},
        {'type':'arrow','pts':[[371,170],[385,640]],'start':1.6,'curve':0.08},
        {'type':'arrow','pts':[[385,660],[40,900]],'start':2.4,'curve':-0.1}]}
    json.dump(spec,open(f'{W}/tspec.json','w'),ensure_ascii=False)
    os.makedirs(f'{W}/tele',exist_ok=True)
    run(['python3',f'{R}/telestrate.py',f'{W}/freeze.png',f'{W}/tele',f'{W}/tspec.json'])
    run(['ffmpeg','-v','error','-y','-loop','1','-i',f'{W}/freeze.png','-framerate','30','-i',f'{W}/tele/t%04d.png','-filter_complex',
         f"[0]scale=720:960,setsar=1,format=rgba[f];[f][1]overlay=format=auto,"
         f"drawbox=x=24:y=60:w=150:h=50:color={GOLD}@1:t=fill,"
         f"drawtext=fontfile={FONT}:text='РАЗБОР':fontsize=32:fontcolor=black:x=38:y=68,"
         f"drawtext=fontfile={FONTI}:text='НАВЕС — И ГОЛОВОЙ':fontsize=42:fontcolor={GOLD}:borderw=4:bordercolor=black:x=(w-tw)/2:y=140:enable='gte(t\\,2.6)',format=yuv420p",
         '-frames:v',str(n),'-r','30','-c:v','libx264','-crf','16','-preset','medium',f'{W}/tseg.mp4'])
    BT=['python3',f'{R}/balltrack.py','--color','FDB912']
    # КОМБИНАЦИЯ целиком (ракурс 1): камера за игрой, маркер — с момента, где мяч размечен покадрово
    run(BT+[SRCS[0],f'{W}/bt_a.mp4',f'{W}/pts0.json','--ss','2.0','--frames',str(fr(16)),'--zoom','1.2','--smooth','6','--marker-start','8.4','--post',GRADE])
    nb=fr(20)-fr(16); run(BT+[SRCS[0],f'{W}/bt_b.mp4',f'{W}/pts0.json','--ss',f'{2.0+fr(16)/FPS:.3f}','--speed','0.46','--frames',str(nb),
        '--zoom','1.3','--pre',MI,'--marker-start','8.4','--post',f'{GRADE},{PLATE}'])
    run(BT+[SRCS[0],f'{W}/bt_h.mp4',f'{W}/pts0.json','--ss','9.65','--speed','0.43','--frames',str(fr(31)-fr(28)),
        '--zoom','1.35','--pre',MI,'--post',GRADE,'--smooth','3'])
    # ВЛЁТ из-за ворот (ракурс 4): slow-mo 0.4x, камера за мячом, маркер до касания сетки
    run(BT+[SRCS[3],f'{W}/bt_d.mp4',f'{W}/pts3.json','--ss','2.35','--speed','0.4','--frames',str(fr(35)-fr(31)),
        '--zoom','1.45','--pre',MI,'--post',GRADE,'--smooth','3','--marker-end','2.95'])
    # ГОЛ на дроп: мяч в сетке крупно; ГОООЛ/флеш/шейк только после касания сетки (2.95 c → t≈0.14 c)
    tn=round((2.95-2.9)/0.35,2)
    run(['python3',f'{R}/balltrack.py',SRCS[3],f'{W}/eseg_raw.mp4',f'{W}/pts3.json','--ss','2.9','--speed','0.35','--frames',str(fr(37)-fr(35)),
         '--zoom','1.6','--lead','0','--smooth','2','--pre',MI,'--marker-end','0','--post',GRADE])
    run(['ffmpeg','-v','error','-y','-i',f'{W}/eseg_raw.mp4','-vf',
         f"{SCORE},drawbox=x=0:y=0:w=iw:h=ih:color=white@0.8:t=fill:enable='between(t,{tn},{tn+0.07})',"
         f"drawbox=x=0:y=0:w=iw:h=ih:color=white@0.35:t=fill:enable='between(t,{tn+0.07},{tn+0.17})',"
         f"{txt('ГОООЛ',150,200,FONTI,':enable=gte(t\\,'+str(tn)+')')},format=yuv420p",
         '-an','-c:v','libx264','-crf','16','-preset','medium',f'{W}/eseg.mp4'])
    # ПОВТОРЫ (п.9): вся комбинация + гол с каждого ракурса
    run(BT+[SRCS[0],f'{W}/r0.mp4',f'{W}/pts0.json','--ss','7.0','--frames',str(fr(71)-fr(63)),'--zoom','1.25','--smooth','5','--marker-start','8.4','--marker-end','10.3','--post',f'{GRADE},{SCORE},{WF}'])
    # финальная карточка (без кино-режима и без дыма): Батраков после гола
    run(['ffmpeg','-v','error','-y','-ss','11.6','-t','1.4','-i',SRCS[0],'-an','-c:v','libx264','-crf','14',f'{W}/card_in.mp4'])
    d=(fr(84)-fr(79))/FPS
    run(['ffmpeg','-v','error','-y','-i',f'{W}/card_in.mp4','-vf',f"setpts=PTS/0.6,fps=30,crop=720:800:0:0,scale=720:960,{Z(1.1,360,420,0.002)},{GRADE},format=yuv420p",'-t',str(d),'-an','-c:v','libx264','-crf','14',f'{W}/card_raw.mp4'])
    run(['ffmpeg','-v','error','-y','-i',f'{W}/card_raw.mp4','-vf',
         f"drawbox=x=0:y=660:w=iw:h=170:color=black@0.45:t=fill,"
         f"{txt('АЛЕКСЕЙ БАТРАКОВ',58,684,FONTI,':alpha=min(1\\,t/0.4)')},"
         f"drawtext=fontfile={FONT}:text='44’ · 2\\:2 · пас\\: Шаллаи':fontsize=32:fontcolor={GOLD}:borderw=3:bordercolor=black:x=(w-tw)/2:y=780:alpha='min(1,max(0,(t-0.25)/0.4))',format=yuv420p",
         '-c:v','libx264','-crf','16','-preset','medium',f'{W}/card.mp4'])

seg(0,16,0,f"FILE:{W}/bt_a.mp4")    # КОМБИНАЦИЯ: атака Галатасарая, камера за игрой
seg(16,20,0,f"FILE:{W}/bt_b.mp4")   # навес с фланга, плашка — до кадра разбора
seg(20,28,0,f"FILE:{W}/tseg.mp4")   # РАЗБОР
seg(28,31,0,f"FILE:{W}/bt_h.mp4")   # удар головой — slow-mo, камера за мячом
seg(31,35,0,f"FILE:{W}/bt_d.mp4")   # ВЛЁТ из-за ворот — slow-mo, маркер до сетки
seg(35,37,0,f"FILE:{W}/eseg.mp4")   # ГОЛ на дроп
# ПОВТОРЫ: комбинация + гол с каждого ракурса по очереди
seg(37,45,0.0,f"{Z(1.12,360,420,0.002)},{MI},{WF},{SCORE}",speed=0.7,src=2)     # ракурс 3: у углового — навес, удар, гол
seg(45,55,2.0,f"{Z(1.3,420,380,0.002)},{WF},{SCORE}",speed=1.0,src=1)           # ракурс 2: высоко — вся атака целиком
seg(55,63,0.4,f"{Z(1.15,330,500,0.002)},{MI},{WF},{SCORE}",speed=0.76,src=3)    # ракурс 4: из-за ворот — атака, навес, гол
seg(63,71,0,f"FILE:{W}/r0.mp4")     # ракурс 1 ещё раз: навес и удар головой, камера за мячом
for b0,ss,z in ((71,10.9,1.1),(73,11.8,1.15),(75,12.7,1.1),(77,13.6,1.1)):
    seg(b0,b0+2,ss,f"crop=720:800:0:0,scale=720:960,{shake(14)},zoompan=z='{z}+0.003*on':d=1:x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2':s=720x960:fps=30,{WF},{SCORE}",src=0)
seg(79,84,0,f"FILE:{W}/card.mp4")

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
