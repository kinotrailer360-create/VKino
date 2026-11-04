# ГОЛ: Батраков 12' — Галатасарай 1:0 Штутгарт (ЛЧ, по табло стадиона). 720x960, 30 fps, доли 140 BPM. Без кино-режима/дыма.
# Источники (720x960, с трибун): a — сбоку (вся атака + удар), b — из-за линии ворот, c — дальний общий, d — празднование
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
            f"drawbox=x=(iw-520)/2:y=112:w=260:h=5:color=0xA90432@1:t=fill,drawbox=x=iw/2:y=112:w=260:h=5:color=0xE32219@1:t=fill,"
            f"{mm}drawtext=fontfile={FONT}:text='ГАЛАТАСАРАЙ {a}\\:{b} ШТУТГАРТ':fontsize=27:fontcolor=white:x=(w-520)/2+16:y=74")
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
P=PLATE("12’ БАТРАКОВ · 1\\:0"); S0=SCORE(0,0,12); S1=SCORE(1,0,12)
# 1) вся атака + удар (ракурс a), замедленно, ГОООЛ
plain('a1','a',0.0,0,9,f"{Z(1.05,360,480,0.0008)},{S0},{P}",speed=1.0)                         # 0–3.9
plain('a2','a',2.6,9,16,f"{Z(1.5,330,300,0.003)},{MI},{S0},{PLATE('12’ БАТРАКОВ · ЗАМЕДЛЕННО')},{WF}",speed=0.4)
plain('a3','a',3.5,16,19,f"{Z(1.3,330,300,0.003)},{S1},{GOOL('1\\:0 · ГОЛ БАТРАКОВА')}",speed=0.5)
plain('d1','d',8.3,19,25,f"{Z(1.1,360,480,0.002)},{S1},{WF}",speed=0.9)                       # празднование, сальто
# 2) повторы: из-за линии ворот, общий план
plain('b1','b',0.0,25,33,f"{Z(1.05,360,480,0.0008)},{S1},{PLATE('12’ БАТРАКОВ · 2-й ракурс')},{WF}",speed=1.0)   # 0–3.4
plain('b2','b',2.1,33,39,f"{Z(1.5,150,220,0.003)},{MI},{S1},{PLATE('2-й ракурс · замедленно')},{WF}",speed=0.4)
plain('c1','c',13.0,39,47,f"{Z(1.3,300,330,0.0008)},{S1},{PLATE('12’ БАТРАКОВ · 3-й ракурс')},{WF}",speed=1.0)   # 13–16.4
plain('sb','c',94.6,47,51,f"{Z(1.25,360,330,0.002)},{WF}",speed=1.0)                          # табло стадиона: BATRAKOV 12'
plain('fin','d',11.0,51,56,f"{Z(1.05,360,480,0.001)},drawbox=x=0:y=330:w=iw:h=250:color=black@0.6:t=fill,"
      f"{txt('АЛЕКСЕЙ БАТРАКОВ',52,355)},{txt('ГОЛ В ЛЧ',100,425,col='0xEDBB00')}",speed=0.8)
with open(f'{W}/list.txt','w') as f: f.writelines(f"file '{p}'\n" for p in parts)
run(['ffmpeg','-v','error','-y','-f','concat','-safe','0','-i',f'{W}/list.txt','-c:v','libx264','-crf','16','-preset','medium','-pix_fmt','yuv420p','-r','30',f'{W}/edit_v.mp4'])
print('done', fr(56)/FPS)
