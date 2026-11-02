# Заставка и концовка выпуска «Новости кино» (Remotion)

Исходник анимаций `shorts/assets/weekly_intro.mp4` и `weekly_outro.mp4`. Бот их не собирает — берёт готовые файлы.

Переделать:

```bash
cd tools/intro && npm i
mkdir -p public && cp ../../shorts/assets/{logo.png,Oswald.ttf,Montserrat.ttf,whoosh.wav,boom.wav} public/
npx remotion studio            # посмотреть и поправить
npx remotion render Intro ../../shorts/assets/weekly_intro.mp4 --codec=h264 --crf=18
npx remotion render Outro ../../shorts/assets/weekly_outro.mp4 --codec=h264 --crf=18
```
