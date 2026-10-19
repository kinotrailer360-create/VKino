#!/usr/bin/env bash
# Добавляет брендинг FootyVibe360 и призыв «лайк + подписка» к Shorts-ролику 1080x1920.
# Использование: ./add_cta.sh input.mp4 output.mp4 [время_старта_CTA_в_секундах]
# По умолчанию CTA стартует за 4.2 с до конца ролика.
set -euo pipefail
D="$(cd "$(dirname "$0")" && pwd)"
IN="$1"; OUT="$2"
DUR=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$IN")
T="${3:-$(python3 -c "print(max(0,$DUR-4.2))")}"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

# звуки: «вжух» на выезд плашки и «клик» на нажатие
python3 -I - "$TMP" <<'PY'
import sys, wave, numpy as np
SR=48000; out=sys.argv[1]
def save(name,x):
    x=(np.clip(x,-1,1)*32767).astype(np.int16)
    with wave.open(f"{out}/{name}",'wb') as w: w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR); w.writeframes(x.tobytes())
rng=np.random.default_rng(1)
n=int(0.35*SR); t=np.arange(n)/SR
whoosh=rng.standard_normal(n)*np.sin(np.pi*t/t[-1])**2*0.25
whoosh=np.convolve(whoosh,np.ones(8)/8,'same')
save('whoosh.wav',whoosh)
n=int(0.08*SR); t=np.arange(n)/SR
click=(np.sin(2*np.pi*2200*t)*0.6+rng.standard_normal(n)*0.3)*np.exp(-t/0.012)
save('click.wav',click)
PY

S=$(python3 -c "print($T)")
L=$(python3 -c "print($T+0.5)")
TAP0=$(python3 -c "print($T+1.3)"); TAP1=$(python3 -c "print($T+2.2)")
SW=$(python3 -c "print($T+1.8)")
MS=$(python3 -c "print(int($T*1000))"); MC=$(python3 -c "print(int(($T+1.75)*1000))")

ffmpeg -v error -y -i "$IN" \
  -loop 1 -t "$DUR" -i "$D/handle_badge.png" \
  -loop 1 -t "$DUR" -i "$D/cta_subscribe.png" \
  -loop 1 -t "$DUR" -i "$D/cta_subscribed.png" \
  -loop 1 -t "$DUR" -i "$D/cta_like.png" \
  -loop 1 -t "$DUR" -i "$D/tap.png" \
  -i "$TMP/whoosh.wav" -i "$TMP/click.wav" \
  -filter_complex "
  [1]scale=-1:46,format=rgba[badge];
  [5]scale=96:96,format=rgba[tap];
  [0:v][badge]overlay=x=(W-w)/2:y=176:shortest=1[v1];
  [v1][2]overlay=x=90:y='if(lt(t-$S,0.3),1920-(1920-1230)*sin(PI/2*(t-$S)/0.3),1230)':enable='between(t,$S,$SW)'[v2];
  [v2][3]overlay=x=90:y=1230:enable='gte(t,$SW)'[v3];
  [v3][4]overlay=x='if(lt(t-$L,0.25),-480+(570)*sin(PI/2*(t-$L)/0.25),90)':y=1100:enable='gte(t,$L)'[v4];
  [v4][tap]overlay=x='if(lt(t-$TAP0,0.4),960-(960-790)*(t-$TAP0)/0.4,790)':y='if(lt(t-$TAP0,0.4),1560-(1560-1300)*(t-$TAP0)/0.4,1300+if(between(t-$TAP0,0.45,0.6),12,0))':enable='between(t,$TAP0,$TAP1)'[v];
  [6]adelay=$MS|$MS,volume=0.9[a1];
  [7]adelay=$MC|$MC,volume=1.2[a2];
  [0:a][a1][a2]amix=inputs=3:duration=first:normalize=0[a]" \
  -map "[v]" -map "[a]" -c:v libx264 -crf 17 -preset slow -pix_fmt yuv420p -r 30 \
  -c:a aac -ar 48000 -b:a 192k -t "$DUR" -movflags +faststart "$OUT"
echo "готово: $OUT (CTA с $S с)"
