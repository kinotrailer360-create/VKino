# Генерация PNG-элементов брендинга FootyVibe360 для Shorts (1080x1920)
from PIL import Image, ImageDraw, ImageFont
import os
D=os.path.dirname(os.path.abspath(__file__))
F_BLACK='/usr/share/fonts/opentype/inter/InterDisplay-Black.otf'
F_BOLD='/usr/share/fonts/opentype/inter/Inter-Bold.otf'
EMOJI='/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf'
logo=Image.open(f'{D}/logo_round.png')
CHANNEL='FootyVibe360'; HANDLE='@FootyVibe360'

def emoji(ch,size):
    f=ImageFont.truetype(EMOJI,109); im=Image.new('RGBA',(136,128),(0,0,0,0))
    ImageDraw.Draw(im).text((0,0),ch,font=f,embedded_color=True)
    return im.crop(im.getbbox()).resize((size,size),Image.LANCZOS)

def sub_panel(subscribed):
    W,H=900,170; im=Image.new('RGBA',(W,H),(0,0,0,0)); d=ImageDraw.Draw(im)
    d.rounded_rectangle((0,0,W-1,H-1),radius=40,fill=(10,14,30,215),outline=(245,197,24,255),width=4)
    im.alpha_composite(logo.resize((130,130),Image.LANCZOS),(20,20))
    d.text((168,34),CHANNEL,font=ImageFont.truetype(F_BLACK,48),fill='white')
    d.text((170,96),'эдиты и голы каждый день',font=ImageFont.truetype(F_BOLD,26),fill=(190,200,220))
    bx0,by0,bx1,by1=W-330,42,W-24,H-42
    if subscribed:
        d.rounded_rectangle((bx0,by0,bx1,by1),radius=43,fill=(60,64,75))
        t='ПОДПИСАН'; col=(220,220,220)
    else:
        d.rounded_rectangle((bx0,by0,bx1,by1),radius=43,fill=(230,33,23))
        t='ПОДПИСАТЬСЯ'; col='white'
    f=ImageFont.truetype(F_BLACK if not subscribed else F_BOLD,34)
    if subscribed: f=ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',32)
    tw=d.textlength(t,font=f); d.text(((bx0+bx1-tw)/2-(26 if subscribed else 0),(by0+by1)/2-20),t,font=f,fill=col)
    if subscribed: im.alpha_composite(emoji('🔔',40),(bx1-62,(by0+by1)//2-20))
    return im

def like_pill():
    W,H=480,110; im=Image.new('RGBA',(W,H),(0,0,0,0)); d=ImageDraw.Draw(im)
    d.rounded_rectangle((0,0,W-1,H-1),radius=55,fill=(255,255,255,240))
    im.alpha_composite(emoji('👍',64),(26,22))
    d.text((108,24),'ПОСТАВЬ ЛАЙК',font=ImageFont.truetype(F_BLACK,40),fill=(15,15,20))
    return im

def handle_badge():
    W,H=420,58; im=Image.new('RGBA',(W,H),(0,0,0,0)); d=ImageDraw.Draw(im)
    im.alpha_composite(logo.resize((54,54),Image.LANCZOS),(0,2))
    d.text((66,8),HANDLE,font=ImageFont.truetype(F_BOLD,34),fill=(255,255,255,235))
    return im.crop((0,0,int(66+d.textlength(HANDLE,font=ImageFont.truetype(F_BOLD,34)))+4,H))

sub_panel(False).save(f'{D}/cta_subscribe.png')
sub_panel(True).save(f'{D}/cta_subscribed.png')
like_pill().save(f'{D}/cta_like.png')
handle_badge().save(f'{D}/handle_badge.png')
emoji('👆',110).save(f'{D}/tap.png')
print('assets ok')
