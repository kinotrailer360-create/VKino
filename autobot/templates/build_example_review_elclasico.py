# ОБЗОР: Барселона 1:2 Реал (Ла Лига, Эль Класико, Камп Ноу, 26.10.2026). 720x960, 30 fps, доли 140 BPM. Без кино-режима/дыма.
# Источники (720x960, с трибун): src_m1a/src_m1b — Мбаппе 41' (2 ракурса), src_m2 — Мбаппе 87' (+ празднование)
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
    mm=f"drawtext=fontfile={FONT}:text='{m}’':fontsize=29:fontcolor={GOLD}:x=(w+460)/2-60:y=73," if m else ""
    return (f"drawbox=x=(iw-460)/2:y=60:w=460:h=56:color=0x0B0B14@0.9:t=fill,"
            f"drawbox=x=(iw-460)/2:y=112:w=230:h=5:color=0xA50044@1:t=fill,drawbox=x=iw/2:y=112:w=230:h=5:color=0xFEBE10@1:t=fill,"
            f"{mm}drawtext=fontfile={FONT}:text='БАРСЕЛОНА  {a}\\:{b}  РЕАЛ':fontsize=29:fontcolor=white:x=(w-460)/2+18:y=73")
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
# ИНТРО
plain('i0','m2',9.0,0,4,f"{Z(1.1,360,480,0.001)},drawbox=x=0:y=300:w=iw:h=300:color=black@0.6:t=fill,"
      f"{txt('БАРСЕЛОНА — РЕАЛ',54,325)},{txt('ЭЛЬ КЛАСИКО',86,405,col='0xEDBB00')},{txt('ВСЕ ГОЛЫ · КАМП НОУ',32,525,FONT)}")
# 1) 41' МБАППЕ 0:1 — ракурс 1 (атака целиком → мяч в сетке), замедленно, ГОООЛ, ракурс 2 целиком
P1=PLATE("41’ МБАППЕ · 0\\:1")
plain('g1a','m1a',2.3,4,16,f"{Z(1.1,300,450,0.0008)},{SCORE(0,0,41)},{P1}",speed=1.0)                 # 2.3–7.4
plain('g1b','m1a',6.6,16,23,f"{Z(1.7,170,420,0.003)},{MI},{SCORE(0,0,41)},{P1},{WF}",speed=0.4)      # удар→сетка 0.4x
plain('g1c','m1a',7.6,23,26,f"{Z(1.5,170,420,0.003)},{SCORE(0,1,41)},{GOOL('0\\:1 · ГОЛ РЕАЛА')}",speed=0.5)
plain('g1d','m1b',0.0,26,38,f"{Z(1.1,330,420,0.0008)},{SCORE(0,1,41)},{PLATE('41’ МБАППЕ · 2-й ракурс')},{WF}",speed=1.0)   # 0–5.1
plain('g1e','m1b',3.9,38,44,f"{Z(1.6,250,200,0.003)},{MI},{SCORE(0,1,41)},{PLATE('41’ МБАППЕ · 2-й ракурс · замедленно')},{WF}",speed=0.4)
# 2) 61' РАФИНЬЯ 1:1 (пас Педри) — видео гола нет → плашка
plain('t1','m1b',9.0,44,50,f"boxblur=12:2,eq=brightness=-0.15,drawbox=x=0:y=370:w=iw:h=140:color=black@0.65:t=fill,"
      f"{txt('61’ РАФИНЬЯ · 1\\:1',52,385)},{txt('пас Педри',34,452,FONT,col='0xEDBB00')},{SCORE(1,1,61)},{WF}",speed=1.0)
# 3) 87' МБАППЕ 1:2 (пас Камавинга) — атака целиком, замедленно, ГОООЛ, празднование
P3=PLATE("87’ МБАППЕ · 1\\:2 · пас Камавинга")
plain('g3a','m2',0.6,50,57,f"{Z(1.1,360,450,0.0008)},{SCORE(1,1,87)},{P3},{WF}",speed=1.0)            # 0.6–3.6
plain('g3b','m2',2.6,57,64,f"{Z(1.6,250,450,0.003)},{MI},{SCORE(1,1,87)},{P3},{WF}",speed=0.4)
plain('g3c','m2',3.6,64,67,f"{Z(1.4,250,450,0.003)},{SCORE(1,2,87)},{GOOL('1\\:2 · ГОЛ РЕАЛА')}",speed=0.5)
plain('g3d','m2',7.0,67,74,f"{Z(1.1,360,450,0.002)},{SCORE(1,2,87)},{WF}",speed=0.8)
# ФИНАЛ
plain('fin','m2',14.0,74,80,f"{Z(1.05,360,480,0.001)},drawbox=x=0:y=330:w=iw:h=250:color=black@0.6:t=fill,"
      f"{txt('БАРСЕЛОНА — РЕАЛ',54,355)},{txt('ИТОГ 1\\:2',110,425,col='0xEDBB00')}",speed=0.8)
with open(f'{W}/list.txt','w') as f: f.writelines(f"file '{p}'\n" for p in parts)
run(['ffmpeg','-v','error','-y','-f','concat','-safe','0','-i',f'{W}/list.txt','-c:v','libx264','-crf','16','-preset','medium','-pix_fmt','yuv420p','-r','30',f'{W}/edit_v.mp4'])
print('done', fr(80)/FPS)
