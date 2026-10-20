# Монтаж: 720x960, 30 fps, склейки по долям трека 140 BPM
import subprocess, sys, os
SRCS=sys.argv[3].split(','); SRC=SRCS[0]; OUT=sys.argv[2]; W=os.path.dirname(os.path.abspath(__file__))
B=60/140; FPS=30
FONT='/usr/share/fonts/opentype/inter/InterDisplay-Black.otf'
FONTI='/usr/share/fonts/opentype/inter/InterDisplay-BlackItalic.otf'
GRADE='eq=saturation=1.35:contrast=1.12:brightness=0.01,unsharp=5:5:0.6,vignette=PI/5'
def fr(beat): return round(beat*B*FPS)
def txt(t,size,y,font=FONTI,extra=''):
    return (f"drawtext=fontfile={font}:text='{t}':fontsize={size}:fontcolor=white:"
            f"borderw=5:bordercolor=black@0.85:x=(w-tw)/2:y={y}{extra}")
PLATE=("drawbox=x=(iw-320)/2:y=70:w=320:h=62:color=0x004D98@0.92:t=fill,"
       "drawbox=x=(iw-320)/2:y=126:w=320:h=6:color=0xA50044@1:t=fill,"
       f"drawtext=fontfile={FONTI}:text='L.YAMAL':fontsize=44:fontcolor=white:x=(w-tw)/2+10:y=80,"
       f"drawtext=fontfile={FONT}:text='10':fontsize=26:fontcolor=0xEDBB00:x=(w-320)/2+12:y=88")
def crop(y,h=960): return f"crop=720:{h}:0:{y},scale=720:960:flags=lanczos,setsar=1"
def shake(a): return (f"scale=792:1056,crop=720:960:x='36+{a}*sin(t*71)*exp(-t*2.2)':"
                      f"y='48+{a}*cos(t*53)*exp(-t*2.2)'")
segs=[]
def seg(b0,b1,ss,vf,speed=1.0,still=False,src=0):
    segs.append((b0,b1,ss,speed,vf,still,src))
Z=lambda z,cx,cy,dz=0: f"zoompan=z='{z}+{dz}*on':d=1:x='{cx}-{cx}/zoom':y='{cy}-{cy}/zoom':s=720x960:fps=30"
MI="minterpolate=fps=30:mi_mode=mci:mc_mode=aobmc"
# источники: 0 aarav (верх сбоку), 1 уровень поля, 2 из-за ворот, 3 широкий сбоку (letterbox), 4 угловой
def CAM(t,y=60): return (f"drawbox=x=40:y={y}:w={len(t)*21+30}:h=50:color=black@0.6:t=fill,"
                    f"drawtext=fontfile={FONT}:text='{t}':fontsize=32:fontcolor=white:x=55:y={y+8}")
C3="crop=720:955:0:170,scale=720:960:flags=lanczos,setsar=1"
SL=lambda c: f"{c},hue=s=0.85"
# источники: 0 A верх сбоку (576x1024), 1 B бровка (576x768), 2 C дальний конец (576x768), 3 D нижний ярус (720x1280)
CA="crop=576:768:0:200,scale=720:960:flags=lanczos,setsar=1"
CB="scale=720:960:flags=lanczos,setsar=1"
CBd="delogo=x=190:y=595:w=300:h=75,scale=720:960:flags=lanczos,setsar=1"   # убрать подпись автора
CC="scale=720:960:flags=lanczos,setsar=1"
# A0: Ямаль крупно на дриблинге (камера 1, бровка)
seg(0,6,0.0,f"{CB},{Z(1.05,240,700,0.0008)},{CAM('КАМЕРА 1')}",src=1,speed=0.78)
# B: проход в штрафную (камера 2) — заканчивается на кадре разбора
seg(6,10,6.2,f"{crop(120)},{PLATE},{CAM('КАМЕРА 2',880)}",speed=0.963,src=3)
# T: РАЗБОР
seg(10,18,0,f"FILE:{W}/ytseg.mp4")
# B2: удар — слоу-мо (камера 2)
seg(18,20,7.85,f"{crop(120)},{Z(1.0,530,450,0.006)},{MI}",speed=0.525,src=3)
# D: удар сверху сбоку (камера 3) — слоу-мо
seg(20,23,5.25,f"{CA},{Z(1.35,300,250,0.004)},{MI},{CAM('КАМЕРА 3')},fade=in:st=0:d=0.1:color=white",speed=0.39,src=0)
# E: ГОЛ на дроп
seg(23,25,5.75,f"{CA},{shake(26)},{Z(1.3,300,250,-0.004)},"
    f"{txt('ГОООЛ',150,330,FONTI,':enable=lt(t\\,0.75)')},fade=in:st=0:d=0.3:color=white",src=0)
