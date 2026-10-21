# Гол Фабиана Руиса ПСЖ — Барселона (4:2, 86'): монтаж 720x960, 30 fps, склейки по долям трека 140 BPM. Без дымки, со слежением за мячом.
# Источники (720x960): src0 — трибуна высоко у углового (проход №2, пас, гол; водяной знак заблюрен, подпись обрезана),
#            src1 — дальняя трибуна (подпись обрезана), src2 — празднование
import subprocess, sys, os, json
W=os.path.dirname(os.path.abspath(__file__)); R='/home/user/VKino/branding'
SRCS=[f'{W}/src0.mp4',f'{W}/src1.mp4',f'{W}/src2.mp4']
B=60/140; FPS=30
FONT='/usr/share/fonts/opentype/inter/InterDisplay-Black.otf'
FONTI='/usr/share/fonts/opentype/inter/InterDisplay-BlackItalic.otf'
GRADE='eq=saturation=0.95:contrast=1.08:brightness=-0.02,unsharp=5:5:0.6,vignette=PI/5'
NAVY='0x0A1F44'; RED='0xDA291C'; GOLD='0xEDBB00'
def fr(beat): return round(beat*B*FPS)
def run(cmd):
    r=subprocess.run(cmd,capture_output=True,text=True)
    if r.returncode: print(' '.join(cmd)[:300]); print(r.stderr[-2000:]); sys.exit(1)
def txt(t,size,y,font=FONTI,extra=''):
    return (f"drawtext=fontfile={font}:text='{t}':fontsize={size}:fontcolor=white:"
            f"borderw=5:bordercolor=black@0.85:x=(w-tw)/2:y={y}{extra}")
PLATE=("drawbox=x=(iw-360)/2:y=70:w=360:h=62:color=0x111111@0.92:t=fill,"
       f"drawbox=x=(iw-360)/2:y=126:w=360:h=6:color={RED}@1:t=fill,"
       f"drawtext=fontfile={FONTI}:text='A.HAKIMI':fontsize=44:fontcolor=white:x=(w-tw)/2+14:y=81,"
       f"drawtext=fontfile={FONT}:text='2':fontsize=28:fontcolor={RED}:x=(w-360)/2+12:y=87")
SCORE=(f"drawbox=x=(iw-420)/2:y=60:w=420:h=56:color=0x0B0B14@0.9:t=fill,"
       f"drawbox=x=(iw-420)/2:y=112:w=210:h=5:color={NAVY}@1:t=fill,drawbox=x=iw/2:y=112:w=210:h=5:color={RED}@1:t=fill,"
       f"drawtext=fontfile={FONT}:text='ПСЖ  4\\:2  БАРСЕЛОНА':fontsize=30:fontcolor=white:x=(w-420)/2+18:y=72,"
       f"drawtext=fontfile={FONT}:text='86’':fontsize=30:fontcolor={GOLD}:x=(w+420)/2-58:y=72")
def shake(a): return (f"scale=792:1056,crop=720:960:x='36+{a}*sin(t*71)*exp(-t*2.2)':"
                      f"y='48+{a}*cos(t*53)*exp(-t*2.2)'")
Z=lambda z,cx,cy,dz=0: f"zoompan=z='{z}+{dz}*on':d=1:x='{cx}-{cx}/zoom':y='{cy}-{cy}/zoom':s=720x960:fps=30"
MI="minterpolate=fps=30:mi_mode=mci:mc_mode=aobmc"
WF="fade=in:st=0:d=0.1:color=white"
segs=[]
def seg(b0,b1,ss,vf,speed=1.0,src=0): segs.append((b0,b1,ss,speed,vf,src))

