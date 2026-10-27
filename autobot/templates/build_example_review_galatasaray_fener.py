# ОБЗОР: Галатасарай 2:1 Фенербахче (дерби) — дубль Осимхена + отменённый гол. 720x960, 30 fps, доли 140 BPM. Без кино-режима/дыма.
# Источники (720x960, с трибун): a1/a2 — Осимхен 29' (2 ракурса), b1/b2 — Осимхен 44' (2 ракурса), d1..d3 — отменённый гол (офсайд, 3 ракурса), fb — гол Кахведжи (съёмка у поля beIN)
import subprocess, sys, os, json
W=os.path.dirname(os.path.abspath(__file__)); R='/home/user/VKino/branding'
S=lambda n: f'{W}/src_{n}.mp4'
B=60/140; FPS=30
FONT='/usr/share/fonts/opentype/inter/InterDisplay-Black.otf'; FONTI='/usr/share/fonts/opentype/inter/InterDisplay-BlackItalic.otf'
GRADE='eq=saturation=1.05:contrast=1.08:brightness=-0.01,unsharp=5:5:0.6,vignette=PI/5'
JUV='FFFFFF'; REN='E4002B'; GOLD='0xEDBB00'
def fr(b): return round(b*B*FPS)
def run(cmd):
    r=subprocess.run(cmd,capture_output=True,text=True)
    if r.returncode: print(' '.join(cmd)[:300]); print(r.stderr[-1500:]); sys.exit(1)
def txt(t,size,y,font=FONTI,extra='',col='white'):
    return f"drawtext=fontfile={font}:text='{t}':fontsize={size}:fontcolor={col}:borderw=5:bordercolor=black@0.85:x=(w-tw)/2:y={y}{extra}"
def SCORE(a,b,m):
    mm=f"drawtext=fontfile={FONT}:text='{m}’':fontsize=27:fontcolor={GOLD}:x=(w+520)/2-58:y=74," if m else ""
    return (f"drawbox=x=(iw-520)/2:y=60:w=520:h=56:color=0x0B0B14@0.9:t=fill,"
            f"drawbox=x=(iw-520)/2:y=112:w=260:h=5:color=0xA90432@1:t=fill,drawbox=x=iw/2:y=112:w=260:h=5:color=0xFFED00@1:t=fill,"
            f"{mm}drawtext=fontfile={FONT}:text='ГАЛАТАСАРАЙ {a}\\:{b} ФЕНЕРБАХЧЕ':fontsize=27:fontcolor=white:x=(w-520)/2+16:y=74")
def PLATE(t):   # плашка гола внизу (не на мяче/воротах)
    return (f"drawbox=x=0:y=820:w=iw:h=70:color=black@0.6:t=fill,"
            f"drawtext=fontfile={FONTI}:text='{t}':fontsize=40:fontcolor={GOLD}:borderw=3:bordercolor=black:x=(w-tw)/2:y=834")
def shake(a): return f"scale=792:1056,crop=720:960:x='36+{a}*sin(t*71)*exp(-t*2.2)':y='48+{a}*cos(t*53)*exp(-t*2.2)'"
Z=lambda z,cx,cy,dz=0: f"zoompan=z='{z}+{dz}*on':d=1:x='{cx}-{cx}/zoom':y='{cy}-{cy}/zoom':s=720x960:fps=30"
MI="minterpolate=fps=30:mi_mode=mci:mc_mode=aobmc"; WF="fade=in:st=0:d=0.1:color=white"
parts=[]
def plain(name,src,ss,b0,b1,vf,speed=1.0):
    out=f'{W}/{name}.mp4'; n=fr(b1)-fr(b0); pre=f"setpts=(PTS-STARTPTS)/{speed}," if speed!=1 else ""
    run(['ffmpeg','-v','error','-y','-ss',f'{ss:.3f}','-t',str(n/FPS*speed+0.3),'-i',S(src),'-vf',f"{pre}fps=30,{vf},{GRADE},format=yuv420p",
         '-an','-frames:v',str(n),'-r','30','-c:v','libx264','-crf','16','-preset','medium',out]); parts.append(out)
GOOL=lambda lab: f"{shake(18)},{txt('ГОООЛ',150,230)},drawbox=x=0:y=400:w=iw:h=80:color=black@0.6:t=fill,{txt(lab,52,412,col='0xEDBB00')},{WF}"
NOGOAL=lambda: f"drawbox=x=0:y=380:w=iw:h=120:color=0xC8102E@0.85:t=fill,{txt('ГОЛ ОТМЕНЁН · ОФСАЙД',56,412)}"
# ИНТРО
plain('i0','b1',20.0,0,4,f"{Z(1.1,360,480,0.001)},drawbox=x=0:y=300:w=iw:h=300:color=black@0.6:t=fill,"
      f"{txt('ГАЛАТАСАРАЙ — ФЕНЕРБАХЧЕ',34,330)},{txt('ДУБЛЬ ОСИМХЕНА',62,410,col='0xEDBB00')},{txt('+ ОТМЕНЁННЫЙ ГОЛ · ДЕРБИ',32,525,FONT)}")
