# ОБЗОР: Ювентус 2:2 Ренн (ЛЕ, 3-й тур) — голы Юве + отменённый гол. 720x960, 30 fps, доли 140 BPM. Без кино-режима/дыма.
# Источники (720x960, с трибун): src_celik/src_celik2 — Челик 5', src_pen/src_pen2 — пенальти Вольтемаде 67',
#            src_dis1..3 — отменённый гол 90' (3 ракурса), src_dis2 ~90 с — арбитр у монитора VAR
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
    mm=f"drawtext=fontfile={FONT}:text='{m}’':fontsize=29:fontcolor={GOLD}:x=(w+440)/2-60:y=73," if m else ""
    return (f"drawbox=x=(iw-440)/2:y=60:w=440:h=56:color=0x0B0B14@0.9:t=fill,"
            f"drawbox=x=(iw-440)/2:y=112:w=220:h=5:color=0xFFFFFF@1:t=fill,drawbox=x=iw/2:y=112:w=220:h=5:color=0x{REN}@1:t=fill,"
            f"{mm}drawtext=fontfile={FONT}:text='ЮВЕНТУС  {a}\\:{b}  РЕНН':fontsize=29:fontcolor=white:x=(w-440)/2+18:y=73")
def PLATE(t):   # плашка гола внизу (не на мяче/воротах)
    return (f"drawbox=x=0:y=820:w=iw:h=70:color=black@0.6:t=fill,"
            f"drawtext=fontfile={FONTI}:text='{t}':fontsize=40:fontcolor={GOLD}:borderw=3:bordercolor=black:x=(w-tw)/2:y=834")
def shake(a): return f"scale=792:1056,crop=720:960:x='36+{a}*sin(t*71)*exp(-t*2.2)':y='48+{a}*cos(t*53)*exp(-t*2.2)'"
Z=lambda z,cx,cy,dz=0: f"zoompan=z='{z}+{dz}*on':d=1:x='{cx}-{cx}/zoom':y='{cy}-{cy}/zoom':s=720x960:fps=30"
MI="minterpolate=fps=30:mi_mode=mci:mc_mode=aobmc"; WF="fade=in:st=0:d=0.1:color=white"
parts=[]
def bt(name,src,pts,ss,b0,b1,speed=1.0,zoom=1.3,col='FFFFFF',ms=None,me=-1,post='',mi=False):
    out=f'{W}/{name}.mp4'; cmd=['python3',f'{R}/balltrack.py',S(src),out,f'{W}/p_{pts}.json','--ss',str(ss),'--speed',str(speed),
        '--frames',str(fr(b1)-fr(b0)),'--zoom',str(zoom),'--color',col,'--marker-end',str(me),'--smooth','6','--post',GRADE+(','+post if post else '')]
    if ms is not None: cmd+=['--marker-start',str(ms)]
    if mi: cmd+=['--pre',MI]
    run(cmd); parts.append(out)
def plain(name,src,ss,b0,b1,vf,speed=1.0):
    out=f'{W}/{name}.mp4'; n=fr(b1)-fr(b0); pre=f"setpts=(PTS-STARTPTS)/{speed}," if speed!=1 else ""
    run(['ffmpeg','-v','error','-y','-ss',f'{ss:.3f}','-t',str(n/FPS*speed+0.3),'-i',S(src),'-vf',f"{pre}fps=30,{vf},{GRADE},format=yuv420p",
         '-an','-frames:v',str(n),'-r','30','-c:v','libx264','-crf','16','-preset','medium',out]); parts.append(out)
GOOL=lambda lab: f"{shake(18)},{txt('ГОООЛ',150,230)},drawbox=x=0:y=400:w=iw:h=80:color=black@0.6:t=fill,{txt(lab,52,412,col='0xEDBB00')},{WF}"
NOGOAL=lambda: f"drawbox=x=0:y=380:w=iw:h=120:color=0xC8102E@0.85:t=fill,{txt('ГОЛ ОТМЕНЁН · VAR',62,410)}"
# ИНТРО
plain('i0','pen',0.4,0,4,f"{Z(1.05,360,480,0.001)},drawbox=x=0:y=300:w=iw:h=300:color=black@0.6:t=fill,"
      f"{txt('ЮВЕНТУС — РЕНН',58,325)},{txt('ГОЛЫ ЮВЕ',90,405,col='0xEDBB00')},{txt('+ ОТМЕНЁННЫЙ ГОЛ · ЛИГА ЕВРОПЫ',30,525,FONT)}")