FREEZE_T=5.6   # кадр разбора (src0): №2 прострелом отдаёт под удар
def prebuilt():
    run(['ffmpeg','-v','error','-y','-ss',str(FREEZE_T),'-i',SRCS[0],'-vf',f"{GRADE}",'-frames:v','1',f'{W}/freeze.png'])
    n=fr(18)-fr(10); dur=n/FPS
    spec={'duration':dur,'elements':[
        {'type':'arrow','pts':[[300,560],[270,370]],'start':0.25,'curve':0.1},
        {'type':'arrow','pts':[[265,345],[470,170]],'start':1.4,'curve':-0.12},
        {'type':'zone','poly':[[380,60],[560,40],[600,190],[420,220]],'start':2.0,'label':'БЛИЖНИЙ'}]}
    json.dump(spec,open(f'{W}/tspec.json','w'),ensure_ascii=False)
    os.makedirs(f'{W}/tele',exist_ok=True)
    run(['python3',f'{R}/telestrate.py',f'{W}/freeze.png',f'{W}/tele',f'{W}/tspec.json'])
    run(['ffmpeg','-v','error','-y','-loop','1','-i',f'{W}/freeze.png','-framerate','30','-i',f'{W}/tele/t%04d.png','-filter_complex',
         f"[0]scale=720:960,setsar=1,format=rgba[f];[f][1]overlay=format=auto,"
         f"drawbox=x=24:y=60:w=150:h=50:color={GOLD}@1:t=fill,"
         f"drawtext=fontfile={FONT}:text='РАЗБОР':fontsize=32:fontcolor=black:x=38:y=68,"
         f"drawtext=fontfile={FONTI}:text='ПРОСТРЕЛ ХАКИМИ — ГОЛ':fontsize=40:fontcolor={GOLD}:borderw=4:bordercolor=black:x=(w-tw)/2:y=800:enable='gte(t\\,2.4)',format=yuv420p",
         '-frames:v',str(n),'-r','30','-c:v','libx264','-crf','16','-preset','medium',f'{W}/tseg.mp4'])
    BT=['python3',f'{R}/balltrack.py','--color','DA291C']
    run(BT+[SRCS[0],f'{W}/bt_a.mp4',f'{W}/ptsA.json','--ss','1.3','--frames',str(fr(6)-fr(0)),'--zoom','1.3','--post',GRADE])
    nb=fr(10)-fr(6); run(BT+[SRCS[0],f'{W}/bt_b.mp4',f'{W}/ptsA.json','--ss',f'{FREEZE_T-nb/FPS:.3f}','--frames',str(nb),
        '--zoom','1.35','--post',f'{GRADE},{PLATE}'])
    run(BT+[SRCS[0],f'{W}/bt_b2.mp4',f'{W}/ptsD.json','--ss',str(FREEZE_T),'--speed','0.5','--frames',str(fr(20)-fr(18)),
        '--zoom','1.35','--pre',MI,'--post',GRADE,'--smooth','3'])
    run(BT+[SRCS[0],f'{W}/bt_d.mp4',f'{W}/ptsD.json','--ss','6.0','--speed','0.45','--frames',str(fr(23)-fr(20)),
        '--zoom','1.45','--pre',MI,'--post',GRADE,'--smooth','4','--marker-end','6.5'])
    # ГОЛ на дроп: мяч уже в сетке у ближней штанги — зум, шейк/флеш/ГОООЛ только тут
    run(['ffmpeg','-v','error','-y','-ss','6.5','-t','0.8','-i',SRCS[0],'-vf',
         f"setpts=(PTS-STARTPTS)/0.5,fps=30,{MI},{Z(1.7,500,380,0.004)},{GRADE},{shake(20)},{SCORE},"
         f"{txt('ГОООЛ',150,250,FONTI,':enable=lt(t\\,0.75)')},fade=in:st=0:d=0.3:color=white,format=yuv420p",
         '-an','-frames:v',str(fr(25)-fr(23)),'-r','30','-c:v','libx264','-crf','16','-preset','medium',f'{W}/eseg.mp4'])
    # КИНО-повтор (без тумана)
    run(['ffmpeg','-v','error','-y','-ss','5.9','-t','1.0','-i',SRCS[0],'-an','-c:v','libx264','-crf','14',f'{W}/cine_in.mp4'])
    d=(fr(38)-fr(34))/FPS
    run(['bash',f'{R}/cinematic.sh',f'{W}/cine_in.mp4',f'{W}/cine.mp4',f"setpts=PTS/0.45,fps=30,{Z(1.3,380,380,0.002)}",str(d)])
    # финальная карточка (без дыма)
    run(['ffmpeg','-v','error','-y','-ss','13.0','-t','1.4','-i',SRCS[2],'-an','-c:v','libx264','-crf','14',f'{W}/card_in.mp4'])
    d=(fr(51)-fr(46))/FPS
    run(['bash',f'{R}/cinematic.sh',f'{W}/card_in.mp4',f'{W}/card_raw.mp4',f"setpts=PTS/0.6,fps=30,{Z(1.3,380,420,0.002)}",str(d)])
    run(['ffmpeg','-v','error','-y','-i',f'{W}/card_raw.mp4','-vf',
         f"drawbox=x=0:y=660:w=iw:h=170:color=black@0.45:t=fill,"
         f"{txt('ФАБИАН РУИС',76,680,FONTI,':alpha=min(1\\,t/0.4)')},"
         f"drawtext=fontfile={FONT}:text='86’ · 4\\:2 · пас\\: Хакими':fontsize=32:fontcolor={GOLD}:borderw=3:bordercolor=black:x=(w-tw)/2:y=780:alpha='min(1,max(0,(t-0.25)/0.4))',format=yuv420p",
         '-c:v','libx264','-crf','16','-preset','medium',f'{W}/card.mp4'])

seg(0,6,0,f"FILE:{W}/bt_a.mp4")     # проход Хакими, камера за мячом
seg(6,10,0,f"FILE:{W}/bt_b.mp4")    # обыгрыш, плашка — до кадра разбора
seg(10,18,0,f"FILE:{W}/tseg.mp4")   # РАЗБОР
seg(18,20,0,f"FILE:{W}/bt_b2.mp4")  # прострел — slow-mo
seg(20,23,0,f"FILE:{W}/bt_d.mp4")   # удар и влёт — slow-mo, маркер до сетки
seg(23,25,0,f"FILE:{W}/eseg.mp4")   # ГОЛ на дроп
seg(25,28,5.0,f"{Z(1.15,330,450,0.003)},{MI},{WF},{SCORE}",speed=0.583,src=0)
seg(28,31,5.5,f"{Z(1.25,360,420,0.003)},{MI},{WF},{SCORE}",speed=0.583,src=0)
seg(31,34,6.3,f"{Z(1.5,430,400,0.003)},{MI},{WF},{SCORE}",speed=0.583,src=0)
seg(34,38,0,f"FILE:{W}/cine.mp4")
for b0,ss,z,src in ((38,0.5,1.15,2),(40,4.5,1.2,2),(42,12.5,1.3,2),(44,16.0,1.3,2)):
    seg(b0,b0+2,ss,f"{shake(14)},zoompan=z='{z}+0.003*on':d=1:x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2':s=720x960:fps=30,{WF},{SCORE}",src=src)
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