# R: тот же гол с других камер
seg(25,28,1.55,f"{CC},{Z(1.6,360,440,0.003)},{MI},{CAM('КАМЕРА 4')},fade=in:st=0:d=0.1:color=white",speed=0.583,src=2)
seg(28,31,5.4,f"{CB},{Z(1.5,310,570,0.003)},{MI},{CAM('КАМЕРА 1')},fade=in:st=0:d=0.1:color=white",speed=0.544,src=1)
seg(31,34,8.1,f"{crop(120)},{Z(1.3,560,420,0.003)},{MI},{CAM('КАМЕРА 2')},fade=in:st=0:d=0.1:color=white",speed=0.5,src=3)
# F: празднование со всех камер
for b0,ss,cr,z,src in ((34,10.0,CBd,1.1,1),(36,12.8,CBd,1.15,1),(38,15.8,CC,1.2,2),(40,13.0,crop(320),1.1,3),(42,15.5,CBd,1.1,1),(44,16.8,CC,1.25,2)):
    seg(b0,b0+2,ss,f"{cr},{shake(14)},zoompan=z='{z}+0.003*on':d=1:x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2':s=720x960:fps=30,fade=in:st=0:d=0.08:color=white",src=src)
# H: финальная карточка
seg(46,51,17.0,f"{CC},boxblur=6:1,eq=brightness=-0.25,"
    f"zoompan=z='1.2+0.002*on':d=1:x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2':s=720x960:fps=30,"
    f"{txt('ЛАМИН ЯМАЛЬ',82,370)},{txt('1 ГОЛ — 4 КАМЕРЫ',46,480,FONT)},{txt('БЕТИС — БАРСЕЛОНА',36,545,FONT)}",still=True,src=2)

parts=[]
for i,(b0,b1,ss,speed,vf,still,src) in enumerate(segs):
    n=fr(b1)-fr(b0)
    if vf.startswith('FILE:'):
        parts.append(vf[5:]); print('seg',i,'prebuilt'); continue
    p=f"{W}/yseg{i:02d}.mp4"; parts.append(p)
    if still:
        pre=f"trim=end_frame=1,loop=loop={n}:size=1,setpts=N/{FPS}/TB,"
        inp=['-ss',str(ss),'-i',SRCS[src]]
    else:
        pre=f"setpts=(PTS-STARTPTS)/{speed}," if speed!=1 else ""
        inp=['-ss',str(ss),'-t',str(n/FPS*speed+0.3),'-i',SRCS[src]]
    vf_full=f"{pre}fps={FPS},{vf},{GRADE},format=yuv420p"
    if ';' in vf_full or '[' in vf_full:
        args=['-filter_complex',f"[0:v]{vf_full}[out]",'-map','[out]']
    else:
        args=['-vf',vf_full]
    cmd=['ffmpeg','-v','error','-y',*inp,*args,'-an','-frames:v',str(n),'-r',str(FPS),
         '-c:v','libx264','-crf','16','-preset','medium',p]
    r=subprocess.run(cmd,capture_output=True,text=True)
    if r.returncode: print(i,r.stderr); sys.exit(1)
    print('seg',i,n,'frames')
with open(f"{W}/ylist.txt",'w') as f: f.writelines(f"file '{p}'\n" for p in parts)
subprocess.run(['ffmpeg','-v','error','-y','-f','concat','-safe','0','-i',f"{W}/ylist.txt",'-i',f"{W}/track_yamal.wav",
    '-map','0:v','-map','1:a','-c:v','libx264','-crf','17','-preset','medium','-pix_fmt','yuv420p',
    '-c:a','aac','-b:a','192k','-shortest','-movflags','+faststart',OUT],check=True)
print('done',OUT)
