import math, random, sys
from pathlib import Path
from PIL import Image, ImageDraw, ImageFilter, ImageFont
ROOT = Path(__file__).resolve().parents[2]; A = ROOT/"shorts/assets"; OUT = ROOT/"promo/rsya"
LOGO = Image.open(A/"logo.png").convert("RGBA")
def F(name, size): return ImageFont.truetype(str(A/name), size)
BLUE=(40,170,230); ORANGE=(245,130,40); WHITE=(255,255,255)

def bg(w,h):
    im = Image.new("RGB",(w,h),(8,12,28)); d = ImageDraw.Draw(im)
    for y in range(h):
        t=y/h; d.line([(0,y),(w,y)], fill=(int(8+10*t),int(14+8*t),int(34+12*t)))
    glow = Image.new("RGB",(w,h),(0,0,0)); g=ImageDraw.Draw(glow); r=max(w,h)
    g.ellipse([-r*0.3,-r*0.4,r*0.5,r*0.4], fill=(20,90,150))
    g.ellipse([w-r*0.45,h-r*0.35,w+r*0.35,h+r*0.45], fill=(150,70,15))
    glow = glow.filter(ImageFilter.GaussianBlur(r/6))
    im = Image.blend(im, Image.eval(glow,lambda v:v), 0.55)
    d = ImageDraw.Draw(im); rnd=random.Random(w*h)
    for _ in range(int(w*h/9000)):
        x,y=rnd.randrange(w),rnd.randrange(h); s=rnd.choice([1,1,2])
        d.ellipse([x,y,x+s,y+s], fill=(200,220,255))
    # плёнка внизу и вверху
    s = max(10, min(w,h)//22)
    for yy in (0, h-s):
        d.rectangle([0,yy,w,yy+s], fill=(16,18,26))
        for x in range(s//3, w, s): d.rounded_rectangle([x, yy+s*0.25, x+s*0.5, yy+s*0.75], radius=2, fill=(60,66,80))
    return im

def fit(text, font_name, max_w, size):
    while size>8:
        f=F(font_name,size)
        if f.getbbox(text)[2] <= max_w: return f
        size-=1
    return F(font_name,size)

def logo(im, box):
    x,y,w,h = box; l=LOGO.copy(); l.thumbnail((w,h), Image.LANCZOS)
    im.paste(l,(int(x+(w-l.width)/2),int(y+(h-l.height)/2)),l)

def button(d, cx, y, text, size, maxw):
    f = fit(text,"Montserrat.ttf",maxw,size); size=f.size; bw=f.getbbox(text)[2]; bh=int(size*1.9)
    d.rounded_rectangle([cx-bw/2-size, y, cx+bw/2+size, y+bh], radius=bh//2, fill=ORANGE)
    d.text((cx,y+bh/2), text, font=f, fill=WHITE, anchor="mm")
    return bh

def text_c(d,cx,y,text,font,fill=WHITE):
    d.text((cx,y),text,font=font,fill=fill,anchor="ma"); b=font.getbbox(text); return b[3]-b[1]

HEAD=["НОВИНКИ КИНО","КАЖДЫЙ ДЕНЬ"]; SUB="Трейлеры · премьеры · подборки"; CTA="Подписаться в Telegram"

def layout(w,h,name,with_cta=True):
    im=bg(w,h); d=ImageDraw.Draw(im); pad=int(min(w,h)*0.06); s=max(10,min(w,h)//22)
    ratio=w/h
    if ratio>2.2:  # узкий горизонтальный
        logo(im,(pad*0.5,s,h*1.3,h-2*s)); x0=pad*0.5+h*1.4; avail=w-x0-pad*0.5
        cta = CTA if w>=600 else "Подписаться"
        bf=int(h*(0.16 if ratio<4.5 else 0.2)); f=fit(cta,"Montserrat.ttf",avail*0.36,bf); bf=f.size
        bw=f.getbbox(cta)[2]+2*bf; bh=int(bf*1.9); bx=w-pad*0.5-bw
        d.rounded_rectangle([bx,(h-bh)/2,bx+bw,(h+bh)/2],radius=bh//2,fill=ORANGE)
        d.text((bx+bw/2,h/2),cta,font=f,fill=WHITE,anchor="mm"); avail=bx-x0-pad*0.5
        if ratio<4.5:  # две строки заголовка
            hf=fit(HEAD[1],"Oswald.ttf",avail,int(h*0.24)); sub = h>=150
            sf=fit(SUB,"Montserrat.ttf",avail,int(h*0.09)) if sub else None
            total=hf.size*2.3+(sf.size*1.5 if sub else 0); y=(h-total)/2
            d.text((x0,y),HEAD[0],font=hf,fill=WHITE); y+=hf.size*1.15
            d.text((x0,y),HEAD[1],font=hf,fill=ORANGE); y+=hf.size*1.3
            if sub: d.text((x0,y),SUB,font=sf,fill=(200,228,250))
        else:
            hf=fit(" ".join(HEAD),"Oswald.ttf",avail,int(h*0.36))
            if h>=90:
                d.text((x0,h*0.5)," ".join(HEAD),font=hf,fill=WHITE,anchor="ls")
                sf=fit(SUB,"Montserrat.ttf",avail,int(h*0.17)); d.text((x0,h*0.55),SUB,font=sf,fill=(200,228,250),anchor="la")
            else:
                d.text((x0,h/2)," ".join(HEAD),font=hf,fill=WHITE,anchor="lm")
    elif ratio>=1.5:  # горизонтальный
        logo(im,(pad,s+pad*0.5,w*0.42,h-2*s-pad)); x0=pad+w*0.45; cx=x0+(w-x0-pad)/2; avail=w-x0-pad
        f=fit(HEAD[0],"Oswald.ttf",avail,int(h*0.17)); f=fit(HEAD[1],"Oswald.ttf",avail,f.size); hs=f.size
        sf=fit(SUB,"Montserrat.ttf",avail,int(h*0.065)); bs=int(h*0.06)
        total=hs*2.5+sf.size*1.6+(bs*2.2 if with_cta else 0); y=(h-total)/2
        y+=text_c(d,cx,y,HEAD[0],f)+hs*0.25; y+=text_c(d,cx,y,HEAD[1],f,ORANGE)+hs*0.4
        y+=text_c(d,cx,y,SUB,sf,(200,228,250))+sf.size*1.2
        if with_cta: button(d,cx,y,CTA,bs,avail*0.85)
    else:  # квадрат / вертикаль
        cx=w/2; avail=w-2*pad; lh=h*(0.36 if ratio>1.1 else 0.42 if ratio>0.8 else 0.38)
        if ratio<0.6: lh=h*0.3
        logo(im,(pad,s+pad*0.6,avail,lh)); y=s+pad*0.6+lh+pad*0.5
        hs=int(min(w*0.12,h*0.11)); f=fit(HEAD[0],"Oswald.ttf",avail,hs); f=fit(HEAD[1],"Oswald.ttf",avail,f.size)
        y+=text_c(d,cx,y,HEAD[0],f)+f.size*0.25; y+=text_c(d,cx,y,HEAD[1],f,ORANGE)+f.size*0.45
        sf=fit(SUB,"Montserrat.ttf",avail,int(min(w*0.055,h*0.05))); y+=text_c(d,cx,y,SUB,sf,(200,228,250))+f.size*0.5
        if with_cta: button(d,cx,y,CTA,int(min(w*0.05,h*0.045)),avail*0.85)
    d.rectangle([0,0,w-1,h-1],outline=(90,100,120),width=1)  # рамка — требование для ТГО
    q = 92
    im.save(OUT/name, "JPEG", quality=q, optimize=True)
    while (OUT/name).stat().st_size > 120_000 and q>60:
        q-=6; im.save(OUT/name,"JPEG",quality=q,optimize=True)

# Текстово-графические объявления: картинки (без кнопки — её даёт сам Директ)
TGO = OUT/"tgo"; TGO.mkdir(exist_ok=True)
for w,h,n in [(1080,607,"16x9"),(1080,1080,"1x1"),(1080,1440,"3x4"),(1080,810,"4x3")]:
    layout(w,h,f"tgo/kinomir360_{n}_{w}x{h}.jpg",with_cta=False)
# Графические объявления (баннеры) — фиксированные размеры РСЯ
BAN = OUT/"banners"; BAN.mkdir(exist_ok=True)
for w,h in [(240,400),(300,250),(300,500),(300,600),(336,280),(728,90),(970,250),(320,50),(320,100),(320,480),(480,320),(640,100),(640,200),(640,960),(960,640),(1000,120)]:
    layout(w,h,f"banners/kinomir360_{w}x{h}.jpg")
print("ok")
