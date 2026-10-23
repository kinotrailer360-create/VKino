# ОБЗОР: Бавария 2:2 Арсенал (ЛЧ, 3-й тур) — все голы. 720x960, 30 fps, доли 140 BPM. Без кино-режима/дыма.
# Источники: src8 — гол Эзе (трибуна напротив), src1 — гол Дэвиса (трибуна), src4 — пенальти Кейна (у линии, затем за воротами),
#            src7 — атака перед голом Райса, src5 — гол Райса из-за ворот + празднование №41
import subprocess, sys, os, json
W=os.path.dirname(os.path.abspath(__file__)); R='/home/user/VKino/branding'
S=lambda n: f'{W}/src{n}.mp4'
B=60/140; FPS=30
FONT='/usr/share/fonts/opentype/inter/InterDisplay-Black.otf'; FONTI='/usr/share/fonts/opentype/inter/InterDisplay-BlackItalic.otf'
GRADE='eq=saturation=1.05:contrast=1.08:brightness=-0.01,unsharp=5:5:0.6,vignette=PI/5'
BAY='DC052D'; ARS='EF0107'; GOLD='0xEDBB00'
def fr(b): return round(b*B*FPS)
def run(cmd):
    r=subprocess.run(cmd,capture_output=True,text=True)
    if r.returncode: print(' '.join(cmd)[:300]); print(r.stderr[-1500:]); sys.exit(1)
def txt(t,size,y,font=FONTI,extra='',col='white'):
    return f"drawtext=fontfile={font}:text='{t}':fontsize={size}:fontcolor={col}:borderw=5:bordercolor=black@0.85:x=(w-tw)/2:y={y}{extra}"
def SCORE(a,b,m):
    return (f"drawbox=x=(iw-460)/2:y=60:w=460:h=56:color=0x0B0B14@0.9:t=fill,"
            f"drawbox=x=(iw-460)/2:y=112:w=230:h=5:color=0x{BAY}@1:t=fill,drawbox=x=iw/2:y=112:w=230:h=5:color=0x{ARS}@1:t=fill,"
            f"drawtext=fontfile={FONT}:text='БАВАРИЯ  {a}\\:{b}  АРСЕНАЛ':fontsize=29:fontcolor=white:x=(w-460)/2+18:y=73,"
            f"drawtext=fontfile={FONT}:text='{m}’':fontsize=29:fontcolor={GOLD}:x=(w+460)/2-60:y=73")
def PLATE(t):   # плашка гола внизу (не на мяче/воротах)
    return (f"drawbox=x=0:y=820:w=iw:h=70:color=black@0.6:t=fill,"
            f"drawtext=fontfile={FONTI}:text='{t}':fontsize=40:fontcolor={GOLD}:borderw=3:bordercolor=black:x=(w-tw)/2:y=834")
def shake(a): return f"scale=792:1056,crop=720:960:x='36+{a}*sin(t*71)*exp(-t*2.2)':y='48+{a}*cos(t*53)*exp(-t*2.2)'"
Z=lambda z,cx,cy,dz=0: f"zoompan=z='{z}+{dz}*on':d=1:x='{cx}-{cx}/zoom':y='{cy}-{cy}/zoom':s=720x960:fps=30"
MI="minterpolate=fps=30:mi_mode=mci:mc_mode=aobmc"; WF="fade=in:st=0:d=0.1:color=white"
parts=[]
def bt(name,src,pts,ss,b0,b1,speed=1.0,zoom=1.3,col=ARS,ms=None,me=-1,post='',mi=False):
    out=f'{W}/{name}.mp4'; cmd=['python3',f'{R}/balltrack.py',S(src),out,f'{W}/p_{pts}.json','--ss',str(ss),'--speed',str(speed),
        '--frames',str(fr(b1)-fr(b0)),'--zoom',str(zoom),'--color',col,'--marker-end',str(me),'--smooth','5','--post',GRADE+(','+post if post else '')]
    if ms is not None: cmd+=['--marker-start',str(ms)]
    if mi: cmd+=['--pre',MI]
    run(cmd); parts.append(out)
def plain(name,src,ss,b0,b1,vf,speed=1.0):
    out=f'{W}/{name}.mp4'; n=fr(b1)-fr(b0); pre=f"setpts=(PTS-STARTPTS)/{speed}," if speed!=1 else ""
    run(['ffmpeg','-v','error','-y','-ss',f'{ss:.3f}','-t',str(n/FPS*speed+0.3),'-i',S(src),'-vf',f"{pre}fps=30,{vf},{GRADE},format=yuv420p",
         '-an','-frames:v',str(n),'-r','30','-c:v','libx264','-crf','16','-preset','medium',out]); parts.append(out)
