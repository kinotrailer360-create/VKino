# Гол Мбаппе Реал — РБ Лейпциг (3:0, 83'): монтаж 720x960, 30 fps, склейки по долям трека 140 BPM. Без дымки, со слежением за мячом.
# Источники (720x960): src0 — сверху за углом (вся комбинация + удар + влёт; водяные знаки заблюрены), src1 — дальняя трибуна (широкий),
#            src2 — трибуна сбоку, src3 — у ворот (празднование №10)
import subprocess, sys, os, json
W=os.path.dirname(os.path.abspath(__file__)); R='/home/user/VKino/branding'
SRCS=[f'{W}/src0.mp4',f'{W}/src1.mp4',f'{W}/src2.mp4',f'{W}/src3.mp4']
B=60/140; FPS=30
FONT='/usr/share/fonts/opentype/inter/InterDisplay-Black.otf'
FONTI='/usr/share/fonts/opentype/inter/InterDisplay-BlackItalic.otf'
GRADE='eq=saturation=0.95:contrast=1.08:brightness=-0.02,unsharp=5:5:0.6,vignette=PI/5'
NAVY='0x00529F'; RED='0xFEBE10'; GOLD='0xFEBE10'   # Реал: белый/синий/золото
def fr(beat): return round(beat*B*FPS)
def run(cmd):
    r=subprocess.run(cmd,capture_output=True,text=True)
    if r.returncode: print(' '.join(cmd)[:300]); print(r.stderr[-2000:]); sys.exit(1)
def txt(t,size,y,font=FONTI,extra=''):
    return (f"drawtext=fontfile={font}:text='{t}':fontsize={size}:fontcolor=white:"
            f"borderw=5:bordercolor=black@0.85:x=(w-tw)/2:y={y}{extra}")
PLATE=("drawbox=x=(iw-360)/2:y=70:w=360:h=62:color=0x111111@0.92:t=fill,"
       f"drawbox=x=(iw-360)/2:y=126:w=360:h=6:color={RED}@1:t=fill,"
       f"drawtext=fontfile={FONTI}:text='K.MBAPPÉ':fontsize=42:fontcolor=white:x=(w-tw)/2+14:y=81,"
       f"drawtext=fontfile={FONT}:text='10':fontsize=28:fontcolor={RED}:x=(w-360)/2+12:y=87")
SCORE=(f"drawbox=x=(iw-420)/2:y=60:w=420:h=56:color=0x0B0B14@0.9:t=fill,"
       f"drawbox=x=(iw-420)/2:y=112:w=210:h=5:color={NAVY}@1:t=fill,drawbox=x=iw/2:y=112:w=210:h=5:color={RED}@1:t=fill,"
       f"drawtext=fontfile={FONT}:text='РЕАЛ  3\\:0  ЛЕЙПЦИГ':fontsize=30:fontcolor=white:x=(w-420)/2+18:y=72,"
       f"drawtext=fontfile={FONT}:text='83’':fontsize=30:fontcolor={GOLD}:x=(w+420)/2-58:y=72")
def shake(a): return (f"scale=792:1056,crop=720:960:x='36+{a}*sin(t*71)*exp(-t*2.2)':"
                      f"y='48+{a}*cos(t*53)*exp(-t*2.2)'")
Z=lambda z,cx,cy,dz=0: f"zoompan=z='{z}+{dz}*on':d=1:x='{cx}-{cx}/zoom':y='{cy}-{cy}/zoom':s=720x960:fps=30"
MI="minterpolate=fps=30:mi_mode=mci:mc_mode=aobmc"
WF="fade=in:st=0:d=0.1:color=white"
segs=[]
def seg(b0,b1,ss,vf,speed=1.0,src=0): segs.append((b0,b1,ss,speed,vf,src))

