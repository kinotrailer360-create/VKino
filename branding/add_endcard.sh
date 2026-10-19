#!/usr/bin/env bash
# Добавляет к Shorts-ролику 1080x1920 ник канала и концовку «лайк + подписка» ПОСЛЕ основного видео.
# Концовка сделана под бесшовный повтор: фон — размытый ПЕРВЫЙ кадр ролика, в конце размытие
# и плашки уходят, и последний кадр совпадает с первым, поэтому при автоповторе склейки не видно.
# Использование: ./add_endcard.sh input.mp4 output.mp4 [длина_концовки_с=2.6] [обрезать_начало_с=0]
# (обрезка начала нужна, если ролик начинается с затемнения — для повтора первый кадр должен быть картинкой)
set -euo pipefail
D="$(cd "$(dirname "$0")" && pwd)"
IN0="$1"; OUT="$2"; C="${3:-2.6}"; CUT="${4:-0}"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
IN="$TMP/src.mp4"
ffmpeg -v error -y -ss "$CUT" -i "$IN0" -c:v libx264 -crf 15 -preset fast -c:a aac -b:a 256k "$IN"
calc(){ python3 -c "print(round($1,3))"; }

# 1) основное видео + ник канала
ffmpeg -v error -y -i "$IN" -loop 1 -i "$D/handle_badge.png" -filter_complex "
  [1]scale=-1:46,format=rgba[badge];[0:v][badge]overlay=x=(W-w)/2:y=176:shortest=1,format=yuv420p[v];
  [0:a]aformat=sample_rates=48000:channel_layouts=stereo[a]" \
  -map "[v]" -map "[a]" -r 30 -c:v libx264 -crf 17 -preset medium -c:a aac -b:a 192k "$TMP/main.mp4"

# первый кадр готового ролика (с ником) — фон концовки
ffmpeg -v error -y -i "$TMP/main.mp4" -frames:v 1 "$TMP/first.png"

# звуки: «вжух» и «клик»
python3 -I - "$TMP" <<'PY'
import sys, wave, numpy as np
SR=48000; out=sys.argv[1]; rng=np.random.default_rng(1)
def save(name,x):
    x=(np.clip(x,-1,1)*32767).astype(np.int16)
    with wave.open(f"{out}/{name}",'wb') as w: w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR); w.writeframes(x.tobytes())
n=int(0.35*SR); t=np.arange(n)/SR
save('whoosh.wav',np.convolve(rng.standard_normal(n)*np.sin(np.pi*t/t[-1])**2*0.25,np.ones(8)/8,'same'))
n=int(0.08*SR); t=np.arange(n)/SR
save('click.wav',(np.sin(2*np.pi*2200*t)*0.6+rng.standard_normal(n)*0.3)*np.exp(-t/0.012))
PY

L=0.35; P=0.15; TAP0=0.9; SW=$(calc "$TAP0+0.45"); TAP1=$(calc "$TAP0+0.85")
OUTF=$(calc "$C-0.55")   # с этого момента плашки уходят, фон становится резким
MC=$(calc "($TAP0+0.42)*1000")

# 2) концовка: видео
ffmpeg -v error -y -loop 1 -t "$C" -i "$TMP/first.png" \
  -loop 1 -t "$C" -i "$D/logo_round.png" \
  -loop 1 -t "$C" -i "$D/cta_like.png" \
  -loop 1 -t "$C" -i "$D/cta_subscribe.png" \
  -loop 1 -t "$C" -i "$D/cta_subscribed.png" \
  -loop 1 -t "$C" -i "$D/tap.png" \
  -filter_complex "
  [0]split[f0][f1];
  [f0]boxblur=40:3,eq=brightness=-0.35:saturation=1.2[bg];
  [f1]format=rgba,fade=in:st=$OUTF:d=0.5:alpha=1[sharp];
  [1]scale=300:300,format=rgba,fade=in:st=0:d=0.25:alpha=1,fade=out:st=$OUTF:d=0.35:alpha=1[logo];
  [2]format=rgba,fade=out:st=$OUTF:d=0.35:alpha=1[like];
  [3]format=rgba,fade=out:st=$OUTF:d=0.35:alpha=1[sub];
  [4]format=rgba,fade=out:st=$OUTF:d=0.35:alpha=1[subd];
  [5]scale=96:96,format=rgba[tap];
  [bg][logo]overlay=x=(W-w)/2:y=520[a];
  [a]drawtext=fontfile=/usr/share/fonts/opentype/inter/InterDisplay-Black.otf:text='FootyVibe360':fontsize=80:fontcolor=white:borderw=5:bordercolor=black:x=(w-tw)/2:y=850:alpha='if(lt(t,$OUTF),min(1,t/0.25),max(0,1-(t-$OUTF)/0.35))'[b];
  [b][like]overlay=x='if(lt(t-$L,0.25),-480+(780)*sin(PI/2*(t-$L)/0.25),300)':y=990:enable='gte(t,$L)'[c];
  [c][sub]overlay=x=90:y='if(lt(t-$P,0.3),1920-(1920-1140)*sin(PI/2*(t-$P)/0.3),1140)':enable='between(t,$P,$SW)'[d];
  [d][subd]overlay=x=90:y=1140:enable='gte(t,$SW)'[e];
  [e][tap]overlay=x='if(lt(t-$TAP0,0.4),960-(960-790)*(t-$TAP0)/0.4,790)':y='if(lt(t-$TAP0,0.4),1520-(1520-1210)*(t-$TAP0)/0.4,1210+if(between(t-$TAP0,0.42,0.55),12,0))':enable='between(t,$TAP0,$TAP1)'[g];
  [g][sharp]overlay,format=yuv420p[v]" \
  -map "[v]" -r 30 -c:v libx264 -crf 17 -preset medium -t "$C" "$TMP/card_v.mp4"

# 3) концовка: звук — начало трека ролика приглушённо (подводит к повтору) + эффекты
ffmpeg -v error -y -i "$IN" -i "$TMP/whoosh.wav" -i "$TMP/click.wav" -filter_complex "
  [0:a]atrim=0:$C,asetpts=PTS-STARTPTS,lowpass=f=1200,volume=0.8[m];
  [1]adelay=100|100[w]; [2]adelay=${MC%.*}|${MC%.*},volume=1.2[k];
  [m][w][k]amix=inputs=3:duration=first:normalize=0,aformat=sample_rates=48000:channel_layouts=stereo[a]" \
  -map "[a]" -c:a aac -b:a 192k "$TMP/card_a.m4a"
ffmpeg -v error -y -i "$TMP/card_v.mp4" -i "$TMP/card_a.m4a" -c copy -shortest "$TMP/card.mp4"

# 4) склейка
printf "file '%s'\nfile '%s'\n" "$TMP/main.mp4" "$TMP/card.mp4" > "$TMP/list.txt"
ffmpeg -v error -y -f concat -safe 0 -i "$TMP/list.txt" -c:v libx264 -crf 17 -preset slow -pix_fmt yuv420p -r 30 \
  -c:a aac -ar 48000 -b:a 192k -movflags +faststart "$OUT"
echo "готово: $OUT"
