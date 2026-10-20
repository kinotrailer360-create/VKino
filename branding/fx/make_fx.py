# Текстуры для «киношного» регрейда: световые лучи и туман (рендерятся один раз)
import numpy as np, sys
from PIL import Image, ImageDraw, ImageFilter
OUT=sys.argv[1]; W,H=720,960; rng=np.random.default_rng(5)
# лучи прожектора из правого верхнего угла
im=Image.new('L',(W,H),0); d=ImageDraw.Draw(im)
ox,oy=W*0.85,-80
for ang,wd,a in ((112,70,120),(120,45,90),(104,35,70),(128,60,60),(97,25,50)):
    t=np.radians(ang); L=1800
    ex,ey=ox+L*np.cos(t),oy+L*np.sin(t); nx,ny=-np.sin(t),np.cos(t)
    d.polygon([(ox-nx*6,oy-ny*6),(ox+nx*6,oy+ny*6),(ex+nx*wd*6,ey+ny*wd*6),(ex-nx*wd*6,ey-ny*wd*6)],fill=a)
im=im.filter(ImageFilter.GaussianBlur(40))
fall=np.linspace(1,0.15,H)[:,None]          # лучи гаснут к низу кадра
arr=(np.asarray(im)*fall).clip(0,255).astype(np.uint8)
Image.fromarray(np.dstack([arr*0.95,arr,arr*0.9]).astype(np.uint8)).save(f'{OUT}/rays.png')
# туман: многоуровневый шум, широкая текстура для «дрейфа»
FW,FH=1600,960; f=np.zeros((FH,FW))
for s,amp in ((256,1.0),(96,0.5),(32,0.25)):
    n=rng.random((FH//s+2,FW//s+2)); n=Image.fromarray((n*255).astype(np.uint8)).resize((FW+2*s,FH+2*s),Image.BICUBIC)
    f+=amp*np.asarray(n.filter(ImageFilter.GaussianBlur(s/3)))[s:s+FH,s:s+FW]/255
f=(f-f.min())/(f.max()-f.min()); f=np.clip((f-0.35)*1.8,0,1)
f*=np.linspace(0.5,1,FH)[:,None]            # гуще у газона
g=(f*200).astype(np.uint8)
Image.fromarray(np.dstack([g*0.85,g,g*0.95]).astype(np.uint8)).save(f'{OUT}/fog.png')
print('fx ok')