FREEZE_T=2.55   # кадр разбора (src0): мяч после удара Мбаппе летит к ближней штанге
def prebuilt():
    run(['ffmpeg','-v','error','-y','-ss','2.2','-i',SRCS[0],'-vf',f"{GRADE}",'-frames:v','1',f'{W}/freeze.png'])
    n=fr(18)-fr(10); dur=n/FPS
    spec={'duration':dur,'elements':[
        {'type':'beam','pos':[490,540],'start':0.25,'label':'МБАППЕ'},
        {'type':'arrow','pts':[[475,507],[100,215]],'start':1.4,'curve':-0.1}]}
    json.dump(spec,open(f'{W}/tspec.json','w'),ensure_ascii=False)
    os.makedirs(f'{W}/tele',exist_ok=True)
    run(['python3',f'{R}/telestrate.py',f'{W}/freeze.png',f'{W}/tele',f'{W}/tspec.json'])
    run(['ffmpeg','-v','error','-y','-loop','1','-i',f'{W}/freeze.png','-framerate','30','-i',f'{W}/tele/t%04d.png','-filter_complex',
         f"[0]scale=720:960,setsar=1,format=rgba[f];[f][1]overlay=format=auto,"
         f"drawbox=x=24:y=60:w=150:h=50:color={GOLD}@1:t=fill,"
         f"drawtext=fontfile={FONT}:text='РАЗБОР':fontsize=32:fontcolor=black:x=38:y=68,"
         f"drawtext=fontfile={FONTI}:text='ПЕРЕВОД — И ВЫСТРЕЛ':fontsize=40:fontcolor={GOLD}:borderw=4:bordercolor=black:x=(w-tw)/2:y=820:enable='gte(t\\,2.4)',format=yuv420p",
         '-frames:v',str(n),'-r','30','-c:v','libx264','-crf','16','-preset','medium',f'{W}/tseg.mp4'])
    # СЛЕЖЕНИЕ ЗА МЯЧОМ на всей комбинации, ударе и влёте (src0), маркер золотой
    BT=['python3',f'{R}/balltrack.py','--color','FEBE10']
    run(BT+[SRCS[0],f'{W}/bt_a.mp4',f'{W}/ptsA.json','--ss','0.0','--speed','0.6','--frames',str(fr(6)-fr(0)),'--zoom','1.3','--pre',MI,'--post',GRADE])
    run(BT+[SRCS[0],f'{W}/bt_b.mp4',f'{W}/ptsA.json','--ss','1.54','--speed','0.6','--frames',str(fr(10)-fr(6)),'--zoom','1.3','--pre',MI,'--post',f'{GRADE},{PLATE}'])
    run(BT+[SRCS[0],f'{W}/bt_b2.mp4',f'{W}/ptsA.json','--ss','2.05','--speed','0.4','--frames',str(fr(20)-fr(18)),
        '--zoom','1.35','--pre',MI,'--post',GRADE,'--smooth','3'])
    run(BT+[SRCS[0],f'{W}/bt_d.mp4',f'{W}/ptsA.json','--ss','2.4','--speed','0.4','--frames',str(fr(23)-fr(20)),
        '--zoom','1.45','--pre',MI,'--post',GRADE,'--smooth','3','--marker-end','3.0'])
    # ГОЛ на дроп: мяч заходит в сетку у ближней штанги — камера за мячом крупно (1.6x, slow-mo 0.35x);
    # касание сетки ≈3.0 c источника → t≈0.43 c сегмента: только после этого флеш/шейк/ГОООЛ (текст ниже ворот)
    tn=0.43
    run(['python3',f'{R}/balltrack.py',SRCS[0],f'{W}/eseg_raw.mp4',f'{W}/ptsE.json','--ss','2.85','--speed','0.35','--frames',str(fr(25)-fr(23)),
         '--zoom','1.6','--lead','0','--smooth','2','--pre',MI,'--marker-end','2.0','--post',GRADE])
    run(['ffmpeg','-v','error','-y','-i',f'{W}/eseg_raw.mp4','-vf',
         f"{SCORE},drawbox=x=0:y=0:w=iw:h=ih:color=white@0.8:t=fill:enable='between(t,{tn},{tn+0.07})',"
         f"drawbox=x=0:y=0:w=iw:h=ih:color=white@0.35:t=fill:enable='between(t,{tn+0.07},{tn+0.17})',"
         f"{txt('ГОООЛ',150,700,FONTI,':enable=gte(t\\,'+str(tn)+')')},format=yuv420p",
         '-an','-c:v','libx264','-crf','16','-preset','medium',f'{W}/eseg.mp4'])
    # ПОВТОРЫ с каждого ракурса: комбинация целиком (1x) + удар/влёт slow-mo; камера ведёт игру (маркер только на ракурсе 1, где мяч размечен покадрово)
    BTW=['python3',f'{R}/balltrack.py','--marker-end','-1','--smooth','6','--lead','20']
    run(BTW+[SRCS[1],f'{W}/r1a.mp4',f'{W}/pts_r1.json','--ss','0.0','--frames',str(fr(38)-fr(25)),'--zoom','1.25','--post',f'{GRADE},{SCORE}'])
    run(BTW+[SRCS[1],f'{W}/r1b.mp4',f'{W}/pts_r1.json','--ss',f'{(fr(38)-fr(25))/FPS:.3f}','--speed','0.45','--frames',str(fr(43)-fr(38)),'--zoom','1.35','--pre',MI,'--post',f'{GRADE},{SCORE},{WF}'])
    run(BTW+[SRCS[2],f'{W}/r2a.mp4',f'{W}/pts_r2.json','--ss','0.0','--frames',str(fr(54)-fr(43)),'--zoom','1.25','--post',f'{GRADE},{SCORE},{WF}'])
    run(BTW+[SRCS[2],f'{W}/r2b.mp4',f'{W}/pts_r2.json','--ss',f'{(fr(54)-fr(43))/FPS:.3f}','--speed','0.45','--frames',str(fr(59)-fr(54)),'--zoom','1.35','--pre',MI,'--post',f'{GRADE},{SCORE},{WF}'])
    # КИНО-повтор (без тумана): комбинация + удар + влёт с ракурса 1, камера за мячом с маркером
    nc=fr(69)-fr(59)
    run(['python3',f'{R}/balltrack.py','--color','FEBE10',SRCS[0],f'{W}/cine_in.mp4',f'{W}/ptsA.json','--ss','0.9','--speed','0.55','--frames',str(nc),
         '--zoom','1.3','--pre',MI,'--marker-end','3.0'])
    run(['bash',f'{R}/cinematic.sh',f'{W}/cine_in.mp4',f'{W}/cine.mp4',"null",f'{nc/FPS:.3f}'])
    run(['ffmpeg','-v','error','-y','-ss','9.2','-t','1.4','-i',SRCS[3],'-an','-c:v','libx264','-crf','14',f'{W}/card_in.mp4'])
    d=(fr(82)-fr(77))/FPS
    run(['bash',f'{R}/cinematic.sh',f'{W}/card_in.mp4',f'{W}/card_raw.mp4',f"setpts=PTS/0.6,fps=30,{Z(1.2,360,420,0.002)}",str(d)])
    run(['ffmpeg','-v','error','-y','-i',f'{W}/card_raw.mp4','-vf',
         f"drawbox=x=0:y=660:w=iw:h=170:color=black@0.45:t=fill,"
         f"{txt('КИЛИАН МБАППЕ',70,682,FONTI,':alpha=min(1\\,t/0.4)')},"
         f"drawtext=fontfile={FONT}:text='83’ · 3\\:0 · РЕАЛ — РБ ЛЕЙПЦИГ':fontsize=32:fontcolor={GOLD}:borderw=3:bordercolor=black:x=(w-tw)/2:y=780:alpha='min(1,max(0,(t-0.25)/0.4))',format=yuv420p",
         '-c:v','libx264','-crf','16','-preset','medium',f'{W}/card.mp4'])

