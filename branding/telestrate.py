# Тактическая «телестрация» поверх стоп-кадра: луч+кольцо, стрелка паса, лупа, заштрихованная зона.
# Рендерит PNG-последовательность (RGBA) для наложения через ffmpeg.
# Использование: python3 telestrate.py frame.png out_dir spec.json
import sys, json, math, os
from PIL import Image, ImageDraw, ImageFilter, ImageFont
FPS=30; YEL=(255,230,40)
FONT='/usr/share/fonts/opentype/inter/InterDisplay-BlackItalic.otf'
frame=Image.open(sys.argv[1]).convert('RGBA'); OUT=sys.argv[2]; spec=json.load(open(sys.argv[3]))
W,H=frame.size; os.makedirs(OUT,exist_ok=True)
ease=lambda p: 1-(1-max(0,min(1,p)))**3

def beam(img,x,y,p,label=None):
    if p<=0: return
    a=ease(p); L=Image.new('RGBA',(W,H),(0,0,0,0)); d=ImageDraw.Draw(L)
    top=max(0,y-260)
    for i in range(14):                       # световой столб, мягкий по краям
        w=36-i*2; al=int(16*a)
        d.rectangle((x-w,top+int((1-a)*(y-top)),x+w,y),fill=(255,235,80,al))
    rx,ry=int(46*a),int(16*a)                 # кольцо на газоне
    d.ellipse((x-rx,y-ry,x+rx,y+ry),outline=YEL+(int(255*a),),width=5)
    d.ellipse((x-rx-10,y-ry-5,x+rx+10,y+ry+5),outline=YEL+(int(140*a),),width=2)
    img.alpha_composite(L.filter(ImageFilter.GaussianBlur(1)))
    if label and a>0.6:
        f=ImageFont.truetype(FONT,30); d2=ImageDraw.Draw(img); tw=d2.textlength(label,font=f)
        lx=min(max(x-tw/2,16),W-tw-16)
        d2.rounded_rectangle((lx-12,y+28,lx+tw+12,y+70),radius=10,fill=(0,0,0,int(170*a)))
        d2.text((lx,y+32),label,font=f,fill=YEL+(int(255*a),))

def arrow(img,pts,p,curve=0.0):
    if p<=0: return
    (x0,y0),(x1,y1)=pts; mx,my=(x0+x1)/2,(y0+y1)/2; nx,ny=-(y1-y0),(x1-x0)
    cx,cy=mx+nx*curve,my+ny*curve; e=ease(p); path=[]
    for i in range(41):
        t=i/40*e; path.append(((1-t)**2*x0+2*(1-t)*t*cx+t*t*x1,(1-t)**2*y0+2*(1-t)*t*cy+t*t*y1))
    d=ImageDraw.Draw(img); d.line(path,fill=(0,0,0,120),width=11,joint='curve'); d.line(path,fill=YEL+(255,),width=7,joint='curve')
    if e>0.95:
        (ax,ay),(bx,by)=path[-3],path[-1]; ang=math.atan2(by-ay,bx-ax); s=22
        tri=[(bx+math.cos(ang)*6,by+math.sin(ang)*6),(bx-s*math.cos(ang-0.5),by-s*math.sin(ang-0.5)),(bx-s*math.cos(ang+0.5),by-s*math.sin(ang+0.5))]
        d.polygon(tri,fill=YEL+(255,))

def zone(img,poly,p,col=(60,255,140),label=None):
    if p<=0: return
    a=ease(p); M=Image.new('L',(W,H),0); ImageDraw.Draw(M).polygon(poly,fill=255)
    Hh=Image.new('RGBA',(W,H),(0,0,0,0)); d=ImageDraw.Draw(Hh)
    for k in range(-H,W+H,18): d.line([(k,0),(k+H,H)],fill=col+(int(150*a),),width=5)
    Hh.putalpha(Image.eval(M,lambda v:int(v*0.6*a)))
    img.alpha_composite(Hh); ImageDraw.Draw(img).polygon(poly,outline=col+(int(255*a),),width=3)
    if label and a>0.6:
        f=ImageFont.truetype(FONT,26); d=ImageDraw.Draw(img); cx=sum(v[0] for v in poly)/len(poly); cy=sum(v[1] for v in poly)/len(poly)
        tw=d.textlength(label,font=f); d.rounded_rectangle((cx-tw/2-10,cy-20,cx+tw/2+10,cy+18),radius=8,fill=(0,0,0,int(170*a)))
        d.text((cx-tw/2,cy-17),label,font=f,fill=col+(int(255*a),))

def lens(img,x,y,r,zoom,p,at=None):
    if p<=0: return
    a=ease(p); rr=int(r*a)
    if rr<4: return
    dx,dy=at if at else (x,y)
    src=frame.crop((int(x-rr/zoom),int(y-rr/zoom),int(x+rr/zoom),int(y+rr/zoom))).resize((2*rr,2*rr),Image.LANCZOS)
    m=Image.new('L',(2*rr,2*rr),0); ImageDraw.Draw(m).ellipse((0,0,2*rr-1,2*rr-1),fill=255); src.putalpha(m)
    d=ImageDraw.Draw(img)
    if at:                                     # выноска: кружок на игроке + линия к увеличению
        d.ellipse((x-26,y-26,x+26,y+26),outline=YEL+(255,),width=3)
        ang=math.atan2(dy-y,dx-x); d.line([(x+26*math.cos(ang),y+26*math.sin(ang)),(dx-rr*math.cos(ang),dy-rr*math.sin(ang))],fill=YEL+(255,),width=3)
    img.alpha_composite(src,(int(dx-rr),int(dy-rr)))
    d.ellipse((dx-rr,dy-rr,dx+rr,dy+rr),outline=YEL+(255,),width=5)
    d.ellipse((dx-rr-8,dy-rr-8,dx+rr+8,dy+rr+8),outline=(255,255,255,int(120*a)),width=2)

N=int(spec['duration']*FPS)
for i in range(N):
    t=i/FPS; img=Image.new('RGBA',(W,H),(0,0,0,0))
    for el in spec['elements']:
        p=(t-el['start'])/el.get('anim',0.4)
        if t>el.get('end',1e9): continue
        k=el['type']
        if k=='zone': zone(img,[tuple(v) for v in el['poly']],p,label=el.get('label'))
        elif k=='arrow': arrow(img,el['pts'],p,el.get('curve',0))
        elif k=='beam': beam(img,*el['pos'],p,el.get('label'))
        elif k=='lens': lens(img,*el['pos'],el['r'],el.get('zoom',2.0),p,el.get('at'))
    img.save(f"{OUT}/t{i:04d}.png")
print('frames',N)