# 1) 29' ОСИМХЕН 1:0 — ракурс 1 целиком, замедленно, ГОООЛ, ракурс 2 целиком + замедленно
P1=PLATE("29’ ОСИМХЕН · 1\\:0")
plain('g1a','a1',0.0,4,10,f"{Z(1.05,360,400,0.0008)},{SCORE(0,0,29)},{P1}",speed=1.0)            # 0–2.6
plain('g1b','a1',0.9,10,16,f"{Z(1.5,470,300,0.003)},{MI},{SCORE(0,0,29)},{P1},{WF}",speed=0.4)
plain('g1c','a1',2.0,16,19,f"{Z(1.3,470,300,0.003)},{SCORE(1,0,29)},{GOOL('1\\:0 · ГОЛ ГАЛАТАСАРАЯ')}",speed=0.5)
plain('g1d','a2',0.0,19,26,f"{Z(1.05,360,480,0.0008)},{SCORE(1,0,29)},{PLATE('29’ ОСИМХЕН · 2-й ракурс')},{WF}",speed=1.0)   # 0–3.0
plain('g1e','a2',1.6,26,32,f"{Z(1.5,300,700,0.003)},{MI},{SCORE(1,0,29)},{PLATE('29’ ОСИМХЕН · замедленно')},{WF}",speed=0.4)
# 2) 44' ОСИМХЕН 2:0
P2=PLATE("44’ ОСИМХЕН · 2\\:0")
plain('g2a','b1',0.5,32,43,f"{Z(1.05,400,350,0.0008)},{SCORE(1,0,44)},{P2},{WF}",speed=1.0)          # 0.5–5.2
plain('g2b','b1',3.6,43,49,f"{Z(1.5,560,250,0.003)},{MI},{SCORE(1,0,44)},{P2},{WF}",speed=0.4)
plain('g2c','b1',4.4,49,52,f"{Z(1.3,560,250,0.003)},{SCORE(2,0,44)},{GOOL('2\\:0 · ГОЛ ГАЛАТАСАРАЯ')}",speed=0.5)
plain('g2d','b2',3.4,52,62,f"{Z(1.1,300,200,0.0008)},{SCORE(2,0,44)},{PLATE('44’ ОСИМХЕН · 2-й ракурс')},{WF}",speed=1.0)  # 3.4–7.7
# 3) ОТМЕНЁННЫЙ ГОЛ — 3 ракурса целиком, табло без минуты (минута неизвестна)
P3=PLATE("ГОЛ ОСИМХЕНА · ОТМЕНЁН")
plain('d1','d1',2.5,62,72,f"{Z(1.1,200,350,0.0008)},{P3},{WF}",speed=1.0)                       # 2.5–6.8
plain('d2','d2',2.8,72,82,f"{Z(1.1,500,300,0.0008)},{P3},{WF}",speed=1.0)                        # 2.8–7.1
plain('d3','d3',0.0,82,88,f"{Z(1.15,200,400,0.0008)},{P3},{WF}",speed=1.0)                        # 0–2.6
plain('dn','d1',5.4,88,94,f"{Z(1.4,150,350,0.003)},{MI},{NOGOAL()},{WF}",speed=0.4)
# 4) ФЕНЕРБАХЧЕ: ИРФАН КАН КАХВЕДЖИ, удар через себя → 2:1 (съёмка у поля, минута неизвестна)
P4=PLATE("КАХВЕДЖИ · 2\\:1 · ЧЕРЕЗ СЕБЯ")
plain('f1','fb',0.3,94,106,f"{Z(1.05,360,480,0.0008)},{SCORE(2,0,'')},{P4},{WF}",speed=1.0)     # 0.3–5.4
plain('f2','fb',3.3,106,115,f"{Z(1.35,420,420,0.002)},{MI},{SCORE(2,0,'')},{PLATE('КАХВЕДЖИ · ЗАМЕДЛЕННО')},{WF}",speed=0.4)
plain('f3','fb',4.7,115,118,f"{Z(1.3,400,420,0.003)},{SCORE(2,1,'')},{GOOL('2\\:1 · ГОЛ ФЕНЕРБАХЧЕ')}",speed=0.5)
plain('fin','b1',22.0,118,124,f"{Z(1.05,360,480,0.001)},drawbox=x=0:y=330:w=iw:h=250:color=black@0.6:t=fill,"
      f"{txt('ГАЛАТАСАРАЙ — ФЕНЕРБАХЧЕ',34,360)},{txt('ИТОГ 2\\:1',110,425,col='0xEDBB00')}",speed=0.8)
with open(f'{W}/list.txt','w') as f: f.writelines(f"file '{p}'\n" for p in parts)
run(['ffmpeg','-v','error','-y','-f','concat','-safe','0','-i',f'{W}/list.txt','-c:v','libx264','-crf','16','-preset','medium','-pix_fmt','yuv420p','-r','30',f'{W}/edit_v.mp4'])
print('done', fr(124)/FPS)
