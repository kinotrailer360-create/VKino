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
PLATE=("drawbox=x=(iw-320)/2:y=70:w=320:h=62:color=0x111111@0.92:t=fill,"
       "drawbox=x=(iw-320)/2:y=126:w=320:h=6:color=0xF7B5CD@1:t=fill,"
       f"drawtext=fontfile={FONTI}:text='L.MESSI':fontsize=44:fontcolor=white:x=(w-tw)/2+10:y=80,"
       f"drawtext=fontfile={FONT}:text='10':fontsize=26:fontcolor=0xEDBB00:x=(w-320)/2+12:y=88")
def crop(y,h=960): return f"crop=720:{h}:0:{y},scale=720:960:flags=lanczos,setsar=1"
def shake(a): return (f"scale=792:1056,crop=720:960:x='36+{a}*sin(t*71)*exp(-t*2.2)':"
                      f"y='48+{a}*cos(t*53)*exp(-t*2.2)'")
segs=[]
def seg(b0,b1,ss,vf,speed=1.0,still=False,src=0):
    segs.append((b0,b1,ss,speed,vf,still,src))
Z=lambda z,cx,cy,dz=0: f"zoompan=z='{z}+{dz}*on':d=1:x='{cx}-{cx}/zoom':y='{cy}-{cy}/zoom':s=720x960:fps=30"
MI="minterpolate=fps=30:mi_mode=mci:mc_mode=aobmc"
def CAM(t,y=60): return (f"drawbox=x=40:y={y}:w={len(t)*21+30}:h=50:color=black@0.6:t=fill,"
                    f"drawtext=fontfile={FONT}:text='{t}':fontsize=32:fontcolor=white:x=55:y={y+8}")
# источники: 0 n1 высоко за углом, 1 n2 низко сбоку (24fps), 2 n3 трибуна сбоку, 3 n4 у поля вплотную, 4 n7 трибуна
# A0: розыгрыш (трибуна сбоку)
seg(0,6,5.0,f"{crop(350)},{Z(1.1,360,450,0.001)}",src=2)
# B: Месси с мячом у штрафной (у поля) — заканчивается на кадре разбора
seg(6,10,7.3,f"{crop(100)},{PLATE}",speed=0.99,src=3)
# T: РАЗБОР
seg(10,18,0,f"FILE:{W}/m2tseg.mp4")
# B2: проход между защитниками — слоу-мо
seg(18,20,9.0,f"{crop(100)},{Z(1.0,300,600,0.006)},{MI}",speed=0.525,src=3)
# D: удар вплотную — слоу-мо
seg(20,23,9.45,f"{crop(100)},{Z(1.15,450,620,0.004)},{MI},fade=in:st=0:d=0.1:color=white",speed=0.39,src=3)
# E: ГОЛ на дроп
seg(23,25,9.85,f"{crop(100)},{shake(26)},{Z(1.15,450,620,-0.003)},"
    f"{txt('ГОООЛ',150,330,FONTI,':enable=lt(t\\,0.75)')},fade=in:st=0:d=0.3:color=white",src=3)
# R: тот же гол с других трибун
seg(25,28,1.6,f"{crop(330)},{Z(1.3,400,560,0.003)},{MI},fade=in:st=0:d=0.1:color=white",speed=0.583,src=0)
seg(28,31,11.2,f"{crop(400)},{Z(1.4,600,690,0.003)},{MI},fade=in:st=0:d=0.1:color=white",speed=0.583,src=1)
seg(31,34,6.8,f"{crop(350)},{Z(1.5,480,330,0.003)},{MI},fade=in:st=0:d=0.1:color=white",speed=0.583,src=4)
# КИНО-повтор
seg(34,38,0,f"FILE:{W}/m2cine.mp4")
# F: празднование — Месси у углового, скамейка, болельщики
for b0,ss,cr,z,src in ((38,11.6,crop(250),1.1,2),(40,13.4,crop(250),1.15,2),(42,14.6,crop(250),1.1,3),(44,10.2,crop(200),1.0,4)):
    seg(b0,b0+2,ss,f"{cr},{shake(14)},zoompan=z='{z}+0.003*on':d=1:x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2':s=720x960:fps=30,fade=in:st=0:d=0.08:color=white",src=src)
# H: финальная карточка в дыму
seg(46,51,0,f"FILE:{W}/m2card.mp4")

parts=[]
for i,(b0,b1,ss,speed,vf,still,src) in enumerate(segs):
    n=fr(b1)-fr(b0)
    if vf.startswith('FILE:'):
        parts.append(vf[5:]); print('seg',i,'prebuilt'); continue
    p=f"{W}/nseg{i:02d}.mp4"; parts.append(p)
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
with open(f"{W}/nlist.txt",'w') as f: f.writelines(f"file '{p}'\n" for p in parts)
subprocess.run(['ffmpeg','-v','error','-y','-f','concat','-safe','0','-i',f"{W}/nlist.txt",'-i',f"{W}/track_messi2.wav",
    '-map','0:v','-map','1:a','-c:v','libx264','-crf','17','-preset','medium','-pix_fmt','yuv420p',
    '-c:a','aac','-b:a','192k','-shortest','-movflags','+faststart',OUT],check=True)
print('done',OUT)