# 1) 5' ЧЕЛИК 1:0 — дальний ракурс: атака, крупно у ворот, ГОООЛ; празднование со 2-го ракурса
P1=PLATE("5’ ЧЕЛИК · 1\\:0"); 
bt('g1a','celik','celik',0.0,4,12,speed=1.0,zoom=1.3,post=f"{SCORE(0,0,5)},{P1}")
plain('g1b','celik',1.2,12,17,f"{Z(1.7,250,690,0.002)},{MI},{SCORE(0,0,5)},{P1}",speed=0.4)
plain('g1c','celik',2.05,17,20,f"{Z(1.8,250,690,0.003)},{SCORE(1,0,5)},{GOOL('1\\:0 · ГОЛ ЮВЕНТУСА')}",speed=0.5)
plain('g1d','celik2',0.0,20,24,f"{Z(1.1,360,480,0.002)},{SCORE(1,0,5)},{WF}",speed=0.7)
# Ренн: 15' Аль-Тамари 1:1 и 43' Диа (пен.) 1:2 — в видео пользователя не было (только голы Юве) — переходная плашка
plain('t1','celik',8.0,24,29,f"boxblur=12:2,eq=brightness=-0.15,drawbox=x=0:y=360:w=iw:h=170:color=black@0.65:t=fill,"
      f"{txt('15’ АЛЬ-ТАМАРИ · 1\\:1',44,375,col='white')},{txt('43’ ДИА (пен.) · 1\\:2',44,440,col='white')},{SCORE(1,2,'')},{WF}",speed=1.0)
# 2) 67' ВОЛЬТЕМАДЕ, ПЕНАЛЬТИ 2:2 — из-за ворот (маркер на мяче), затем 2-й ракурс с трибуны
P2=PLATE("67’ ВОЛЬТЕМАДЕ · 2\\:2 · пенальти")
bt('g2a','pen','pen',2.0,29,36,speed=0.8,zoom=1.25,ms=3.4,me=4.55,post=f"{SCORE(1,2,67)},{P2},{WF}")
bt('g2b','pen','pen',3.85,36,40,speed=0.35,zoom=1.35,ms=3.4,me=4.55,mi=True,post=f"{SCORE(1,2,67)},{P2}")
plain('g2c','pen',4.5,40,43,f"{Z(1.3,330,800,0.003)},{SCORE(2,2,67)},{GOOL('2\\:2 · ГОЛ ЮВЕНТУСА')}",speed=0.6)
plain('g2d','pen2',10.9,43,49,f"{Z(1.15,380,480,0.002)},{MI},{SCORE(2,2,67)},{PLATE('67’ ВОЛЬТЕМАДЕ · 2-й ракурс')},{WF}",speed=0.5)
# 3) 90' ОТМЕНЁННЫЙ ГОЛ — каждый ракурс ЦЕЛИКОМ: розыгрыш → удар → мяч в сетке (1x), затем slow-mo добивания, затем VAR
P3=PLATE("90’ ГОЛ ЮВЕНТУСА · ОТМЕНЁН VAR"); S3=SCORE(2,2,90)
plain('d1','dis1',1.7,49,61,f"{Z(1.15,470,520,0.0008)},{S3},{P3},{WF}",speed=1.0)            # ракурс 1: у штрафной, 1.7–6.9 c
plain('d2','dis2',4.4,61,69,f"{Z(1.12,470,480,0.0008)},{S3},{P3},{WF}",speed=1.0)            # ракурс 2: сбоку, 4.4–7.9 c
plain('d3','dis3',6.0,69,80,f"{Z(1.15,340,600,0.0008)},{S3},{P3},{WF}",speed=1.0)            # ракурс 3: высоко, 6.0–10.7 c
plain('d4','dis1',5.4,80,87,f"{Z(1.55,560,520,0.003)},{MI},{S3},{PLATE('90’ ОТМЕНЁННЫЙ ГОЛ · ЗАМЕДЛЕННО')},{WF}",speed=0.4)  # удар→сетка 0.4x
plain('var','dis2',89.0,87,92,f"{Z(1.1,360,480,0.002)},{S3},{NOGOAL()},{WF}",speed=1.0)
# ФИНАЛ — без кадров пенальти: игроки у арбитра после отмены
plain('fin','dis2',104.0,92,98,f"{Z(1.05,360,480,0.001)},drawbox=x=0:y=330:w=iw:h=250:color=black@0.6:t=fill,"
      f"{txt('ЮВЕНТУС — РЕНН',54,355)},{txt('ИТОГ 2\\:2',110,425,col='0xEDBB00')}",speed=0.8)
with open(f'{W}/list.txt','w') as f: f.writelines(f"file '{p}'\n" for p in parts)
run(['ffmpeg','-v','error','-y','-f','concat','-safe','0','-i',f'{W}/list.txt','-c:v','libx264','-crf','16','-preset','medium','-pix_fmt','yuv420p','-r','30',f'{W}/edit_v.mp4'])
print('done', fr(98)/FPS)
