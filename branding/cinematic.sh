#!/usr/bin/env bash
# «Киношный» регрейд фрагмента: тёмный тил-зелёный грейд, лучи прожектора, дрейфующий туман, свечение.
# Использование: cinematic.sh in.mp4 out.mp4 "<фильтры подготовки: обрезка/слоу-мо/зум, на выходе 720x960>" длительность
set -euo pipefail
FX="$(cd "$(dirname "$0")" && pwd)/fx"
IN="$1"; OUT="$2"; PREP="$3"; DUR="$4"
ffmpeg -v error -y -i "$IN" -loop 1 -i "$FX/rays.png" -loop 1 -i "$FX/fog.png" -filter_complex "
 [0:v]$PREP,eq=brightness=-0.09:contrast=1.32:saturation=0.72,
  colorbalance=rs=-0.10:gs=0.03:bs=0.07:rm=-0.06:gm=0.02:bm=0.04:rh=0.03:bh=-0.02,
  vignette=PI/2.4,format=gbrp,split[b][g];
 [g]gblur=sigma=22[glow];
 [b][glow]blend=all_mode=screen:all_opacity=0.28[v1];
 [1]format=gbrp,scale=720:960[r];
 [v1][r]blend=all_mode=screen:all_opacity=0.75[v2];
 [2]format=gbrp,crop=720:960:x='mod(t*28,880)':y=0[f];
 [v2][f]blend=all_mode=screen:all_opacity=0.33,format=yuv420p[v]" \
 -map "[v]" -t "$DUR" -r 30 -an -c:v libx264 -crf 16 -preset medium "$OUT"
