// Снимает кадры анимации через Playwright: кадр n рисуется ровно в момент t = n / FPS.
// node render.js [каталог_кадров] [--frames 0,45,90] [--workers 4]
const http = require('http');
const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright');
const T = require('./kharcho_animation.js');

const ROOT = path.resolve(__dirname, '..');
const args = process.argv.slice(2);
const outDir = path.resolve(args.find((a) => !a.startsWith('--') && !/^[\d,]+$/.test(a)) || path.join(__dirname, 'frames'));
const opt = (name, def) => { const i = args.indexOf(name); return i >= 0 ? args[i + 1] : def; };
const workers = Number(opt('--workers', 4));
const total = Math.round(T.DURATION * T.FPS);
const frames = opt('--frames', null) ? opt('--frames').split(',').map(Number) : [...Array(total).keys()];

const TYPES = { '.html': 'text/html', '.js': 'text/javascript', '.ttf': 'font/ttf' };
function serve() {
  const srv = http.createServer((req, res) => {
    const file = path.join(ROOT, decodeURIComponent(req.url.split('?')[0]));
    if (!file.startsWith(ROOT) || !fs.existsSync(file)) { res.writeHead(404); return res.end(); }
    res.writeHead(200, { 'Content-Type': TYPES[path.extname(file)] || 'application/octet-stream' });
    fs.createReadStream(file).pipe(res);
  });
  return new Promise((ok) => srv.listen(0, '127.0.0.1', () => ok(srv)));
}

(async () => {
  fs.mkdirSync(outDir, { recursive: true });
  const srv = await serve();
  const url = `http://127.0.0.1:${srv.address().port}/kharcho/index.html?render`;
  const browser = await chromium.launch({ args: ['--disable-gpu-vsync', '--force-color-profile=srgb'] });
  const started = Date.now();
  let done = 0;
  const queue = frames.slice();
  await Promise.all([...Array(Math.min(workers, frames.length))].map(async () => {
    const page = await browser.newPage({ viewport: { width: 1920, height: 1080 } });
    page.on('pageerror', (e) => { console.error('page error:', e.message); process.exitCode = 1; });
    await page.goto(url);
    await page.evaluate(() => window.ready);
    while (queue.length) {
      const n = queue.shift();
      const data = await page.evaluate((t) => {
        KHARCHO.renderFrame(t);
        return document.getElementById('c').toDataURL('image/png');
      }, n / T.FPS);
      fs.writeFileSync(path.join(outDir, `f${String(n).padStart(4, '0')}.png`), Buffer.from(data.split(',')[1], 'base64'));
      done++;
      if (done % 60 === 0) console.log(`${done}/${frames.length} кадров, ${((Date.now() - started) / 1000).toFixed(0)} с`);
    }
    await page.close();
  }));
  await browser.close();
  srv.close();
  console.log(`готово: ${done} кадров за ${((Date.now() - started) / 1000).toFixed(1)} с → ${outDir}`);
})();
