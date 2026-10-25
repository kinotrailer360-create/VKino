#!/usr/bin/env bash
# Полная сборка: звук → озвучка → кадры (Playwright) → сведение и кодирование (FFmpeg) → контактный лист.
set -euo pipefail
cd "$(dirname "$0")"
FRAMES=${FRAMES:-build/frames}
mkdir -p build

node kharcho_audio.js
python3 kharcho_voice.py "${PIPER_MODEL:-/opt/piper/voice.onnx}"

# SKIP_FRAMES=1 — пересобрать только звук и видео из уже снятых кадров
if [ -z "${SKIP_FRAMES:-}" ]; then
  rm -rf "$FRAMES"
  node render.js "$FRAMES" --workers "${WORKERS:-4}"
fi

# Сведение: голос чуть компрессируется, музыка приглушается под голос (sidechain),
# эффекты тихо под всем; лимитер страхует от перегрузки.
ffmpeg -v error -y \
  -framerate 30 -i "$FRAMES/f%04d.png" \
  -i kharcho_voice.wav -i kharcho_music.wav -i kharcho_sfx.wav \
  -filter_complex "
    [1:a]aformat=sample_rates=48000:channel_layouts=stereo,highpass=f=90,equalizer=f=3200:t=q:w=1.2:g=2.5,
         acompressor=threshold=0.12:ratio=3:attack=8:release=120:makeup=1.4,volume=1.0,asplit=2[v][key];
    [2:a]volume=0.42[m];
    [m][key]sidechaincompress=threshold=0.02:ratio=8:attack=25:release=400:makeup=1[md];
    [3:a]volume=0.32[s];
    [v][md][s]amix=inputs=3:normalize=0:duration=first,volume=1.65,
         alimiter=limit=0.8:attack=5:release=60:level=0,atrim=0:30,asetpts=N/SR/TB[a]" \
  -map 0:v -map "[a]" \
  -c:v libx264 -preset slow -crf 20 -profile:v high -pix_fmt yuv420p -r 30 \
  -c:a aac -b:a 192k -ar 48000 \
  -t 30 -movflags +faststart kharcho_final.mp4

# Контактный лист: 24 кадра каждые 1,25 с (0,0 … 28,75) + последний кадр 29,97 с.
rm -rf build/sheet && mkdir -p build/sheet
i=0
for t in $(seq 0 1.25 28.75) 29.966; do
  ffmpeg -v error -y -ss "$t" -i kharcho_final.mp4 -frames:v 1 \
    -vf "scale=480:-1,drawtext=fontfile=../shorts/assets/Montserrat.ttf:text='${t} s':x=10:y=10:fontsize=22:fontcolor=white:box=1:boxcolor=black@0.6" \
    "build/sheet/$(printf %02d $i).png"
  i=$((i+1))
done
ffmpeg -v error -y -framerate 1 -i build/sheet/%02d.png -vf "tile=5x5:padding=4:color=black" -frames:v 1 -q:v 3 kharcho_contact_sheet.jpg
echo "готово: kharcho_final.mp4, kharcho_contact_sheet.jpg"