GOOL=lambda lab: f"{shake(18)},{txt('ГОООЛ',150,230)},drawbox=x=0:y=400:w=iw:h=80:color=black@0.6:t=fill,{txt(lab,52,412,col='0xEDBB00')},{WF}"
# 0) ИНТРО
plain('i0',4,6.0,0,4,f"{Z(1.05,360,480,0.001)},drawbox=x=0:y=300:w=iw:h=300:color=black@0.55:t=fill,"
      f"{txt('БАВАРИЯ 2\\:2 АРСЕНАЛ',50,335)},{txt('ВСЕ ГОЛЫ',96,420,col='0xEDBB00')},{txt('ЛИГА ЧЕМПИОНОВ · 3-Й ТУР',30,535,FONT)}")
# 1) 25' ЭЗЕ 0:1 (пас Эдегор) — мяч на дальнем ракурсе не читается: камера ведёт атаку, без маркера
P1=PLATE("25’ ЭЗЕ · 0\\:1 · пас Эдегор"); SC1=SCORE(0,0,25); SC1b=SCORE(0,1,25)
bt('g1a',8,'eze',0.0,4,10,speed=0.6,zoom=1.35,post=f"{SC1},{P1}")
bt('g1b',8,'eze',0.36,10,14,speed=0.3,zoom=1.9,mi=True,post=f"{SC1},{P1}")
plain('g1c',8,0.95,14,17,f"{Z(1.9,650,505,0.003)},{SC1b},{GOOL('0\\:1 · ГОЛ АРСЕНАЛА')}",speed=0.5)
# 2) 35' ДЭВИС 1:1 (пас Кейн): обводка вратаря, маркер на мяче 0.8–1.45
P2=PLATE("35’ ДЭВИС · 1\\:1 · пас Кейн"); SC2=SCORE(0,1,35); SC2b=SCORE(1,1,35)
bt('g2a',1,'dav',0.0,17,27,speed=0.9,zoom=1.3,col=BAY,ms=0.8,me=1.45,post=f"{SC2},{P2},{WF}")
bt('g2b',1,'dav',0.7,27,31,speed=0.4,zoom=1.45,col=BAY,ms=0.8,me=1.45,mi=True,post=f"{SC2},{P2}")
plain('g2c',1,2.9,31,34,f"{Z(1.2,200,420,0.003)},{SC2b},{GOOL('1\\:1 · ГОЛ БАВАРИИ')}",speed=0.6)
# 3) 61' КЕЙН, ПЕНАЛЬТИ 2:1: разбег и удар, маркер на мяче до ворот
P3=PLATE("61’ КЕЙН · 2\\:1 · пенальти"); SC3=SCORE(1,1,61); SC3b=SCORE(2,1,61)
bt('g3a',4,'kane',4.4,34,41,speed=0.8,zoom=1.3,col=BAY,ms=6.5,me=6.85,post=f"{SC3},{P3},{WF}")
bt('g3b',4,'kane',6.45,41,45,speed=0.35,zoom=1.5,col=BAY,ms=6.5,me=6.85,mi=True,post=f"{SC3},{P3}")
plain('g3c',4,8.2,45,48,f"{Z(1.15,360,480,0.002)},{SC3b},{GOOL('2\\:1 · ГОЛ БАВАРИИ')}",speed=0.8)
plain('g3d',4,9.0,48,52,f"{Z(1.2,360,520,0.002)},{SC3b},{WF}",speed=0.8)   # мяч в сетке после пенальти, ближе
# 4) 82' РАЙС 2:2 (пас Дьокереш): атака, затем влёт из-за ворот с маркером до сетки
P4=PLATE("82’ РАЙС · 2\\:2 · пас Дьокереш"); SC4=SCORE(2,1,82); SC4b=SCORE(2,2,82)
bt('g4a',7,'rice7',4.0,52,61,speed=1.0,zoom=1.25,post=f"{SC4},{P4},{WF}")
bt('g4b',5,'rice5',1.15,61,66,speed=0.4,zoom=1.35,ms=1.35,me=2.15,mi=True,post=f"{SC4},{P4},{WF}")
plain('g4c',5,2.15,66,69,f"{Z(1.6,450,590,0.003)},{SC4b},{GOOL('2\\:2 · ГОЛ АРСЕНАЛА')}",speed=0.5)
plain('g4d',5,10.4,69,73,f"{Z(1.15,360,520,0.002)},{SC4b},{WF}",speed=0.8)     # празднование №41
# ФИНАЛ
plain('fin',5,12.0,73,79,f"{Z(1.1,360,480,0.002)},drawbox=x=0:y=330:w=iw:h=250:color=black@0.55:t=fill,"
      f"{txt('БАВАРИЯ 2\\:2 АРСЕНАЛ',50,360)},{txt('2\\:2',130,430,col='0xEDBB00')}",speed=0.8)
with open(f'{W}/list.txt','w') as f: f.writelines(f"file '{p}'\n" for p in parts)
run(['ffmpeg','-v','error','-y','-f','concat','-safe','0','-i',f'{W}/list.txt','-c:v','libx264','-crf','16','-preset','medium','-pix_fmt','yuv420p','-r','30',f'{W}/edit_v.mp4'])
print('done', fr(79)/FPS)