seg(0,6,0,f"FILE:{W}/bt_a.mp4")     # комбинация: розыгрыш и передачи, камера за мячом
seg(6,10,0,f"FILE:{W}/bt_b.mp4")    # передача на Мбаппе и удар, плашка
seg(10,18,0,f"FILE:{W}/tseg.mp4")   # РАЗБОР
seg(18,20,0,f"FILE:{W}/bt_b2.mp4")  # удар — slow-mo
seg(20,23,0,f"FILE:{W}/bt_d.mp4")   # влёт — slow-mo 0.4x, маркер до сетки
seg(23,25,0,f"FILE:{W}/eseg.mp4")   # ГОЛ на дроп — мяч в сетке
# ПОВТОРЫ (п.9 стандартов): вся комбинация + удар + влёт с КАЖДОГО ракурса по очереди, камера за игрой, slow-mo на ударе/влёте
seg(25,38,0,f"FILE:{W}/r1a.mp4")    # ракурс 2: дальняя трибуна — комбинация целиком
seg(38,43,0,f"FILE:{W}/r1b.mp4")    #           удар и влёт — slow-mo
seg(43,54,0,f"FILE:{W}/r2a.mp4")    # ракурс 3: трибуна сбоку — комбинация целиком
seg(54,59,0,f"FILE:{W}/r2b.mp4")    #           удар и влёт — slow-mo
seg(59,69,0,f"FILE:{W}/cine.mp4")   # КИНО-повтор (без тумана): комбинация + гол, ракурс 1
for b0,ss,z,src in ((69,7.6,1.15,3),(71,8.6,1.15,3),(73,9.6,1.1,3),(75,9.0,1.1,1)):
    seg(b0,b0+2,ss,f"{shake(14)},zoompan=z='{z}+0.003*on':d=1:x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2':s=720x960:fps=30,{WF},{SCORE}",src=src)
seg(77,82,0,f"FILE:{W}/card.mp4")

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
