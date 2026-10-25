/*
 * Харчо — 30-секундная кулинарная анимация на Canvas 2D.
 *
 * Всё нарисовано процедурно: текстуры дерева, бульона, мяса, риса и зелени
 * создаются при запуске, никаких внешних картинок нет. Кадр — чистая функция
 * времени: renderFrame(t) всегда рисует одно и то же, поэтому кадры можно
 * снимать в любом порядке и в нескольких вкладках сразу.
 *
 * Что редактировать:
 *   SCENES   — сцены и их точные границы (секунды), тип перехода в сцену;
 *   CAPTIONS — подписи на экране;
 *   CARDS    — карточки с ингредиентами по группам;
 *   VOICE    — фразы диктора и моменты их начала (tts — текст для синтеза,
 *              если нужно поправить ударение фонемами Piper: [[...]]);
 *   SFX      — звуковые эффекты (используются kharcho_audio.js).
 *
 * Файл работает и в браузере (window.KHARCHO), и в Node (require) — Node берёт
 * отсюда только таймлайн для озвучки и звука.
 */
(function (root) {
  'use strict';

  const W = 1920, H = 1080, FPS = 30, DURATION = 30;

  // ---------------------------------------------------------------- таймлайн
  // stage — этап сценария; hook состоит из двух планов: тарелка и кастрюля.
  const SCENES = [
    { id: 'hookBowl',    stage: 'hook',        start: 0,    end: 1.9, transitionIn: null },
    { id: 'hookPot',     stage: 'hook',        start: 1.9,  end: 3,   transitionIn: 'whip' },
    { id: 'ingredients', stage: 'ingredients', start: 3,    end: 7,   transitionIn: 'zoom' },
    { id: 'broth',       stage: 'broth',       start: 7,    end: 12,  transitionIn: 'whip' },
    { id: 'dressing',    stage: 'dressing',    start: 12,   end: 17,  transitionIn: 'whip' },
    { id: 'assemble',    stage: 'assemble',    start: 17,   end: 22,  transitionIn: 'zoom' },
    { id: 'spices',      stage: 'spices',      start: 22,   end: 26,  transitionIn: 'zoom' },
    { id: 'serve',       stage: 'serve',       start: 26,   end: 30,  transitionIn: 'whip' },
  ];
  const TRANSITION = 0.5; // длительность перехода, по центру границы сцен

  const CAPTIONS = [
    { start: 0.35, end: 2.8,  style: 'title', accent: 'Харчо', text: ' — насыщенный вкус Кавказа' },
    { start: 3.25, end: 6.85, style: 'top',   text: 'На 4–5 порций' },
    { start: 7.45, end: 11.85, style: 'main', text: 'Говядина — до мягкости' },
    { start: 12.3, end: 16.85, style: 'main', text: 'Лук + томаты + ткемали' },
    { start: 17.3, end: 21.85, style: 'main', text: 'Добавляем рис и заправку' },
    { start: 22.3, end: 25.85, style: 'main', text: 'Чеснок, специи, кинза' },
    { start: 26.7, end: 30.5, style: 'final', text: 'Дай настояться 10 минут.', sub: 'Приятного аппетита!' },
  ];

  const CARDS = [
    { start: 7.6, end: 11.85, title: 'Бульон', rows: [
      ['Говядина на кости', '600 г', true], ['Вода', '2 л'], ['Соль', 'по вкусу']],
      note: 'Варить 1,5–2 часа, снимая пену' },
    { start: 12.45, end: 16.85, title: 'Заправка', rows: [
      ['Лук', '2 шт.'], ['Помидоры', '2 шт. / 200 г'], ['Томатная паста', '1 ст. л.'],
      ['Ткемали', '2 ст. л.', true], ['Растительное масло', '1–2 ст. л.']] },
    { start: 17.45, end: 21.85, title: 'В кастрюлю', rows: [
      ['Рис, промытый', '80 г', true], ['Заправка', 'вся']],
      note: 'Варить до готовности риса' },
    { start: 22.45, end: 25.85, title: 'Аромат', rows: [
      ['Чеснок', '4 зубчика', true], ['Хмели-сунели', '1 ч. л.', true], ['Грецкие орехи*', '50 г'],
      ['Кинза', 'пучок'], ['Чёрный перец', 'по вкусу']],
      note: '* по желанию' },
  ];

  const VOICE = [
    { at: 0.30,  text: 'Готовим ароматный харчо!' },
    { at: 3.40,  text: 'Возьми шестьсот граммов говядины' },
    { at: 7.70,  text: 'и вари до мягкости.' },
    { at: 12.25, text: 'Обжарь лук с томатами и ложкой томатной пасты.',
      tts: '[[ʌbʒˈɑrʲ]] лук с томатами и ложкой томатной пасты.' },
    { at: 17.15, text: 'Добавь заправку, ткемали и восемьдесят граммов промытого риса.' },
    { at: 20.68, text: 'Вари до готовности риса.' },
    { at: 22.35, text: 'Затем чеснок, хмели-сунели и грецкие орехи по вкусу.' },
    { at: 25.62, text: 'Добавь кинзу.', tts: 'Добавь [[kʲinzˈu]].' },
    { at: 26.6, text: 'Дай супу настояться десять минут.' },
    { at: 28.55, text: 'Приятного аппетита!' },
  ];

  const SFX = [
    { type: 'boil', start: 0, end: 3.15, level: 0.35 },
    { type: 'whoosh', at: 1.9 }, { type: 'whoosh', at: 3.0 }, { type: 'whoosh', at: 7.0 },
    { type: 'whoosh', at: 12.0 }, { type: 'whoosh', at: 17.0 }, { type: 'whoosh', at: 22.0 },
    { type: 'whoosh', at: 26.0 },
    { type: 'plop', at: 7.33 }, { type: 'plop', at: 7.5 }, { type: 'plop', at: 7.66 }, { type: 'plop', at: 7.8 },
    { type: 'boil', start: 7.4, end: 12.2, level: 0.55 },
    { type: 'sizzle', start: 12.15, end: 17.15, level: 0.6 },
    { type: 'pour', start: 17.25, end: 18.5, level: 0.45 },
    { type: 'sprinkle', start: 18.85, end: 19.8, level: 0.35 },
    { type: 'boil', start: 17.0, end: 26.2, level: 0.45 },
    { type: 'sprinkle', start: 22.85, end: 23.7, level: 0.25 },
    { type: 'pour', start: 26.1, end: 27.45, level: 0.7 },
  ];

  const TIMELINE = { W, H, FPS, DURATION, SCENES, TRANSITION, CAPTIONS, CARDS, VOICE, SFX };

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = TIMELINE;
  }
  if (typeof document === 'undefined') return;

  // ---------------------------------------------------------------- утилиты
  const TAU = Math.PI * 2;
  const clamp = (x, a = 0, b = 1) => (x < a ? a : x > b ? b : x);
  const lerp = (a, b, t) => a + (b - a) * t;
  const inv = (a, b, x) => clamp((x - a) / (b - a));
  const E = {
    inOut: (t) => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2),
    out: (t) => 1 - Math.pow(1 - t, 3),
    in: (t) => t * t * t,
    in2: (t) => t * t,
    sine: (t) => -(Math.cos(Math.PI * t) - 1) / 2,
    outBack: (t) => { const c1 = 1.5, c3 = c1 + 1; return 1 + c3 * Math.pow(t - 1, 3) + c1 * Math.pow(t - 1, 2); },
  };
  const ramp = (t, a, b, e = E.inOut) => e(inv(a, b, t));
  const bell = (t, a, b) => { const p = inv(a, b, t); return Math.sin(Math.PI * p); };

  function hash(n) { const x = Math.sin(n * 127.1 + 311.7) * 43758.5453; return x - Math.floor(x); }
  const R = (i, k) => hash(i * 17.31 + k * 3.77 + 0.123);

  const mix = (c1, c2, t) => [lerp(c1[0], c2[0], t), lerp(c1[1], c2[1], t), lerp(c1[2], c2[2], t)];
  const rgba = (c, a = 1) => `rgba(${c[0] | 0},${c[1] | 0},${c[2] | 0},${a})`;
  const shade = (c, k) => [c[0] * k, c[1] * k, c[2] * k];

  function makeCanvas(w, h) {
    const c = document.createElement('canvas');
    c.width = Math.ceil(w); c.height = Math.ceil(h);
    return c;
  }
  function sprite(w, h, fn) {
    const c = makeCanvas(w, h);
    fn(c.getContext('2d'), w, h);
    return c;
  }

  // Значения шума на решётке 256×256 — быстрый value noise для текстур.
  const LAT = new Float32Array(256 * 256);
  for (let i = 0; i < LAT.length; i++) LAT[i] = hash(i * 0.731 + 7.1);
  function noise(x, y) {
    const xi = Math.floor(x), yi = Math.floor(y);
    const xf = x - xi, yf = y - yi;
    const u = xf * xf * (3 - 2 * xf), v = yf * yf * (3 - 2 * yf);
    const x0 = xi & 255, y0 = yi & 255, x1 = (x0 + 1) & 255, y1 = (y0 + 1) & 255;
    const a = LAT[y0 * 256 + x0], b = LAT[y0 * 256 + x1], c = LAT[y1 * 256 + x0], d = LAT[y1 * 256 + x1];
    return a + (b - a) * u + (c - a) * v + (a - b - c + d) * u * v;
  }
  function fbm(x, y, oct = 4) {
    let s = 0, a = 0.5, f = 1, n = 0;
    for (let i = 0; i < oct; i++) { s += a * noise(x * f, y * f); n += a; a *= 0.5; f *= 2.03; }
    return s / n;
  }

  function blobPath(g, cx, cy, r, seed, k = 0.16, sy = 1) {
    g.beginPath();
    for (let a = 0; a <= 72; a++) {
      const th = (a / 72) * TAU;
      const rr = r * (1 + k * Math.sin(3 * th + seed * 5.1) + k * 0.6 * Math.sin(5 * th + seed * 9.3) +
        k * 0.35 * Math.sin(8 * th + seed * 2.7));
      const x = cx + Math.cos(th) * rr, y = cy + Math.sin(th) * rr * sy;
      if (a) g.lineTo(x, y); else g.moveTo(x, y);
    }
    g.closePath();
  }
  function roundRect(g, x, y, w, h, r) {
    g.beginPath();
    g.moveTo(x + r, y);
    g.arcTo(x + w, y, x + w, y + h, r);
    g.arcTo(x + w, y + h, x, y + h, r);
    g.arcTo(x, y + h, x, y, r);
    g.arcTo(x, y, x + w, y, r);
    g.closePath();
  }
  function radial(g, x, y, r0, r1, stops) {
    const gr = g.createRadialGradient(x, y, r0, x, y, r1);
    for (const [o, c] of stops) gr.addColorStop(o, c);
    return gr;
  }
  function linear(g, x0, y0, x1, y1, stops) {
    const gr = g.createLinearGradient(x0, y0, x1, y1);
    for (const [o, c] of stops) gr.addColorStop(o, c);
    return gr;
  }
  function drawSprite(g, spr, x, y, s, rot = 0, alpha = 1, sy = 1) {
    if (alpha <= 0.003 || s <= 0) return;
    g.save();
    g.translate(x, y);
    if (rot) g.rotate(rot);
    g.scale(s, s * sy);
    g.globalAlpha *= alpha;
    g.drawImage(spr, -spr.width / 2, -spr.height / 2);
    g.restore();
  }

  // ---------------------------------------------------------------- ассеты
  let A = null; // всё, что создаётся один раз

  function buildWood(w, h) {
    const c = makeCanvas(w, h), g = c.getContext('2d');
    const img = g.createImageData(w, h), d = img.data;
    const plankH = 210;
    for (let y = 0; y < h; y++) {
      const p = Math.floor(y / plankH), yy = y % plankH;
      const tone = 0.82 + R(p, 1) * 0.32, off = R(p, 2) * 900;
      let edge = 1;
      if (yy < 5) edge = 0.3 + yy * 0.13; else if (yy > plankH - 4) edge = 0.55 + (plankH - yy) * 0.08;
      if (yy >= 5 && yy < 9) edge = 1.12;
      for (let x = 0; x < w; x++) {
        const n = fbm((x + off) * 0.0035, y * 0.028 + p * 7, 3);
        const n2 = noise((x + off) * 0.0009, y * 0.006 + p);
        let ring = Math.sin(y * 0.12 + n * 16 + n2 * 26) * 0.5 + 0.5;
        ring = ring * ring * ring;
        const fine = (LAT[(x * 7 + y * 131) & 65535] - 0.5) * 0.07;
        let v = (0.62 + (n - 0.5) * 0.55 - ring * 0.22 + (n2 - 0.5) * 0.3 + fine) * tone * edge;
        const i = (y * w + x) * 4;
        d[i] = 116 * v + 14; d[i + 1] = 72 * v + 8; d[i + 2] = 42 * v + 5; d[i + 3] = 255;
      }
    }
    g.putImageData(img, 0, 0);
    return c;
  }

  function buildKitchen() {
    // Тёмный тёплый фон кухни с размытыми огнями — «боке».
    return sprite(W, H, (g) => {
      g.fillStyle = linear(g, 0, 0, 0, H, [[0, '#1a0e08'], [0.6, '#24140b'], [1, '#120a06']]);
      g.fillRect(0, 0, W, H);
      g.filter = 'blur(26px)';
      for (let i = 0; i < 46; i++) {
        const x = R(i, 1) * W, y = R(i, 2) * H * 0.75, r = 30 + R(i, 3) * 110;
        const warm = R(i, 4);
        const col = warm > 0.3 ? [255, 150 + warm * 80, 70] : [255, 220, 170];
        g.fillStyle = rgba(col, 0.08 + R(i, 5) * 0.16);
        g.beginPath(); g.arc(x, y, r, 0, TAU); g.fill();
      }
      g.filter = 'blur(60px)';
      g.fillStyle = 'rgba(255,170,90,0.20)';
      g.beginPath(); g.ellipse(W * 0.22, H * 0.25, 420, 260, 0, 0, TAU); g.fill();
      g.filter = 'none';
    });
  }

  function buildSteam(seed) {
    return sprite(256, 256, (g) => {
      for (let i = 0; i < 14; i++) {
        const x = 128 + (R(seed * 31 + i, 1) - 0.5) * 110, y = 128 + (R(seed * 31 + i, 2) - 0.5) * 110;
        const r = 40 + R(seed * 31 + i, 3) * 60;
        g.fillStyle = radial(g, x, y, 0, r, [[0, 'rgba(255,248,240,0.16)'], [1, 'rgba(255,248,240,0)']]);
        g.beginPath(); g.arc(x, y, r, 0, TAU); g.fill();
      }
    });
  }

  function buildSoupTex() {
    const s = 512, c = makeCanvas(s, s), g = c.getContext('2d');
    const img = g.createImageData(s, s), d = img.data;
    for (let y = 0; y < s; y++) for (let x = 0; x < s; x++) {
      const n = fbm(x * 0.012, y * 0.012, 5);
      const n2 = fbm(x * 0.05 + 40, y * 0.05, 2);
      const v = clamp(0.5 + (n - 0.5) * 1.6 + (n2 - 0.5) * 0.4);
      const i = (y * s + x) * 4;
      d[i] = d[i + 1] = d[i + 2] = v * 255; d[i + 3] = 255;
    }
    g.putImageData(img, 0, 0);
    return c;
  }

  function buildGrain(seed) {
    const s = 384, c = makeCanvas(s, s), g = c.getContext('2d');
    const img = g.createImageData(s, s), d = img.data;
    for (let i = 0; i < s * s; i++) {
      const v = hash(i * 0.917 + seed * 13.1) * 255;
      d[i * 4] = d[i * 4 + 1] = d[i * 4 + 2] = v; d[i * 4 + 3] = 255;
    }
    g.putImageData(img, 0, 0);
    return c;
  }

  function spriteMeat(seed, cooked) {
    return sprite(220, 220, (g) => {
      const cx = 110, cy = 110, r = 66;
      g.save();
      g.shadowColor = 'rgba(0,0,0,0.45)'; g.shadowBlur = 14; g.shadowOffsetY = 8;
      blobPath(g, cx, cy, r, seed, 0.17, 0.86);
      g.fillStyle = cooked ? '#5a2c18' : '#8a1f28';
      g.fill();
      g.restore();
      g.save();
      blobPath(g, cx, cy, r, seed, 0.17, 0.86); g.clip();
      g.fillStyle = radial(g, cx - 22, cy - 26, 4, r * 1.3, cooked
        ? [[0, '#b97a52'], [0.35, '#8e4e2e'], [0.75, '#5e2d18'], [1, '#3a190b']]
        : [[0, '#e8606a'], [0.4, '#b9303a'], [0.8, '#861d27'], [1, '#5a0f17']]);
      g.fillRect(0, 0, 220, 220);
      const ang = seed * 3.1;
      g.lineCap = 'round';
      for (let i = 0; i < 22; i++) {
        const o = (i / 22 - 0.5) * 2 * r * 1.1;
        const ca = Math.cos(ang), sa = Math.sin(ang);
        const x0 = cx + ca * -r * 1.2 - sa * o, y0 = cy + sa * -r * 1.2 + ca * o;
        const x1 = cx + ca * r * 1.2 - sa * o, y1 = cy + sa * r * 1.2 + ca * o;
        g.strokeStyle = i % 2 ? (cooked ? 'rgba(40,14,4,0.28)' : 'rgba(80,8,14,0.3)')
          : (cooked ? 'rgba(220,160,115,0.16)' : 'rgba(255,170,170,0.16)');
        g.lineWidth = 2 + R(seed + i, 1) * 3;
        g.beginPath(); g.moveTo(x0, y0);
        g.quadraticCurveTo(cx - sa * (o + 12 * Math.sin(i + seed)), cy + ca * (o + 12 * Math.sin(i)), x1, y1);
        g.stroke();
      }
      // прожилка/плёнка
      g.strokeStyle = cooked ? 'rgba(238,210,170,0.35)' : 'rgba(255,236,226,0.6)';
      g.lineWidth = 7;
      g.beginPath(); g.moveTo(cx - r, cy + r * 0.2 * Math.sin(seed));
      g.bezierCurveTo(cx - 20, cy - 30, cx + 20, cy + 40, cx + r, cy - 10);
      g.stroke();
      // внутренняя тень и блик
      g.strokeStyle = 'rgba(0,0,0,0.35)'; g.lineWidth = 16;
      blobPath(g, cx, cy + 6, r, seed, 0.17, 0.86); g.stroke();
      g.fillStyle = radial(g, cx - 24, cy - 28, 0, 36, [[0, 'rgba(255,235,210,0.45)'], [1, 'rgba(255,235,210,0)']]);
      g.fillRect(0, 0, 220, 220);
      g.restore();
    });
  }

  function spriteBeefOnBone() {
    return sprite(560, 420, (g) => {
      const cx = 280, cy = 210;
      g.save();
      g.shadowColor = 'rgba(0,0,0,0.5)'; g.shadowBlur = 30; g.shadowOffsetY = 18;
      blobPath(g, cx, cy, 175, 2.3, 0.09, 0.72);
      g.fillStyle = '#7a1820'; g.fill();
      g.restore();
      g.save();
      blobPath(g, cx, cy, 175, 2.3, 0.09, 0.72); g.clip();
      g.fillStyle = radial(g, cx - 60, cy - 50, 10, 260, [[0, '#ef6b72'], [0.35, '#c4343d'], [0.75, '#8e1f29'], [1, '#5c0e16']]);
      g.fillRect(0, 0, 560, 420);
      // мраморность
      g.lineCap = 'round';
      for (let i = 0; i < 16; i++) {
        g.strokeStyle = `rgba(255,236,226,${0.25 + R(i, 7) * 0.35})`;
        g.lineWidth = 2 + R(i, 8) * 5;
        g.beginPath();
        const x0 = 90 + R(i, 1) * 380, y0 = 80 + R(i, 2) * 260;
        g.moveTo(x0, y0);
        g.bezierCurveTo(x0 + 40, y0 - 30 + R(i, 3) * 60, x0 + 80, y0 + R(i, 4) * 50 - 25, x0 + 60 + R(i, 5) * 80, y0 + R(i, 6) * 40 - 20);
        g.stroke();
      }
      // жировая кромка
      g.strokeStyle = 'rgba(250,236,214,0.9)'; g.lineWidth = 26;
      blobPath(g, cx, cy, 175, 2.3, 0.09, 0.72); g.stroke();
      g.strokeStyle = 'rgba(0,0,0,0.25)'; g.lineWidth = 8;
      blobPath(g, cx, cy, 160, 2.3, 0.09, 0.72); g.stroke();
      g.restore();
      // кость
      const bx = cx + 70, by = cy - 10;
      g.fillStyle = radial(g, bx - 10, by - 10, 4, 62, [[0, '#fbf3e4'], [0.7, '#e6d6ba'], [1, '#b49f7c']]);
      g.beginPath(); g.ellipse(bx, by, 58, 46, 0.3, 0, TAU); g.fill();
      g.fillStyle = radial(g, bx, by, 2, 30, [[0, '#d38968'], [0.7, '#a85b43'], [1, '#7c3d2a']]);
      g.beginPath(); g.ellipse(bx + 4, by + 2, 28, 21, 0.3, 0, TAU); g.fill();
      // блики
      g.globalCompositeOperation = 'screen';
      g.fillStyle = radial(g, cx - 80, cy - 60, 0, 120, [[0, 'rgba(255,220,220,0.35)'], [1, 'rgba(255,220,220,0)']]);
      g.fillRect(0, 0, 560, 420);
    });
  }

  function spriteRice() {
    return sprite(52, 24, (g) => {
      g.fillStyle = linear(g, 0, 2, 0, 22, [[0, '#fffdf6'], [0.5, '#f1ead8'], [1, '#c9bc9f']]);
      g.beginPath(); g.ellipse(26, 12, 22, 8.5, 0, 0, TAU); g.fill();
      g.strokeStyle = 'rgba(90,70,40,0.25)'; g.lineWidth = 1.2; g.stroke();
      g.fillStyle = 'rgba(255,255,255,0.8)';
      g.beginPath(); g.ellipse(20, 8.5, 9, 2.5, 0, 0, TAU); g.fill();
    });
  }

  function spriteLeaf(seed) {
    return sprite(160, 160, (g) => {
      const cx = 80, cy = 96;
      const lobes = [[-0.95, 34], [0, 44], [0.95, 34]];
      g.save();
      g.shadowColor = 'rgba(0,0,0,0.35)'; g.shadowBlur = 8; g.shadowOffsetY = 4;
      for (const [a, len] of lobes) {
        const ang = -Math.PI / 2 + a + (R(seed, a + 2) - 0.5) * 0.3;
        const lx = cx + Math.cos(ang) * len * 0.9, ly = cy + Math.sin(ang) * len * 0.9;
        blobPath(g, lx, ly, len * 0.62, seed + a, 0.1, 1);
        g.fillStyle = radial(g, lx - 6, ly - 8, 2, len, [[0, '#7fd35a'], [0.6, '#48a23a'], [1, '#2a6e25']]);
        g.fill();
      }
      g.restore();
      g.strokeStyle = 'rgba(210,255,190,0.55)'; g.lineWidth = 2.2; g.lineCap = 'round';
      for (const [a, len] of lobes) {
        const ang = -Math.PI / 2 + a;
        g.beginPath(); g.moveTo(cx, cy);
        g.lineTo(cx + Math.cos(ang) * len * 1.2, cy + Math.sin(ang) * len * 1.2); g.stroke();
      }
      g.strokeStyle = '#3f8a2f'; g.lineWidth = 4;
      g.beginPath(); g.moveTo(cx, cy); g.lineTo(cx + 4, cy + 50); g.stroke();
    });
  }

  function spriteTomatoCube(seed) {
    return sprite(80, 80, (g) => {
      g.save();
      g.shadowColor = 'rgba(0,0,0,0.4)'; g.shadowBlur = 6; g.shadowOffsetY = 3;
      g.translate(40, 40); g.rotate(R(seed, 1) * 0.6);
      roundRect(g, -24, -22, 48, 44, 9);
      g.fillStyle = linear(g, -24, -22, 24, 22, [[0, '#ff6a4a'], [0.5, '#d9232b'], [1, '#8c0d15']]);
      g.fill();
      g.restore();
      g.fillStyle = 'rgba(255,190,120,0.55)';
      for (let i = 0; i < 4; i++) {
        g.beginPath(); g.ellipse(30 + R(seed, i + 3) * 22, 32 + R(seed, i + 9) * 18, 4, 2.6, R(seed, i), 0, TAU); g.fill();
      }
      g.fillStyle = 'rgba(255,255,255,0.55)';
      g.beginPath(); g.ellipse(30, 26, 8, 3, -0.4, 0, TAU); g.fill();
    });
  }

  function spriteOnion(seed, fried) {
    return sprite(70, 70, (g) => {
      const a0 = R(seed, 1) * TAU, span = 0.7 + R(seed, 2) * 0.6, rr = 20 + R(seed, 3) * 10;
      g.lineCap = 'round';
      g.strokeStyle = fried ? '#8a4a14' : 'rgba(190,175,140,0.9)';
      g.lineWidth = 13;
      g.beginPath(); g.arc(35, 35, rr, a0, a0 + span); g.stroke();
      g.strokeStyle = fried ? '#d99a3c' : 'rgba(248,243,226,0.95)';
      g.lineWidth = 9;
      g.beginPath(); g.arc(35, 35, rr, a0, a0 + span); g.stroke();
      g.strokeStyle = fried ? 'rgba(255,220,140,0.75)' : 'rgba(255,255,255,0.9)';
      g.lineWidth = 2.5;
      g.beginPath(); g.arc(35, 35, rr - 2.5, a0 + 0.1, a0 + span - 0.1); g.stroke();
    });
  }

  function spriteBit(seed, c1, c2, r) {
    return sprite(r * 3, r * 3, (g) => {
      blobPath(g, r * 1.5, r * 1.5, r, seed, 0.22, 0.8);
      g.fillStyle = radial(g, r * 1.2, r * 1.1, 1, r * 1.4, [[0, c1], [1, c2]]);
      g.fill();
    });
  }

  function spriteFat() {
    return sprite(64, 64, (g) => {
      g.fillStyle = radial(g, 32, 32, 0, 30, [[0, 'rgba(255,196,96,0.15)'], [0.7, 'rgba(255,186,80,0.35)'], [0.9, 'rgba(255,214,140,0.85)'], [1, 'rgba(255,214,140,0)']]);
      g.beginPath(); g.arc(32, 32, 30, 0, TAU); g.fill();
      g.fillStyle = 'rgba(255,255,240,0.9)';
      g.beginPath(); g.ellipse(23, 22, 6, 3.5, -0.6, 0, TAU); g.fill();
    });
  }

  function spriteBubble() {
    return sprite(64, 64, (g) => {
      g.fillStyle = radial(g, 32, 34, 0, 30, [[0, 'rgba(255,230,200,0.05)'], [0.75, 'rgba(255,230,200,0.2)'], [0.93, 'rgba(255,240,220,0.75)'], [1, 'rgba(255,240,220,0)']]);
      g.beginPath(); g.arc(32, 32, 30, 0, TAU); g.fill();
      g.fillStyle = 'rgba(255,255,255,0.95)';
      g.beginPath(); g.ellipse(22, 20, 7, 4, -0.7, 0, TAU); g.fill();
      g.fillStyle = 'rgba(0,0,0,0.18)';
      g.beginPath(); g.ellipse(34, 52, 18, 4, 0, 0, TAU); g.fill();
    });
  }

  function spriteFoam(seed) {
    return sprite(200, 140, (g) => {
      for (let i = 0; i < 60; i++) {
        const a = R(seed * 9 + i, 1) * TAU, rr = Math.sqrt(R(seed * 9 + i, 2)) * 70;
        const x = 100 + Math.cos(a) * rr, y = 70 + Math.sin(a) * rr * 0.6, r = 3 + R(seed * 9 + i, 3) * 8;
        g.fillStyle = radial(g, x - r * 0.3, y - r * 0.3, 0, r, [[0, 'rgba(236,224,200,0.8)'], [0.8, 'rgba(184,166,136,0.6)'], [1, 'rgba(150,128,96,0)']]);
        g.beginPath(); g.arc(x, y, r, 0, TAU); g.fill();
      }
    });
  }

  function spriteSphere(size, stops, hl = 0.7) {
    return sprite(size, size, (g) => {
      const c = size / 2, r = size * 0.44;
      g.save();
      g.shadowColor = 'rgba(0,0,0,0.5)'; g.shadowBlur = size * 0.08; g.shadowOffsetY = size * 0.05;
      g.fillStyle = radial(g, c - r * 0.35, c - r * 0.4, r * 0.05, r * 1.2, stops);
      g.beginPath(); g.arc(c, c, r, 0, TAU); g.fill();
      g.restore();
      g.fillStyle = `rgba(255,255,255,${hl})`;
      g.beginPath(); g.ellipse(c - r * 0.38, c - r * 0.45, r * 0.2, r * 0.11, -0.6, 0, TAU); g.fill();
    });
  }

  function spriteTomato() {
    const base = spriteSphere(220, [[0, '#ff8a66'], [0.3, '#ec2f2c'], [0.75, '#a8101a'], [1, '#55060c']]);
    const g = base.getContext('2d');
    g.fillStyle = '#3c7d2a';
    g.save(); g.translate(110, 104);
    for (let i = 0; i < 5; i++) {
      g.rotate(TAU / 5);
      g.beginPath(); g.moveTo(0, 0); g.quadraticCurveTo(10, -8, 30, -2); g.quadraticCurveTo(10, 6, 0, 0); g.fill();
    }
    g.fillStyle = '#2d5e1f'; g.beginPath(); g.arc(0, 0, 7, 0, TAU); g.fill();
    g.restore();
    return base;
  }

  function spriteOnionWhole() {
    const base = spriteSphere(220, [[0, '#f2be78'], [0.35, '#cf8640'], [0.8, '#8a4a1c'], [1, '#4a240c']], 0.45);
    const g = base.getContext('2d');
    g.strokeStyle = 'rgba(90,40,10,0.35)'; g.lineWidth = 2;
    for (let i = -3; i <= 3; i++) {
      g.beginPath(); g.ellipse(110, 110, Math.abs(i) * 14 + 4, 92, 0, -Math.PI / 2, Math.PI / 2, i < 0); g.stroke();
    }
    g.fillStyle = '#7a4a1f';
    g.beginPath(); g.moveTo(102, 28); g.quadraticCurveTo(110, 4, 122, 26); g.fill();
    return base;
  }

  function spriteGarlic() {
    return sprite(220, 220, (g) => {
      g.save();
      g.shadowColor = 'rgba(0,0,0,0.45)'; g.shadowBlur = 16; g.shadowOffsetY = 10;
      for (let i = 0; i < 7; i++) {
        const a = (i / 7) * TAU;
        g.fillStyle = radial(g, 110 + Math.cos(a) * 30 - 10, 110 + Math.sin(a) * 30 - 10, 2, 60,
          [[0, '#fffaf2'], [0.6, '#efe2d4'], [1, '#c9aba0']]);
        g.beginPath(); g.ellipse(110 + Math.cos(a) * 34, 110 + Math.sin(a) * 34, 46, 34, a, 0, TAU); g.fill();
      }
      g.restore();
      g.strokeStyle = 'rgba(170,120,120,0.45)'; g.lineWidth = 2;
      for (let i = 0; i < 7; i++) {
        const a = (i / 7) * TAU + 0.45;
        g.beginPath(); g.moveTo(110, 110); g.lineTo(110 + Math.cos(a) * 76, 110 + Math.sin(a) * 76); g.stroke();
      }
      g.fillStyle = '#b89a80'; g.beginPath(); g.arc(110, 110, 10, 0, TAU); g.fill();
    });
  }

  function spriteClove(seed) {
    return sprite(100, 100, (g) => {
      g.save(); g.translate(50, 50); g.rotate(R(seed, 1) * TAU);
      g.shadowColor = 'rgba(0,0,0,0.4)'; g.shadowBlur = 8; g.shadowOffsetY = 5;
      g.beginPath(); g.moveTo(-34, 6);
      g.quadraticCurveTo(-10, -30, 34, -4); g.quadraticCurveTo(10, 22, -34, 6);
      g.fillStyle = linear(g, 0, -20, 0, 20, [[0, '#fffaf0'], [1, '#d9c3b0']]);
      g.fill();
      g.restore();
    });
  }

  function spriteWalnut(seed) {
    return sprite(120, 120, (g) => {
      g.save();
      g.shadowColor = 'rgba(0,0,0,0.45)'; g.shadowBlur = 10; g.shadowOffsetY = 6;
      blobPath(g, 60, 60, 40, seed, 0.12, 0.85);
      g.fillStyle = radial(g, 48, 46, 4, 56, [[0, '#d39a5c'], [0.6, '#9a6232'], [1, '#5a3416']]);
      g.fill();
      g.restore();
      g.strokeStyle = 'rgba(60,30,10,0.55)'; g.lineWidth = 3; g.lineCap = 'round';
      for (let i = 0; i < 9; i++) {
        g.beginPath();
        const x = 34 + R(seed + i, 1) * 52, y = 36 + R(seed + i, 2) * 48;
        g.moveTo(x, y); g.quadraticCurveTo(x + 10, y - 8, x + 4 + R(seed, i) * 14, y + 10); g.stroke();
      }
      g.strokeStyle = 'rgba(60,30,10,0.7)'; g.lineWidth = 4;
      g.beginPath(); g.moveTo(60, 24); g.lineTo(60, 96); g.stroke();
    });
  }

  function spriteShadow() {
    return sprite(256, 128, (g) => {
      g.fillStyle = radial(g, 128, 64, 0, 128, [[0, 'rgba(0,0,0,0.6)'], [0.6, 'rgba(0,0,0,0.3)'], [1, 'rgba(0,0,0,0)']]);
      g.save(); g.scale(1, 0.5); g.beginPath(); g.arc(128, 128, 128, 0, TAU); g.restore(); g.fill();
    });
  }

  // Содержимое мисок на столе (вид сверху) — заранее нарисованные круги.
  function bowlContent(kind) {
    const s = 360, c = s / 2;
    return sprite(s, s, (g) => {
      g.beginPath(); g.arc(c, c, c, 0, TAU); g.clip();
      if (kind === 'rice') {
        g.fillStyle = '#e9dfc8'; g.fillRect(0, 0, s, s);
        for (let i = 0; i < 520; i++) {
          const a = R(i, 1) * TAU, r = Math.sqrt(R(i, 2)) * c;
          drawSprite(g, A.rice, c + Math.cos(a) * r, c + Math.sin(a) * r, 0.7, R(i, 3) * TAU, 1);
        }
      } else if (kind === 'spice' || kind === 'pepper' || kind === 'salt') {
        const base = kind === 'spice' ? ['#a08c3c', '#6e5a22'] : kind === 'pepper' ? ['#4a4440', '#1f1b18'] : ['#f7f5f0', '#d8d4cc'];
        g.fillStyle = radial(g, c - 30, c - 30, 10, c, [[0, base[0]], [1, base[1]]]);
        g.fillRect(0, 0, s, s);
        const specks = kind === 'spice' ? ['#c9a942', '#5d7a2a', '#8a4b1e', '#e0c060', '#3f5a1c']
          : kind === 'pepper' ? ['#7a726a', '#0e0c0b', '#5a5048'] : ['#ffffff', '#e8e4dc', '#ccc6bc'];
        for (let i = 0; i < 1600; i++) {
          const a = R(i, 4) * TAU, r = Math.sqrt(R(i, 5)) * c;
          g.fillStyle = specks[i % specks.length];
          const z = kind === 'salt' ? 3.2 : 2.2;
          g.fillRect(c + Math.cos(a) * r, c + Math.sin(a) * r, z * (0.5 + R(i, 6)), z * (0.5 + R(i, 7)));
        }
        g.fillStyle = radial(g, c, c, c * 0.6, c, [[0, 'rgba(0,0,0,0)'], [1, 'rgba(0,0,0,0.35)']]);
        g.fillRect(0, 0, s, s);
      } else if (kind === 'tkemali' || kind === 'paste' || kind === 'oil' || kind === 'water') {
        const pal = {
          tkemali: [[0, '#a8323a'], [0.6, '#6e0f1c'], [1, '#3a0610']],
          paste: [[0, '#e0402a'], [0.6, '#a8141a'], [1, '#5a080c']],
          oil: [[0, '#ffe08a'], [0.6, '#d8a030'], [1, '#8a5a10']],
          water: [[0, 'rgba(200,225,235,0.55)'], [0.7, 'rgba(120,160,180,0.45)'], [1, 'rgba(60,90,110,0.6)']],
        }[kind];
        g.fillStyle = radial(g, c - 40, c - 40, 10, c * 1.1, pal);
        g.fillRect(0, 0, s, s);
        g.strokeStyle = kind === 'water' ? 'rgba(255,255,255,0.35)' : 'rgba(255,255,255,0.18)';
        g.lineWidth = 6;
        for (let i = 0; i < 3; i++) {
          g.beginPath(); g.arc(c + 10, c + 10, 50 + i * 38, -2.2 + i * 0.4, -0.9 + i * 0.5); g.stroke();
        }
        g.fillStyle = 'rgba(255,255,255,0.75)';
        g.beginPath(); g.ellipse(c - 60, c - 70, 34, 12, -0.7, 0, TAU); g.fill();
      } else if (kind === 'walnuts') {
        g.fillStyle = '#3a2412'; g.fillRect(0, 0, s, s);
        for (let i = 0; i < 16; i++) {
          const a = R(i, 1) * TAU, r = Math.sqrt(R(i, 2)) * (c - 40);
          drawSprite(g, A.walnuts[i % 4], c + Math.cos(a) * r, c + Math.sin(a) * r, 1.05, R(i, 3) * TAU, 1);
        }
      } else if (kind === 'tomatocubes') {
        g.fillStyle = '#7a0c12'; g.fillRect(0, 0, s, s);
        for (let i = 0; i < 40; i++) {
          const a = R(i, 1) * TAU, r = Math.sqrt(R(i, 2)) * (c - 20);
          drawSprite(g, A.tomatoCubes[i % 4], c + Math.cos(a) * r, c + Math.sin(a) * r, 1.0, R(i, 3) * TAU, 1);
        }
      }
    });
  }

  function buildAssets() {
    A = {};
    A.wood = buildWood(1600, 1900);
    A.kitchen = buildKitchen();
    A.steam = [0, 1, 2, 3].map(buildSteam);
    A.soupTex = buildSoupTex();
    A.grain = [0, 1, 2, 3].map(buildGrain);
    A.meatCooked = [0, 1, 2, 3, 4, 5].map((i) => spriteMeat(i * 1.37 + 0.4, true));
    A.meatRaw = [0, 1, 2, 3, 4, 5].map((i) => spriteMeat(i * 1.37 + 0.4, false));
    A.beef = spriteBeefOnBone();
    A.rice = spriteRice();
    A.leaves = [0, 1, 2, 3, 4, 5].map((i) => spriteLeaf(i * 2.1 + 1));
    A.tomatoCubes = [0, 1, 2, 3].map((i) => spriteTomatoCube(i + 1));
    A.onionRaw = [0, 1, 2, 3, 4, 5].map((i) => spriteOnion(i * 3.3 + 1, false));
    A.onionFried = [0, 1, 2, 3, 4, 5].map((i) => spriteOnion(i * 3.3 + 1, true));
    A.garlicBits = [0, 1, 2, 3].map((i) => spriteBit(i + 4, '#fff6d8', '#d8c088', 9));
    A.walnutBits = [0, 1, 2, 3].map((i) => spriteBit(i + 9, '#c99058', '#6a3e18', 12));
    A.spiceBits = [0, 1, 2, 3].map((i) => spriteBit(i + 15, ['#d2b04a', '#6e8a30', '#9a5a22', '#e8cf6a'][i], '#5a4a18', 4));
    A.walnuts = [0, 1, 2, 3].map((i) => spriteWalnut(i * 1.9 + 1));
    A.fat = spriteFat();
    A.bubble = spriteBubble();
    A.foam = [0, 1, 2, 3].map((i) => spriteFoam(i + 1));
    A.tomato = spriteTomato();
    A.onion = spriteOnionWhole();
    A.garlic = spriteGarlic();
    A.cloves = [0, 1, 2].map((i) => spriteClove(i + 2));
    A.shadow = spriteShadow();
    A.content = {};
    for (const k of ['rice', 'spice', 'pepper', 'salt', 'tkemali', 'paste', 'oil', 'water', 'walnuts', 'tomatocubes']) {
      A.content[k] = bowlContent(k);
    }
    A.vignette = sprite(W, H, (g) => {
      g.fillStyle = radial(g, W / 2, H / 2, H * 0.45, W * 0.62, [[0, 'rgba(0,0,0,0)'], [0.7, 'rgba(0,0,0,0.28)'], [1, 'rgba(0,0,0,0.62)']]);
      g.fillRect(0, 0, W, H);
    });
  }

  // ---------------------------------------------------------------- камера и фон
  function camera(g, cx, cy, s = 1, rot = 0) {
    g.translate(W / 2, H / 2);
    g.scale(s, s);
    if (rot) g.rotate(rot);
    g.translate(-cx, -cy);
  }

  // Стол в перспективе: дерево сжато по вертикали и темнеет к дальнему краю.
  function drawTable(g, horizon = 120, light = 1) {
    g.drawImage(A.kitchen, -400, -300, W + 800, H + 600);
    g.save();
    g.beginPath(); g.rect(-600, horizon, W + 1200, H + 1200); g.clip();
    g.drawImage(A.wood, -600, horizon, W + 1200, (H + 900));
    g.fillStyle = linear(g, 0, horizon, 0, horizon + 520, [[0, 'rgba(10,5,2,0.9)'], [1, 'rgba(10,5,2,0)']]);
    g.fillRect(-600, horizon, W + 1200, 520);
    g.globalCompositeOperation = 'soft-light';
    g.fillStyle = radial(g, W * 0.4, H * 0.55, 50, W * 0.8, [[0, `rgba(255,190,110,${0.6 * light})`], [1, 'rgba(0,0,0,0.5)']]);
    g.fillRect(-600, horizon, W + 1200, H + 1200);
    g.restore();
  }

  function drawStove(g, cx, groundY, t, glow = 1) {
    g.drawImage(A.kitchen, -400, -300, W + 800, H + 600);
    g.fillStyle = linear(g, 0, groundY - 160, 0, H + 300, [[0, '#0b0807'], [0.3, '#16110d'], [1, '#050403']]);
    g.fillRect(-600, groundY - 160, W + 1200, H + 800);
    g.fillStyle = linear(g, 0, groundY - 170, 0, groundY - 150, [[0, 'rgba(255,200,140,0)'], [0.5, 'rgba(255,200,140,0.25)'], [1, 'rgba(255,200,140,0)']]);
    g.fillRect(-600, groundY - 170, W + 1200, 20);
    g.globalCompositeOperation = 'lighter';
    g.fillStyle = radial(g, cx, groundY, 20, 620, [[0, `rgba(255,120,40,${0.32 * glow})`], [0.5, `rgba(120,60,200,${0.05 * glow})`], [1, 'rgba(0,0,0,0)']]);
    g.fillRect(cx - 700, groundY - 600, 1400, 1200);
    g.globalCompositeOperation = 'source-over';
  }

  function drawFlames(g, cx, cy, rx, ry, t, k = 1) {
    g.save();
    g.globalCompositeOperation = 'lighter';
    for (let i = 0; i < 26; i++) {
      const a = (i / 26) * TAU;
      if (Math.sin(a) < -0.1) continue;
      const x = cx + Math.cos(a) * rx, y = cy + Math.sin(a) * ry;
      const h = (26 + 14 * noise(i * 3.1, t * 9)) * k;
      g.fillStyle = linear(g, x, y, x, y - h, [[0, 'rgba(60,120,255,0.85)'], [0.6, 'rgba(120,180,255,0.5)'], [1, 'rgba(180,220,255,0)']]);
      g.beginPath();
      g.moveTo(x - 7, y);
      g.quadraticCurveTo(x - 4, y - h * 0.6, x + Math.sin(t * 7 + i) * 3, y - h);
      g.quadraticCurveTo(x + 4, y - h * 0.6, x + 7, y);
      g.fill();
    }
    g.restore();
  }

  // ---------------------------------------------------------------- частицы
  // Пар: каждый клуб — функция времени, без состояния.
  function drawSteam(g, x, y, w, h, t, k = 1, seed = 0, n = 26, size = 1) {
    if (k <= 0.01) return;
    g.save();
    g.globalCompositeOperation = 'screen';
    for (let i = 0; i < n; i++) {
      const life = 2.2 + R(seed + i, 1) * 1.6;
      const ph = ((t + R(seed + i, 2) * life) / life) % 1;
      const x0 = x + (R(seed + i, 3) - 0.5) * w;
      const sway = Math.sin(ph * 3.2 + R(seed + i, 4) * TAU + t * 0.8) * w * 0.16 * (0.3 + ph);
      const px = x0 + sway + ph * w * 0.08, py = y - ph * h;
      const s = (0.7 + R(seed + i, 5) * 0.8) * (0.5 + ph * 1.8) * size;
      const a = k * Math.pow(Math.sin(Math.PI * ph), 1.3) * 0.36;
      drawSprite(g, A.steam[i % 4], px, py, s, ph * 1.4 + i, a);
    }
    g.restore();
  }

  // Брызги: капли летят по параболе из точки удара.
  function drawSplash(g, x, y, t0, t, n, seed, scale = 1, color = [255, 238, 210]) {
    const dt = t - t0;
    if (dt < 0 || dt > 0.9) return;
    g.save();
    for (let i = 0; i < n; i++) {
      const a = -Math.PI / 2 + (R(seed + i, 1) - 0.5) * 2.4;
      const v = (260 + R(seed + i, 2) * 380) * scale;
      const px = x + Math.cos(a) * v * dt, py = y + Math.sin(a) * v * dt + 900 * scale * dt * dt;
      if (py > y + 20 * scale && dt > 0.1) continue;
      const r = (3 + R(seed + i, 3) * 6) * scale;
      g.fillStyle = rgba(color, 0.85 * (1 - dt / 0.9));
      g.beginPath(); g.ellipse(px, py, r, r * 1.3, a, 0, TAU); g.fill();
    }
    g.restore();
  }

  // ---------------------------------------------------------------- поверхность супа
  const PAL = {
    water:   { c: [96, 112, 116], e: [34, 42, 46] },
    broth:   { c: [222, 166, 78], e: [118, 70, 26] },
    kharcho: { c: [206, 84, 34], e: [104, 28, 12] },
  };

  function soupColors(water, red) {
    const c = mix(mix(PAL.broth.c, PAL.water.c, water), PAL.kharcho.c, red);
    const e = mix(mix(PAL.broth.e, PAL.water.e, water), PAL.kharcho.e, red);
    return { c, e };
  }

  function makeItems(seed, spec, rmax = 0.82) {
    const items = [];
    let k = 0;
    for (const [type, n, s0, s1] of spec) {
      for (let i = 0; i < n; i++, k++) {
        const r = Math.sqrt(R(seed + k, 1)) * rmax, a = R(seed + k, 2) * TAU;
        items.push({
          type, u: Math.cos(a) * r, v: Math.sin(a) * r, s: lerp(s0, s1, R(seed + k, 3)),
          rot: R(seed + k, 4) * TAU, var: Math.floor(R(seed + k, 5) * 6), ph: R(seed + k, 6) * TAU, k,
        });
      }
    }
    return items;
  }

  const ITEM_SPRITE = {
    meat: () => A.meatCooked, meatRaw: () => A.meatRaw, rice: () => [A.rice], leaf: () => A.leaves,
    fat: () => [A.fat], garlic: () => A.garlicBits, walnut: () => A.walnutBits, spice: () => A.spiceBits,
    tomato: () => A.tomatoCubes, onion: () => A.onionFried,
  };

  /*
   * S: { cx, cy, rx, ry, water, red, t, swirl, boil, items, scale (масштаб предметов),
   *      itemAlpha(it) -> 0..1, cook (сырое→варёное мясо), ripples:[{u,v,t0}], extra(g, map) }
   */
  function drawSurface(g, S) {
    const { cx, cy, rx, ry, t } = S;
    const ky = ry / rx;
    const { c, e } = soupColors(S.water || 0, S.red || 0);
    g.save();
    g.beginPath(); g.ellipse(cx, cy, rx, ry, 0, 0, TAU); g.clip();

    // база
    g.fillStyle = radial(g, cx - rx * 0.18, cy - ry * 0.3, 0, rx * 1.15,
      [[0, rgba(shade(c, 1.16))], [0.5, rgba(c)], [1, rgba(e)]]);
    g.fillRect(cx - rx, cy - ry, rx * 2, ry * 2);

    // текстура жидкости, медленно закручивается
    const sw = S.swirl || 0;
    g.save();
    g.translate(cx, cy); g.scale(1, ky); g.rotate(sw * 0.6 + t * 0.03);
    g.globalCompositeOperation = 'overlay';
    g.globalAlpha = S.water ? lerp(0.35, 0.18, S.water) : 0.38;
    g.drawImage(A.soupTex, -rx * 1.1, -rx * 1.1, rx * 2.2, rx * 2.2);
    g.restore();

    const map = (u, v, k = 1) => {
      const a = sw * k;
      const cu = u * Math.cos(a) - v * Math.sin(a), cv = u * Math.sin(a) + v * Math.cos(a);
      return [cx + cu * rx, cy + cv * ry];
    };

    // красное облако заправки (сборка супа)
    if (S.cloud) S.cloud(g, map);

    // предметы, сзади вперёд
    const sc = S.scale || 1;
    const items = S.items || [];
    for (const it of items) {
      const a = S.itemAlpha ? S.itemAlpha(it) : 1;
      if (a <= 0.01) continue;
      const bob = Math.sin(t * 2.2 + it.ph) * 0.006 * (S.boil || 0.3);
      const [x, y] = map(it.u, it.v + bob, it.swirlK != null ? it.swirlK : 1);
      const rot = it.rot + sw * 0.9;
      if (it.type === 'meat' && S.cook != null && S.cook < 1) {
        drawSprite(g, A.meatRaw[it.var], x, y, it.s * sc, rot, a * (1 - S.cook), 0.8);
        drawSprite(g, A.meatCooked[it.var], x, y, it.s * sc, rot, a * S.cook, 0.8);
      } else {
        const arr = ITEM_SPRITE[it.type]();
        const alpha = it.type === 'rice' ? a * 0.9 : it.type === 'fat' ? a * 0.75 : a;
        drawSprite(g, arr[it.var % arr.length], x, y, it.s * sc, rot, alpha, it.type === 'fat' ? ky * 1.2 : 0.82);
      }
    }
    if (S.extra) S.extra(g, map);

    // вода поверх (на этапе чистой воды мясо видно сквозь неё)
    if (S.water > 0.02) {
      g.fillStyle = rgba([120, 140, 146], 0.38 * S.water);
      g.fillRect(cx - rx, cy - ry, rx * 2, ry * 2);
    }

    // пузырьки кипения
    const boil = S.boil || 0;
    if (boil > 0.02) {
      const n = Math.floor(46 * boil);
      for (let i = 0; i < n; i++) {
        const per = 0.55 + R(i, 11) * 0.8;
        const ph = ((t + R(i, 12) * per) / per) % 1;
        const cyc = Math.floor((t + R(i, 12) * per) / per);
        const rr = Math.sqrt(R(i * 7 + cyc, 13)) * 0.88, aa = R(i * 7 + cyc, 14) * TAU;
        const [x, y] = map(Math.cos(aa) * rr, Math.sin(aa) * rr, 0.6);
        const size = (0.25 + R(i, 15) * 0.55) * sc * (S.bubbleScale || 1);
        if (ph < 0.8) {
          drawSprite(g, A.bubble, x, y, size * E.out(ph / 0.8), 0, 0.9, ky * 1.3);
        } else {
          const p = (ph - 0.8) / 0.2;
          g.strokeStyle = `rgba(255,236,210,${0.5 * (1 - p)})`;
          g.lineWidth = 2 * sc;
          g.beginPath(); g.ellipse(x, y, 32 * size * (1 + p * 1.5), 32 * size * ky * 1.3 * (1 + p * 1.5), 0, 0, TAU); g.stroke();
        }
      }
    }

    // круги на воде
    for (const rp of S.ripples || []) {
      const dt = t - rp.t0;
      if (dt < 0 || dt > 1.4) continue;
      const [x, y] = map(rp.u, rp.v, 0);
      for (let j = 0; j < 3; j++) {
        const d = dt - j * 0.15;
        if (d < 0) continue;
        const rad = (40 + d * 260) * (rp.s || 1);
        g.strokeStyle = `rgba(255,240,220,${0.45 * (1 - dt / 1.4)})`;
        g.lineWidth = 3;
        g.beginPath(); g.ellipse(x, y, rad, rad * ky, 0, 0, TAU); g.stroke();
      }
    }

    // блики света на поверхности
    g.globalCompositeOperation = 'screen';
    const jx = Math.sin(t * 3.1) * 6 * boil, jy = Math.cos(t * 2.3) * 3 * boil;
    g.save();
    g.translate(cx - rx * 0.28 + jx, cy - ry * 0.48 + jy); g.scale(1, 0.32);
    g.fillStyle = radial(g, 0, 0, 0, rx * 0.42, [[0, 'rgba(255,240,220,0.42)'], [0.4, 'rgba(255,230,200,0.14)'], [1, 'rgba(255,230,200,0)']]);
    g.fillRect(-rx, -rx, rx * 2, rx * 2);
    g.restore();
    g.save();
    g.translate(cx + rx * 0.32, cy - ry * 0.2); g.rotate(-0.2); g.scale(1, ky);
    g.fillStyle = 'rgba(255,236,210,0.09)';
    roundRect(g, -rx * 0.12, -rx * 0.18, rx * 0.24, rx * 0.36, 12); g.fill();
    g.restore();
    g.globalCompositeOperation = 'source-over';

    // мениск у края
    g.save();
    g.translate(cx, cy); g.scale(1, ky);
    g.fillStyle = radial(g, 0, -rx * 0.04, rx * 0.78, rx, [[0, 'rgba(25,8,0,0)'], [1, 'rgba(25,8,0,0.55)']]);
    g.fillRect(-rx, -rx, rx * 2, rx * 2);
    g.restore();
    g.restore();
    return map;
  }

  // ---------------------------------------------------------------- посуда
  // Глубокая керамическая тарелка, вид 3/4.
  function drawDeepPlate(g, P, S) {
    const { cx, cy, rx } = P, k = P.k || 0.5, ry = rx * k, hb = rx * 0.4;
    drawSprite(g, A.shadow, cx + rx * 0.06, cy + hb * 0.95, rx * 2.3 / 256, 0, 0.9, 1.0);
    // корпус
    g.beginPath();
    g.moveTo(cx - rx, cy);
    g.bezierCurveTo(cx - rx, cy + hb * 0.62, cx - rx * 0.62, cy + hb, cx - rx * 0.4, cy + hb);
    g.lineTo(cx + rx * 0.4, cy + hb);
    g.bezierCurveTo(cx + rx * 0.62, cy + hb, cx + rx, cy + hb * 0.62, cx + rx, cy);
    g.ellipse(cx, cy, rx, ry, 0, 0, Math.PI, false);
    g.closePath();
    g.fillStyle = linear(g, cx - rx, 0, cx + rx, 0, [[0, '#2c1008'], [0.14, '#7e3418'], [0.3, '#c6683a'], [0.38, '#e39a68'], [0.46, '#a84a24'], [0.72, '#5a200e'], [1, '#220b05']]);
    g.fill();
    g.save(); g.clip();
    g.fillStyle = linear(g, 0, cy, 0, cy + hb, [[0, 'rgba(0,0,0,0)'], [1, 'rgba(0,0,0,0.45)']]);
    g.fillRect(cx - rx, cy, rx * 2, hb);
    // орнамент — светлая полоса глазури
    g.strokeStyle = 'rgba(240,210,160,0.55)'; g.lineWidth = rx * 0.018;
    g.beginPath(); g.ellipse(cx, cy + hb * 0.36, rx * 0.93, ry * 0.93, 0, 0.1, Math.PI - 0.1); g.stroke();
    g.strokeStyle = 'rgba(40,12,4,0.5)'; g.lineWidth = rx * 0.01;
    g.beginPath(); g.ellipse(cx, cy + hb * 0.44, rx * 0.88, ry * 0.88, 0, 0.15, Math.PI - 0.15); g.stroke();
    g.restore();
    // край
    g.fillStyle = linear(g, 0, cy - ry, 0, cy + ry, [[0, '#d8c4a2'], [0.5, '#f6ead2'], [1, '#fff8ea']]);
    g.beginPath(); g.ellipse(cx, cy, rx, ry, 0, 0, TAU); g.fill();
    g.strokeStyle = 'rgba(122,46,20,0.85)'; g.lineWidth = rx * 0.012;
    g.beginPath(); g.ellipse(cx, cy, rx * 0.975, ry * 0.975, 0, 0, TAU); g.stroke();
    // внутренняя стенка
    const ix = rx * 0.93, iy = ry * 0.93;
    g.fillStyle = linear(g, 0, cy - iy, 0, cy + iy, [[0, '#f2e6cf'], [0.55, '#c7b18c'], [1, '#8f7656']]);
    g.beginPath(); g.ellipse(cx, cy, ix, iy, 0, 0, TAU); g.fill();
    // суп
    let map = null;
    if (S) {
      const L = clamp(S.level == null ? 1 : S.level);
      const srx = ix * lerp(0.5, 0.965, L);
      const scy = cy + (1 - L) * hb * 0.5 + iy * 0.035;
      g.save();
      g.beginPath(); g.ellipse(cx, cy, ix, iy, 0, 0, TAU); g.clip();
      map = drawSurface(g, Object.assign({}, S, { cx, cy: scy, rx: srx, ry: srx * k }));
      // тень от задней стенки на супе
      g.fillStyle = linear(g, 0, cy - iy, 0, cy - iy * 0.4, [[0, 'rgba(40,20,5,0.35)'], [1, 'rgba(40,20,5,0)']]);
      g.fillRect(cx - ix, cy - iy, ix * 2, iy * 0.6);
      g.restore();
    }
    // блик на переднем крае
    g.strokeStyle = 'rgba(255,255,255,0.65)'; g.lineWidth = rx * 0.008;
    g.beginPath(); g.ellipse(cx, cy, rx * 0.995, ry * 0.995, 0, 0.35, Math.PI - 0.35); g.stroke();
    g.strokeStyle = 'rgba(255,255,255,0.35)';
    g.beginPath(); g.ellipse(cx, cy, ix, iy, 0, Math.PI + 0.5, TAU - 1.2); g.stroke();
    return map;
  }

  // Стальная кастрюля, вид 3/4. depth — насколько уровень ниже края (доля ry).
  function drawPot(g, P, S) {
    const { cx, cy, rx } = P, k = P.k || 0.42, ry = rx * k, h = P.h || rx * 0.75;
    // ручки
    for (const sx of [-1, 1]) {
      g.fillStyle = linear(g, 0, cy + 20, 0, cy + 70, [[0, '#d9dde2'], [0.5, '#7b8088'], [1, '#2a2d31']]);
      roundRect(g, sx < 0 ? cx - rx - 120 : cx + rx - 20, cy + 26, 140, 44, 20); g.fill();
      g.fillStyle = 'rgba(0,0,0,0.6)';
      roundRect(g, sx < 0 ? cx - rx - 92 : cx + rx + 10, cy + 38, 70, 18, 9); g.fill();
    }
    // корпус
    g.beginPath();
    g.moveTo(cx - rx, cy);
    g.lineTo(cx - rx, cy + h);
    g.ellipse(cx, cy + h, rx, ry, 0, Math.PI, 0, true);
    g.lineTo(cx + rx, cy);
    g.ellipse(cx, cy, rx, ry, 0, 0, Math.PI, false);
    g.closePath();
    g.fillStyle = linear(g, cx - rx, 0, cx + rx, 0, [[0, '#1c1e21'], [0.08, '#4a4e54'], [0.2, '#c6ccd4'], [0.27, '#f2f4f6'], [0.33, '#8d939b'], [0.55, '#33363b'], [0.74, '#a6acb4'], [0.82, '#d8dce2'], [0.9, '#5a5f66'], [1, '#141517']]);
    g.fill();
    g.save(); g.clip();
    g.globalCompositeOperation = 'soft-light';
    g.fillStyle = linear(g, 0, cy, 0, cy + h + ry, [[0, 'rgba(255,160,80,0.5)'], [1, 'rgba(255,90,20,0.8)']]);
    g.fillRect(cx - rx, cy, rx * 2, h + ry);
    g.globalCompositeOperation = 'source-over';
    g.fillStyle = linear(g, 0, cy + h - 40, 0, cy + h + ry, [[0, 'rgba(0,0,0,0)'], [1, 'rgba(0,0,0,0.55)']]);
    g.fillRect(cx - rx, cy + h - 40, rx * 2, ry + 40);
    g.restore();
    // кромка
    g.fillStyle = linear(g, cx - rx, 0, cx + rx, 0, [[0, '#5a5e64'], [0.3, '#f0f2f4'], [0.6, '#7e848b'], [1, '#30333a']]);
    g.beginPath(); g.ellipse(cx, cy, rx, ry, 0, 0, TAU); g.fill();
    // внутренняя стенка
    const ix = rx * 0.965, iy = ry * 0.965;
    g.fillStyle = linear(g, 0, cy - iy, 0, cy + iy, [[0, '#2b2c2f'], [0.4, '#6c7076'], [1, '#a9aeb5']]);
    g.beginPath(); g.ellipse(cx, cy, ix, iy, 0, 0, TAU); g.fill();
    let map = null;
    if (S) {
      const depth = S.depth == null ? 0.35 : S.depth;
      g.save();
      g.beginPath(); g.ellipse(cx, cy, ix, iy, 0, 0, TAU); g.clip();
      map = drawSurface(g, Object.assign({}, S, { cx, cy: cy + iy * depth, rx: ix * 0.985, ry: iy * 0.985 }));
      g.fillStyle = linear(g, 0, cy - iy, 0, cy - iy + iy * depth * 2.2, [[0, 'rgba(10,10,12,0.55)'], [1, 'rgba(10,10,12,0)']]);
      g.fillRect(cx - ix, cy - iy, ix * 2, iy * depth * 2.2);
      g.restore();
    }
    g.strokeStyle = 'rgba(255,255,255,0.7)'; g.lineWidth = 3;
    g.beginPath(); g.ellipse(cx, cy, rx * 0.99, ry * 0.99, 0, 0.3, Math.PI - 0.3); g.stroke();
    return map;
  }

  // Чугунная сковорода, вид 3/4, ручка вправо.
  function drawPan(g, P) {
    const { cx, cy, rx } = P, k = P.k || 0.62, ry = rx * k;
    drawSprite(g, A.shadow, cx + 30, cy + ry * 0.35, rx * 2.4 / 256, 0, 0.85, 1.2);
    // ручка
    g.save();
    g.translate(cx + rx * 0.92, cy + 10); g.rotate(0.05);
    g.fillStyle = linear(g, 0, -30, 0, 30, [[0, '#4a4a4c'], [0.4, '#1c1c1e'], [1, '#0a0a0a']]);
    roundRect(g, 0, -30, rx * 1.05, 60, 26); g.fill();
    g.fillStyle = 'rgba(255,255,255,0.12)'; roundRect(g, 20, -24, rx * 0.95, 10, 5); g.fill();
    g.restore();
    // бок
    g.fillStyle = '#121112';
    g.beginPath(); g.ellipse(cx, cy + 26, rx, ry, 0, 0, TAU); g.fill();
    g.fillStyle = linear(g, cx - rx, 0, cx + rx, 0, [[0, '#2a2a2c'], [0.3, '#5a5a5e'], [0.6, '#262628'], [1, '#141415']]);
    g.beginPath(); g.ellipse(cx, cy, rx, ry, 0, 0, TAU); g.fill();
    // дно
    const ix = rx * 0.9, iy = ry * 0.9;
    g.fillStyle = radial(g, cx - ix * 0.2, cy - iy * 0.2, 10, ix * 1.1, [[0, '#3a3836'], [0.6, '#232120'], [1, '#0e0d0d']]);
    g.beginPath(); g.ellipse(cx, cy + 6, ix, iy, 0, 0, TAU); g.fill();
    g.save();
    g.beginPath(); g.ellipse(cx, cy + 6, ix, iy, 0, 0, TAU); g.clip();
    g.globalAlpha = 0.18; g.globalCompositeOperation = 'overlay';
    g.drawImage(A.soupTex, cx - ix, cy - iy, ix * 2, iy * 2);
    g.restore();
    g.strokeStyle = 'rgba(255,255,255,0.22)'; g.lineWidth = 3;
    g.beginPath(); g.ellipse(cx, cy, rx * 0.99, ry * 0.99, 0, Math.PI + 0.3, TAU - 0.3); g.stroke();
    return { cx, cy: cy + 6, rx: ix, ry: iy };
  }

  function drawTopBowl(g, x, y, r, content, rot = 0, glaze = 0) {
    drawSprite(g, A.shadow, x + r * 0.1, y + r * 0.16, r * 2.6 / 256, 0, 0.8, 2.0);
    const pals = [
      [[0, '#f4ead8'], [0.7, '#d9c7a6'], [1, '#9b8462']],
      [[0, '#6a4a3a'], [0.7, '#4a2e22'], [1, '#2a1810']],
      [[0, '#c9783f'], [0.7, '#9a4a22'], [1, '#5a2410']],
    ];
    g.fillStyle = radial(g, x - r * 0.3, y - r * 0.35, r * 0.1, r * 1.15, pals[glaze]);
    g.beginPath(); g.arc(x, y, r, 0, TAU); g.fill();
    g.strokeStyle = 'rgba(255,255,255,0.35)'; g.lineWidth = 3;
    g.beginPath(); g.arc(x, y, r - 2, Math.PI * 1.05, Math.PI * 1.6); g.stroke();
    const ir = r * 0.8;
    g.fillStyle = radial(g, x + ir * 0.2, y + ir * 0.2, ir * 0.2, ir, [[0, 'rgba(0,0,0,0)'], [1, 'rgba(0,0,0,0.5)']]);
    g.beginPath(); g.arc(x, y, ir + 4, 0, TAU); g.fill();
    if (content) {
      g.save();
      g.beginPath(); g.arc(x, y, ir, 0, TAU); g.clip();
      g.translate(x, y); g.rotate(rot);
      g.drawImage(content, -ir, -ir, ir * 2, ir * 2);
      g.restore();
      g.fillStyle = radial(g, x, y, ir * 0.7, ir, [[0, 'rgba(0,0,0,0)'], [1, 'rgba(0,0,0,0.35)']]);
      g.beginPath(); g.arc(x, y, ir, 0, TAU); g.fill();
    }
  }

  function drawKinzaBunch(g, x, y, s, rot, t) {
    g.save();
    g.translate(x, y); g.rotate(rot); g.scale(s, s);
    g.lineCap = 'round';
    for (let i = 0; i < 14; i++) {
      const a = -0.5 + (i / 13) * 1.0 + (R(i, 1) - 0.5) * 0.1;
      const len = 300 + R(i, 2) * 120;
      const ex = Math.sin(a) * len, ey = -Math.cos(a) * len;
      g.strokeStyle = i % 2 ? '#5e9a3a' : '#4c8a2e'; g.lineWidth = 6;
      g.beginPath(); g.moveTo(0, 120); g.quadraticCurveTo(ex * 0.3, ey * 0.5, ex, ey); g.stroke();
      for (let j = 0; j < 3; j++) {
        const lx = ex + (R(i, j + 3) - 0.5) * 70, ly = ey + (R(i, j + 6) - 0.5) * 60;
        drawSprite(g, A.leaves[(i + j) % 6], lx, ly, 0.9 + R(i, j) * 0.4, R(i, j + 9) * TAU + Math.sin(t * 2 + i) * 0.03, 1);
      }
    }
    g.fillStyle = '#c8a46a';
    roundRect(g, -40, 100, 80, 34, 10); g.fill();
    g.restore();
  }

  function drawSpoon(g, x, y, rot, s, fill) {
    g.save();
    g.translate(x, y); g.rotate(rot); g.scale(s, s);
    g.fillStyle = linear(g, 0, -12, 0, 12, [[0, '#d8a874'], [1, '#8a5a30']]);
    roundRect(g, 60, -12, 520, 24, 12); g.fill();
    g.fillStyle = radial(g, -10, -10, 4, 90, [[0, '#e8bc88'], [0.7, '#b07a44'], [1, '#6a4420']]);
    g.beginPath(); g.ellipse(0, 0, 90, 60, 0, 0, TAU); g.fill();
    if (fill) {
      g.fillStyle = fill;
      g.beginPath(); g.ellipse(0, 4, 70, 44, 0, 0, TAU); g.fill();
      g.fillStyle = 'rgba(255,255,255,0.35)';
      g.beginPath(); g.ellipse(-20, -10, 24, 8, -0.3, 0, TAU); g.fill();
    }
    g.restore();
  }

  function drawLadle(g, x, y, rot, s, fillColor, tilt = 0) {
    g.save();
    g.translate(x, y); g.scale(s, s);
    g.save(); g.rotate(rot);
    g.fillStyle = linear(g, 0, -10, 0, 10, [[0, '#f4f6f8'], [0.5, '#9aa0a8'], [1, '#3a3d42']]);
    roundRect(g, 80, -11, 900, 22, 11); g.fill();
    g.restore();
    g.fillStyle = linear(g, -120, 0, 120, 0, [[0, '#2a2c30'], [0.3, '#d8dce2'], [0.6, '#7a8088'], [1, '#22252a']]);
    g.beginPath(); g.ellipse(0, 30, 120, 90, 0, 0, Math.PI); g.lineTo(-120, 0); g.fill();
    g.fillStyle = '#5a5e64';
    g.beginPath(); g.ellipse(0, 0, 120, 44 + tilt * 30, 0, 0, TAU); g.fill();
    if (fillColor) {
      g.fillStyle = fillColor;
      g.beginPath(); g.ellipse(0, 4, 106, 36 + tilt * 26, 0, 0, TAU); g.fill();
      g.fillStyle = 'rgba(255,230,200,0.35)';
      g.beginPath(); g.ellipse(-30, -6, 40, 9, 0, 0, TAU); g.fill();
    }
    g.strokeStyle = 'rgba(255,255,255,0.7)'; g.lineWidth = 4;
    g.beginPath(); g.ellipse(0, 0, 120, 44 + tilt * 30, 0, 0.2, Math.PI - 0.2); g.stroke();
    g.restore();
  }

  // Густая струя (соус, суп) по кривой Безье с бегущими бликами.
  function drawStream(g, x0, y0, cx1, cy1, x1, y1, w0, w1, col, t, alpha = 1) {
    if (alpha <= 0.01) return;
    g.save();
    g.globalAlpha *= alpha;
    const N = 28;
    const pts = [];
    for (let i = 0; i <= N; i++) {
      const u = i / N;
      const x = (1 - u) * (1 - u) * x0 + 2 * (1 - u) * u * cx1 + u * u * x1;
      const y = (1 - u) * (1 - u) * y0 + 2 * (1 - u) * u * cy1 + u * u * y1;
      pts.push([x, y, lerp(w0, w1, u) * (1 + 0.08 * Math.sin(u * 20 - t * 30))]);
    }
    g.fillStyle = linear(g, x0 - w0, 0, x0 + w0, 0, [[0, rgba(shade(col, 0.55))], [0.4, rgba(col)], [1, rgba(shade(col, 0.6))]]);
    g.beginPath();
    for (let i = 0; i <= N; i++) { const [x, y, w] = pts[i]; i ? g.lineTo(x - w / 2, y) : g.moveTo(x - w / 2, y); }
    for (let i = N; i >= 0; i--) { const [x, y, w] = pts[i]; g.lineTo(x + w / 2, y); }
    g.closePath(); g.fill();
    g.strokeStyle = 'rgba(255,240,220,0.55)'; g.lineWidth = 3;
    g.setLineDash([26, 40]); g.lineDashOffset = -t * 900;
    g.beginPath();
    for (let i = 0; i <= N; i++) { const [x, y, w] = pts[i]; i ? g.lineTo(x - w * 0.18, y) : g.moveTo(x - w * 0.18, y); }
    g.stroke();
    g.setLineDash([]);
    g.restore();
  }

  // ---------------------------------------------------------------- сцены
  const BOWL_ITEMS = makeItems(100, [['fat', 34, 0.28, 0.6], ['rice', 70, 0.75, 1.0], ['meat', 9, 0.55, 0.75], ['leaf', 14, 0.45, 0.7]], 0.78);
  BOWL_ITEMS.sort((a, b) => a.v - b.v);

  function sceneHookBowl(g, t) {
    const p = inv(0, 1.9, t);
    const s = lerp(1.0, 1.55, E.in2(p) * 0.6 + p * 0.4);
    camera(g, 960, lerp(560, 600, p), s, lerp(-0.04, 0.0, p));
    drawTable(g, 40, 1);
    const map = drawDeepPlate(g, { cx: 960, cy: 600, rx: 560, k: 0.52 }, {
      t, red: 1, items: BOWL_ITEMS, boil: 0.08, scale: 0.62, swirl: t * 0.04,
    });
    drawSteam(g, 960, 560, 700, 640, t, 1.0, 3, 30, 1.6);
    void map;
  }

  const POT_RED_ITEMS = makeItems(300, [['fat', 26, 0.3, 0.6], ['rice', 60, 0.6, 0.8], ['meat', 8, 0.5, 0.65], ['leaf', 6, 0.4, 0.55]], 0.8);
  POT_RED_ITEMS.sort((a, b) => a.v - b.v);

  function sceneHookPot(g, t) {
    const p = inv(1.9, 3.2, t);
    camera(g, lerp(930, 990, p), lerp(560, 540, p), lerp(1.12, 1.02, E.out(p)), lerp(0.02, 0, p));
    drawStove(g, 960, 1000, t, 1);
    drawFlames(g, 960, 1010, 420, 70, t);
    drawPot(g, { cx: 960, cy: 520, rx: 520, k: 0.42, h: 400 }, {
      t, red: 1, items: POT_RED_ITEMS, boil: 0.7, scale: 0.55, swirl: t * 0.05, depth: 0.32,
    });
    drawSteam(g, 960, 480, 820, 620, t, 1.0, 9, 30, 1.7);
  }

  // Ингредиенты на столе, вид сверху. at — момент появления.
  const ING = [
    { kind: 'beef', x: 560, y: 380, at: 3.05, label: 'Говядина на кости', qty: '600 г', key: true },
    { kind: 'bowl', content: 'rice', x: 1230, y: 360, r: 165, at: 3.2, label: 'Рис', qty: '80 г', key: true },
    { kind: 'onion', x: 1640, y: 290, s: 0.8, at: 3.3 },
    { kind: 'onion', x: 1760, y: 430, s: 0.72, at: 3.38, label: 'Лук', qty: '2 шт.', lx: 1690, ly: 560 },
    { kind: 'tomato', x: 1480, y: 650, s: 0.85, at: 3.48 },
    { kind: 'tomato', x: 1640, y: 740, s: 0.8, at: 3.55, label: 'Помидоры', qty: '2 шт.', lx: 1560, ly: 880 },
    { kind: 'bowl', content: 'paste', x: 1140, y: 720, r: 100, at: 3.62, label: 'Томатная паста', qty: '1 ст. л.', glaze: 1 },
    { kind: 'garlic', x: 300, y: 820, s: 0.95, at: 3.75, label: 'Чеснок', qty: '4 зубчика' },
    { kind: 'bowl', content: 'tkemali', x: 720, y: 900, r: 130, at: 3.88, label: 'Ткемали', qty: '2 ст. л.', key: true, glaze: 2 },
    { kind: 'bowl', content: 'spice', x: 1110, y: 1040, r: 110, at: 4.0, label: 'Хмели-сунели', qty: '1 ч. л.', key: true, glaze: 1 },
    { kind: 'bowl', content: 'pepper', x: 1400, y: 1130, r: 76, at: 4.12, label: 'Перец', glaze: 0 },
    { kind: 'bowl', content: 'salt', x: 1580, y: 1010, r: 76, at: 4.2, label: 'Соль', glaze: 1 },
    { kind: 'bowl', content: 'walnuts', x: 1660, y: 1250, r: 125, at: 4.32, label: 'Грецкие орехи', qty: '50 г', glaze: 0 },
    { kind: 'kinza', x: 880, y: 1420, s: 0.95, rot: -0.9, at: 4.5, label: 'Кинза', qty: 'пучок', lx: 760, ly: 1560 },
    { kind: 'bowl', content: 'oil', x: 1300, y: 1450, r: 95, at: 4.62, label: 'Масло', qty: '1–2 ст. л.', glaze: 0 },
    { kind: 'bowl', content: 'water', x: 400, y: 1290, r: 160, at: 4.72, label: 'Вода', qty: '2 л', glaze: 0, lx: 400, ly: 1150 },
  ];

  function sceneIngredients(g, t) {
    const p = inv(2.8, 7.2, t);
    const cy = lerp(560, 1240, E.sine(p));
    const camS = lerp(1.06, 1.0, p);
    camera(g, lerp(960, 1000, p), cy, camS, lerp(0.015, -0.01, p));
    g.drawImage(A.wood, -200, -200, W + 400, 2100);
    g.globalCompositeOperation = 'soft-light';
    g.fillStyle = radial(g, 700, cy - 100, 100, 1300, [[0, 'rgba(255,200,120,0.75)'], [1, 'rgba(0,0,0,0.6)']]);
    g.fillRect(-200, -200, W + 400, 2100);
    g.globalCompositeOperation = 'source-over';
    // рассыпанные специи и зёрна для живости стола
    for (let i = 0; i < 40; i++) {
      drawSprite(g, A.rice, 1000 + R(i, 1) * 500, 420 + R(i, 2) * 260, 0.6, R(i, 3) * TAU, 0.9);
      drawSprite(g, A.spiceBits[i % 4], 950 + R(i, 4) * 300, 1100 + R(i, 5) * 160, 1.2, 0, 0.8);
    }
    for (const it of ING) {
      const d = inv(it.at, it.at + 0.45, t);
      if (d <= 0) continue;
      const e = E.outBack(d);
      const lift = (1 - E.out(d));
      const s = (it.s || 1) * lerp(1.45, 1, e);
      const alpha = clamp(d * 3);
      g.save();
      g.globalAlpha = alpha;
      g.translate(it.x, it.y);
      g.scale(s / (it.s || 1), s / (it.s || 1));
      g.translate(-it.x, -it.y);
      g.translate(0, -lift * 30);
      drawIngredient(g, it, t);
      g.restore();
    }
    for (const it of ING) {
      if (!it.label) continue;
      const d = inv(it.at + 0.3, it.at + 0.6, t);
      if (d <= 0) continue;
      const lx = it.lx || it.x, ly = it.ly || (it.y + (it.r || (it.kind === 'beef' ? 150 : 90)) + 50);
      const sy = (ly - cy) * camS + H / 2;
      const vis = inv(150, 230, sy) * (1 - inv(H - 70, H - 20, sy));
      drawTag(g, lx, ly, it.label, it.qty, it.key, E.out(d) * vis);
    }
  }

  function drawIngredient(g, it, t) {
    switch (it.kind) {
      case 'beef':
        drawSprite(g, A.shadow, it.x + 20, it.y + 40, 2.4, 0, 0.7, 1.4);
        // доска
        g.fillStyle = linear(g, it.x - 330, 0, it.x + 330, 0, [[0, '#4a2c16'], [0.5, '#6e4426'], [1, '#3e2412']]);
        roundRect(g, it.x - 330, it.y - 210, 660, 420, 40); g.fill();
        g.strokeStyle = 'rgba(255,220,170,0.25)'; g.lineWidth = 4; g.stroke();
        g.drawImage(A.beef, it.x - 280, it.y - 210);
        break;
      case 'bowl':
        drawTopBowl(g, it.x, it.y, it.r, A.content[it.content], 0, it.glaze || 0);
        break;
      case 'onion': drawSprite(g, A.onion, it.x, it.y, it.s, 0.3); break;
      case 'tomato': drawSprite(g, A.tomato, it.x, it.y, it.s, -0.2); break;
      case 'garlic':
        drawSprite(g, A.garlic, it.x, it.y, it.s, 0);
        drawSprite(g, A.cloves[0], it.x + 150, it.y - 40, 0.9, 0.4);
        drawSprite(g, A.cloves[1], it.x + 120, it.y + 70, 0.85, -0.6);
        break;
      case 'kinza': drawKinzaBunch(g, it.x, it.y, it.s, it.rot, t); break;
    }
  }

  function drawTag(g, x, y, label, qty, key, a) {
    g.save();
    g.globalAlpha *= a;
    g.translate(x, y + (1 - a) * 16);
    const f1 = '700 36px Montserrat', f2 = '800 36px Montserrat';
    g.font = f1; const w1 = g.measureText(label).width;
    g.font = f2; const w2 = qty ? g.measureText(qty).width : 0;
    const gap = qty ? 18 : 0, pad = 26, w = w1 + gap + w2 + pad * 2, h = 62;
    g.shadowColor = 'rgba(0,0,0,0.5)'; g.shadowBlur = 18; g.shadowOffsetY = 6;
    g.fillStyle = 'rgba(22,12,6,0.82)';
    roundRect(g, -w / 2, -h / 2, w, h, 31); g.fill();
    g.shadowColor = 'transparent';
    if (key) { g.strokeStyle = '#f4a62a'; g.lineWidth = 3; g.stroke(); }
    g.textBaseline = 'middle';
    g.fillStyle = '#fff6e8'; g.font = f1;
    g.fillText(label, -w / 2 + pad, 2);
    if (qty) { g.fillStyle = '#ffc25e'; g.font = f2; g.fillText(qty, -w / 2 + pad + w1 + gap, 2); }
    g.restore();
  }

  // Варим бульон.
  const BROTH_MEAT = [
    { u: -0.42, v: -0.18, s: 0.95, at: 7.22, var: 0, rot: 0.4 },
    { u: 0.12, v: -0.32, s: 1.0, at: 7.38, var: 1, rot: 1.9 },
    { u: 0.48, v: 0.06, s: 0.9, at: 7.55, var: 2, rot: 3.1 },
    { u: -0.1, v: 0.28, s: 1.05, at: 7.7, var: 3, rot: 5.0 },
    { u: -0.55, v: 0.32, s: 0.85, at: 7.62, var: 4, rot: 2.2 },
    { u: 0.3, v: 0.42, s: 0.9, at: 7.82, var: 5, rot: 0.9 },
  ];
  const FOAM = Array.from({ length: 11 }, (_, i) => {
    const a = R(i, 21) * TAU, r = 0.25 + R(i, 22) * 0.55;
    return { u: Math.cos(a) * r, v: Math.sin(a) * r, s: 0.7 + R(i, 23) * 0.6, var: i % 4, at: 8.3 + R(i, 24) * 0.6 };
  });
  const BROTH_FAT = makeItems(500, [['fat', 20, 0.25, 0.5]], 0.85);

  function sceneBroth(g, t) {
    const P = { cx: 820, cy: 500, rx: 540, k: 0.44, h: 420 };
    const zoom = 1.0 + 0.2 * ramp(t, 7.0, 10.0) - 0.06 * ramp(t, 10.6, 12.2);
    camera(g, lerp(820, 830, ramp(t, 7, 12)), lerp(560, 540, ramp(t, 7, 10)), zoom, lerp(-0.015, 0.012, inv(7, 12, t)));
    const cook = ramp(t, 8.6, 11.6);
    const glow = 0.8 + 0.4 * ramp(t, 7.5, 10);
    drawStove(g, P.cx, 1020, t, glow);
    drawFlames(g, P.cx, 1030, 440, 70, t, 1);
    const water = 1 - ramp(t, 8.2, 11.5);
    const boil = 0.15 + 0.55 * ramp(t, 7.6, 8.8) - 0.25 * ramp(t, 10.6, 11.4);
    const ripples = BROTH_MEAT.map((m) => ({ u: m.u, v: m.v, t0: m.at + 0.22, s: 1 }));
    const surf = { t, water, red: 0, boil, scale: 0.6, cook, swirl: t * 0.03, depth: 0.36, ripples, items: BROTH_FAT,
      itemAlpha: (it) => ramp(t, 9.5, 11.5) * 0.9 };
    // мясо внутри (появляется после удара о воду)
    const skimX = lerp(-1.3, 1.3, ramp(t, 9.55, 10.65, E.sine));
    surf.extra = (gg, map) => {
      for (const m of BROTH_MEAT) {
        const a = clamp((t - m.at - 0.2) * 6);
        if (a <= 0) continue;
        const [x, y] = map(m.u, m.v + Math.sin(t * 2 + m.rot) * 0.01, 1);
        drawSprite(gg, A.meatRaw[m.var], x, y, m.s * 0.95, m.rot + t * 0.03, a * (1 - cook), 0.8);
        drawSprite(gg, A.meatCooked[m.var], x, y, m.s * 0.95, m.rot + t * 0.03, a * cook, 0.8);
      }
      // пена: появляется и снимается шумовкой
      for (const f of FOAM) {
        let a = ramp(t, f.at, f.at + 0.5);
        const passT = lerp(9.55, 10.65, inv(-1.3, 1.3, f.u) * 0.98);
        a *= 1 - ramp(t, passT, passT + 0.18);
        if (a <= 0.01) continue;
        const [x, y] = map(f.u, f.v, 1);
        drawSprite(gg, A.foam[f.var], x, y, f.s * 0.85, f.u, a * 0.85, 1);
      }
    };
    drawPot(g, P, surf);
    // шумовка
    const sk = bell(t, 9.35, 10.9);
    if (sk > 0) {
      const sx = P.cx + skimX * P.rx * 0.62, sy = P.cy + P.rx * P.k * 0.36 + Math.sin(skimX * 2) * 30 - (1 - sk) * 180;
      g.save();
      g.globalAlpha = clamp(sk * 3);
      g.strokeStyle = linear(g, sx, sy, sx + 500, sy - 500, [[0, '#e8ecf0'], [1, '#5a5f66']]);
      g.lineWidth = 22; g.lineCap = 'round';
      g.beginPath(); g.moveTo(sx + 90, sy - 20); g.lineTo(sx + 620, sy - 560); g.stroke();
      g.fillStyle = linear(g, sx - 120, 0, sx + 120, 0, [[0, '#6a7078'], [0.4, '#eef1f4'], [1, '#5a5f66']]);
      g.beginPath(); g.ellipse(sx, sy, 125, 52, 0, 0, TAU); g.fill();
      g.fillStyle = 'rgba(30,30,34,0.75)';
      for (let i = -3; i <= 3; i++) for (let j = -1; j <= 1; j++) {
        if (Math.abs(i) === 3 && j !== 0) continue;
        g.beginPath(); g.ellipse(sx + i * 30, sy + j * 22, 7, 4, 0, 0, TAU); g.fill();
      }
      const collected = ramp(t, 9.6, 10.6);
      for (let i = 0; i < 3; i++) {
        drawSprite(g, A.foam[i], sx - 30 + i * 30, sy - 6, 0.55 * collected, 0, collected);
      }
      g.restore();
    }
    // падающее мясо (над кастрюлей, до удара)
    for (const m of BROTH_MEAT) {
      const dt = t - (m.at - 0.55);
      if (dt < 0 || t > m.at + 0.22) continue;
      const fall = clamp(dt / 0.77);
      const x = P.cx + m.u * P.rx * 0.95, yEnd = P.cy + P.rx * P.k * 0.36 + m.v * P.rx * P.k;
      const y = lerp(-220, yEnd, E.in2(fall));
      drawSprite(g, A.meatRaw[m.var], x, y, m.s * lerp(1.25, 0.95, fall), m.rot + fall * 1.5, 1, 0.85);
    }
    for (const m of BROTH_MEAT) {
      const x = P.cx + m.u * P.rx * 0.95, y = P.cy + P.rx * P.k * 0.36 + m.v * P.rx * P.k;
      drawSplash(g, x, y, m.at + 0.22, t, 14, m.var * 31, 0.8);
    }
    drawSteam(g, P.cx, P.cy + 40, 860, 640, t, 0.25 + 0.75 * ramp(t, 8, 10), 40, 34, 1.7);
    // свет меняется: «время идёт»
    g.save();
    g.globalCompositeOperation = 'soft-light';
    const lx = lerp(200, 1600, ramp(t, 9.2, 12));
    g.fillStyle = radial(g, lx, 200, 50, 1100, [[0, `rgba(255,190,110,${0.25 + 0.35 * ramp(t, 9.2, 10.5)})`], [1, 'rgba(0,0,0,0)']]);
    g.fillRect(-400, -400, W + 800, H + 800);
    g.restore();
  }

  // Заправка на сковороде.
  const ONIONS = makeItems(700, [['onion', 110, 0.65, 0.95]], 0.8).map((o, i) => Object.assign(o, { at: 12.05 + R(i, 31) * 0.5 }));
  const TOMS = makeItems(800, [['tomato', 46, 0.6, 0.85]], 0.72).map((o, i) => Object.assign(o, { at: 13.25 + R(i, 32) * 0.5 }));

  function sceneDressing(g, t) {
    const P = { cx: 800, cy: 560, rx: 560, k: 0.62 };
    const zoom = 1.0 + 0.14 * ramp(t, 12, 14) + 0.3 * ramp(t, 14.4, 15.3) - 0.22 * ramp(t, 15.5, 16.3);
    const focusX = lerp(800, 860, ramp(t, 14.4, 15.3)), focusY = lerp(560, 600, ramp(t, 14.4, 15.3));
    camera(g, focusX, focusY, zoom, lerp(-0.02, 0.03, inv(12, 17, t)));
    drawStove(g, P.cx, 1000, t, 0.9);
    const B = drawPan(g, P);
    const fry = ramp(t, 12.6, 14.6);
    const sauce = ramp(t, 14.6, 15.9);
    const stir = 2.6 * ramp(t, 14.55, 15.75, E.sine);
    g.save();
    g.beginPath(); g.ellipse(B.cx, B.cy, B.rx, B.ry, 0, 0, TAU); g.clip();
    // масло
    g.globalCompositeOperation = 'screen';
    g.fillStyle = radial(g, B.cx - 120, B.cy - 60, 10, B.rx * 0.9, [[0, 'rgba(255,200,90,0.32)'], [1, 'rgba(255,170,60,0)']]);
    g.fillRect(B.cx - B.rx, B.cy - B.ry, B.rx * 2, B.ry * 2);
    g.globalCompositeOperation = 'source-over';
    // соус под кусочками
    if (sauce > 0) {
      g.save();
      g.globalAlpha = sauce;
      g.translate(B.cx, B.cy); g.scale(1, B.ry / B.rx); g.rotate(stir * 0.4);
      g.fillStyle = radial(g, -80, -80, 20, B.rx, [[0, '#d24a24'], [0.6, '#a8301a'], [1, '#5e160a']]);
      g.beginPath(); g.arc(0, 0, B.rx * lerp(0.35, 1, E.out(sauce)), 0, TAU); g.fill();
      g.globalCompositeOperation = 'overlay'; g.globalAlpha = 0.45 * sauce;
      g.drawImage(A.soupTex, -B.rx, -B.rx, B.rx * 2, B.rx * 2);
      g.restore();
    }
    const mp = (u, v, k) => {
      const a = stir * k;
      const cu = u * Math.cos(a) - v * Math.sin(a), cv = u * Math.sin(a) + v * Math.cos(a);
      return [B.cx + cu * B.rx, B.cy + cv * B.ry];
    };
    for (const o of ONIONS) {
      const d = inv(o.at, o.at + 0.3, t);
      if (d <= 0) continue;
      const [x, y] = mp(o.u, o.v, 1 - Math.hypot(o.u, o.v) * 0.5);
      const yy = lerp(y - 380, y, E.in2(d));
      const s = o.s * lerp(1.5, 1, d);
      const jit = Math.sin(t * 40 + o.k) * 1.2 * fry;
      drawSprite(g, A.onionRaw[o.var], x + jit, yy, s, o.rot + stir, 1 - fry);
      drawSprite(g, A.onionFried[o.var], x + jit, yy, s, o.rot + stir, fry);
    }
    for (const o of TOMS) {
      const d = inv(o.at, o.at + 0.3, t);
      if (d <= 0) continue;
      const [x, y] = mp(o.u, o.v, 1 - Math.hypot(o.u, o.v) * 0.5);
      drawSprite(g, A.tomatoCubes[o.var % 4], x, lerp(y - 400, y, E.in2(d)), o.s * lerp(1.6, 1, d) * (1 - 0.35 * sauce), o.rot + stir, 1 - 0.55 * sauce);
    }
    // томатная паста
    const paste = ramp(t, 14.15, 14.6, E.out);
    if (paste > 0) {
      const [x, y] = mp(0.08, -0.06, 0.6);
      g.fillStyle = radial(g, x - 20, y - 20, 4, 120, [[0, '#e8452c'], [0.6, '#b01a1a'], [1, '#6a0a0c']]);
      g.globalAlpha = 1 - 0.8 * sauce;
      blobPath(g, x, y, 80 * paste * (1 + sauce * 0.8), 3.3, 0.12, 0.62); g.fill();
      g.globalAlpha = 1;
    }
    // ткемали
    const tk = ramp(t, 15.95, 16.7, E.out);
    if (tk > 0) {
      const [x, y] = mp(-0.12, 0.12, 0);
      g.fillStyle = radial(g, x - 20, y - 15, 4, 170, [[0, 'rgba(160,40,52,0.95)'], [0.7, 'rgba(110,15,28,0.9)'], [1, 'rgba(70,6,16,0)']]);
      blobPath(g, x, y, 150 * tk, 6.1, 0.1, 0.62); g.fill();
      g.strokeStyle = 'rgba(255,220,220,0.3)'; g.lineWidth = 4;
      g.beginPath(); g.ellipse(x - 20, y - 12, 60 * tk, 22 * tk, -0.2, 3.6, 5.4); g.stroke();
    }
    // шипение — искры масла
    const sizzle = ramp(t, 12.4, 12.9) * (1 - 0.4 * sauce);
    for (let i = 0; i < 60 * sizzle; i++) {
      const per = 0.25 + R(i, 41) * 0.35;
      const ph = ((t + R(i, 42) * per) / per) % 1;
      const cyc = Math.floor((t + R(i, 42) * per) / per);
      const a = R(i * 3 + cyc, 43) * TAU, r = Math.sqrt(R(i * 3 + cyc, 44)) * 0.85;
      const x = B.cx + Math.cos(a) * r * B.rx, y = B.cy + Math.sin(a) * r * B.ry - Math.sin(ph * Math.PI) * 40;
      g.fillStyle = `rgba(255,236,190,${0.9 * (1 - ph)})`;
      g.beginPath(); g.arc(x, y, 2.5 + R(i, 45) * 2.5, 0, TAU); g.fill();
    }
    g.restore();
    // деревянная лопатка
    const sp = bell(t, 14.4, 15.95);
    if (sp > 0) {
      const a = stir * 1.6 + 0.6;
      const x = B.cx + Math.cos(a) * B.rx * 0.42, y = B.cy + Math.sin(a) * B.ry * 0.42 - (1 - E.out(clamp(sp * 2))) * 300;
      g.save();
      g.globalAlpha = clamp(sp * 3);
      g.translate(x, y); g.rotate(-0.75);
      g.fillStyle = linear(g, 0, -20, 0, 20, [[0, '#e2b27c'], [1, '#8e5a2c']]);
      roundRect(g, 40, -14, 640, 28, 14); g.fill();
      roundRect(g, -90, -48, 150, 96, 40); g.fill();
      g.fillStyle = 'rgba(178,40,24,0.75)';
      roundRect(g, -90, -48, 70, 96, 30); g.fill();
      g.restore();
    }
    // ложка с ткемали
    const ts = bell(t, 15.55, 17.1);
    if (ts > 0) {
      const [x, y] = mp(-0.12, 0.12, 0);
      const sx = x + 160, sy = lerp(-120, y - 330, E.out(clamp(ts * 2.2)));
      const pour = ramp(t, 15.9, 16.2) * (1 - ramp(t, 16.55, 16.8));
      drawSpoon(g, sx, sy, -0.5 + pour * 0.5, 1, '#7a1424');
      drawStream(g, sx - 60, sy + 40, sx - 90, sy + 160, x, y - 6, 26, 18, [128, 20, 36], t, pour);
    }
    // ложка томатной пасты
    const ps = bell(t, 13.8, 14.8);
    if (ps > 0) {
      const [x, y] = mp(0.08, -0.06, 0.6);
      drawSpoon(g, x + 120, lerp(-150, y - 160, E.out(clamp(ps * 2))), 0.25 - ramp(t, 14.1, 14.5) * 0.5, 0.95,
        ramp(t, 14.1, 14.45) < 1 ? '#b8231c' : null);
    }
    drawSteam(g, B.cx, B.cy, 900, 560, t, 0.35 + 0.4 * fry, 70, 26, 1.3);
  }

  // Собираем суп.
  const AS_MEAT = makeItems(900, [['meat', 9, 0.55, 0.72]], 0.7);
  const AS_FAT = makeItems(950, [['fat', 26, 0.25, 0.55]], 0.85);
  const AS_RICE = makeItems(1000, [['rice', 140, 0.6, 0.8]], 0.85).map((r, i) => {
    // рис падает в одну точку и расходится от неё
    return Object.assign(r, { u0: 0.35 + (R(i, 51) - 0.5) * 0.2, v0: -0.25 + (R(i, 52) - 0.5) * 0.2, at: 18.85 + R(i, 53) * 0.9 });
  });

  function sceneAssemble(g, t) {
    const P = { cx: 820, cy: 600, rx: 600, k: 0.56, h: 420 };
    camera(g, lerp(820, 860, inv(17, 22, t)), lerp(600, 640, inv(17, 22, t)), lerp(1.0, 1.14, ramp(t, 16.8, 22.2)), lerp(0.02, -0.015, inv(17, 22, t)));
    drawStove(g, P.cx, 1080, t, 1);
    const red = ramp(t, 17.6, 21.6);
    const stir = 2.4 * ramp(t, 19.9, 22.2, E.sine) + t * 0.03;
    const items = [].concat(AS_FAT, AS_MEAT);
    const surf = {
      t, red, boil: 0.45 + 0.25 * ramp(t, 19.9, 21), scale: 0.62, swirl: stir, depth: 0.3, items,
      cloud: (gg, map) => {
        const c = ramp(t, 17.35, 19.5);
        if (c <= 0) return;
        const [x, y] = map(-0.42, -0.25, 1);
        gg.save();
        gg.globalCompositeOperation = 'multiply';
        gg.fillStyle = radial(gg, x, y, 0, 900 * E.out(c), [[0, 'rgba(200,60,30,0.85)'], [0.6, `rgba(200,80,40,${0.4 * (1 - red)})`], [1, 'rgba(255,255,255,0)']]);
        gg.fillRect(x - 1000, y - 800, 2000, 1600);
        gg.restore();
      },
      ripples: [{ u: 0.35, v: -0.25, t0: 19.0, s: 0.8 }, { u: -0.42, v: -0.25, t0: 17.4, s: 1 }],
      extra: (gg, map) => {
        for (const r of AS_RICE) {
          const d = inv(r.at, r.at + 0.25, t);
          if (d <= 0) continue;
          const spread = ramp(t, r.at + 0.1, r.at + 1.6, E.out);
          const [x, y] = map(lerp(r.u0, r.u, spread), lerp(r.v0, r.v, spread), 1);
          drawSprite(gg, A.rice, x, y, r.s * 0.62 * (1 + (1 - d) * 0.5), r.rot + stir, 0.88 * clamp(d * 2), 0.82);
        }
      },
    };
    const map = drawPot(g, P, surf);
    // заправка вливается со сковороды
    const pan = bell(t, 16.9, 18.9);
    if (pan > 0) {
      const [ex, ey] = map(-0.42, -0.25, 0);
      const px = ex - 330, py = lerp(-360, 40, E.out(clamp(pan * 2)));
      const pour = ramp(t, 17.2, 17.45) * (1 - ramp(t, 18.3, 18.6));
      drawStream(g, px + 210, py + 120, px + 330, py + 200, ex, ey, 70, 46, [176, 46, 24], t, pour);
      g.save();
      g.translate(px, py); g.rotate(0.55);
      g.fillStyle = '#141314';
      g.beginPath(); g.ellipse(0, 0, 300, 160, 0, 0, TAU); g.fill();
      g.fillStyle = radial(g, 40, 40, 10, 260, [[0, '#c63a1e'], [1, '#6a160a']]);
      g.beginPath(); g.ellipse(10, 14, 260, 130, 0, 0, TAU); g.fill();
      g.restore();
    }
    // рис из миски
    const rb = bell(t, 18.5, 20.1);
    if (rb > 0) {
      const [ex, ey] = map(0.35, -0.25, 0);
      const bx = ex + 280, by = lerp(-260, -40, E.out(clamp(rb * 2)));
      for (const r of AS_RICE) {
        const dt = t - (r.at - 0.45);
        if (dt < 0 || dt > 0.48) continue;
        const f = dt / 0.45;
        const x = lerp(bx - 140, ex + (R(r.k, 61) - 0.5) * 80, f), y = lerp(by + 60, ey, E.in2(f));
        drawSprite(g, A.rice, x, y, 0.8, r.rot + f * 6, 1);
      }
      g.save();
      g.translate(bx, by); g.rotate(-0.7);
      drawTopBowl(g, 0, 0, 150, A.content.rice, 0, 0);
      g.restore();
    }
    // половник мешает
    const ld = bell(t, 19.7, 22.4);
    if (ld > 0) {
      const a = stir * 1.4 + 1;
      const [x, y] = map(Math.cos(a) * 0.4, Math.sin(a) * 0.4, 0);
      g.save();
      g.globalAlpha = clamp(ld * 3);
      drawLadle(g, x, y - (1 - E.out(clamp(ld * 2))) * 260, -0.75, 0.7, null);
      g.restore();
    }
    drawSteam(g, P.cx, P.cy + 30, 900, 620, t, 0.75, 120, 32, 1.7);
  }

  // Специи и аромат — макро поверхности.
  const SP_ITEMS = makeItems(1100, [['fat', 30, 0.5, 1.0], ['rice', 90, 1.1, 1.5], ['meat', 7, 1.1, 1.4]], 0.92);
  SP_ITEMS.sort((a, b) => a.v - b.v);
  const SP_DROPS = [].concat(
    makeItems(1200, [['walnut', 34, 0.9, 1.4]], 0.55).map((o, i) => Object.assign(o, { at: 22.15 + R(i, 71) * 0.6, u: o.u - 0.25, v: o.v - 0.1 })),
    makeItems(1300, [['spice', 220, 1.2, 2.0]], 0.5).map((o, i) => Object.assign(o, { at: 22.9 + R(i, 72) * 0.75, u: o.u + 0.05, v: o.v })),
    makeItems(1400, [['garlic', 46, 0.9, 1.3]], 0.55).map((o, i) => Object.assign(o, { at: 23.75 + R(i, 73) * 0.6, u: o.u + 0.25, v: o.v + 0.1 })),
    makeItems(1500, [['leaf', 26, 0.7, 1.1]], 0.7).map((o, i) => Object.assign(o, { at: 24.6 + R(i, 74) * 0.8 })),
  );

  function sceneSpices(g, t) {
    const S = { cx: 960, cy: 600, rx: 1250, ry: 800 };
    const rot = lerp(-0.02, 0.07, inv(21.7, 26.3, t));
    camera(g, 960, 560, lerp(1.0, 1.12, ramp(t, 21.7, 26.3)), rot);
    g.fillStyle = '#1a0c06'; g.fillRect(-400, -400, W + 800, H + 800);
    const map = drawSurface(g, {
      cx: S.cx, cy: S.cy, rx: S.rx, ry: S.ry, t, red: 1, boil: 0.35, bubbleScale: 2.2, scale: 1, swirl: t * 0.04,
      items: SP_ITEMS,
      extra: (gg, mp) => {
        for (const o of SP_DROPS) {
          const d = inv(o.at, o.at + (o.type === 'leaf' ? 0.7 : 0.4), t);
          if (d < 1) continue;
          const [x, y] = mp(o.u, o.v, 1);
          const arr = ITEM_SPRITE[o.type]();
          drawSprite(gg, arr[o.var % arr.length], x, y, o.s, o.rot, o.type === 'spice' ? 0.85 : 1, 0.85);
        }
      },
    });
    // падают в кадре — крупнее, ближе к камере
    for (const o of SP_DROPS) {
      const dur = o.type === 'leaf' ? 0.7 : 0.4;
      const d = inv(o.at, o.at + dur, t);
      if (d <= 0 || d >= 1) continue;
      const [x, y] = map(o.u, o.v, 1);
      const fx = o.type === 'leaf' ? Math.sin(d * 7 + o.ph) * 60 : 0;
      const px = x + fx, py = lerp(y - 700, y, o.type === 'leaf' ? E.sine(d) : E.in2(d));
      const arr = ITEM_SPRITE[o.type]();
      const flip = o.type === 'leaf' ? Math.abs(Math.cos(d * 5 + o.ph)) * 0.8 + 0.2 : 1;
      drawSprite(g, arr[o.var % arr.length], px, py, o.s * lerp(2.0, 1, d), o.rot + d * 4, clamp(d * 4), flip);
    }
    // облачко молотых специй
    const puff = bell(t, 22.95, 24.2);
    if (puff > 0) {
      const [x, y] = map(0.05, 0, 1);
      g.save(); g.globalCompositeOperation = 'screen';
      for (let i = 0; i < 10; i++) {
        const a = R(i, 81) * TAU, r = 60 + R(i, 82) * 200 * puff;
        drawSprite(g, A.steam[i % 4], x + Math.cos(a) * r, y - 100 * puff + Math.sin(a) * r * 0.4, 1.4 * puff + 0.5, i, puff * 0.35);
      }
      g.restore();
      g.save(); g.globalCompositeOperation = 'multiply'; g.globalAlpha = puff * 0.35;
      g.fillStyle = radial(g, x, y, 0, 300, [[0, '#c2a040'], [1, 'rgba(255,255,255,0)']]);
      g.fillRect(x - 320, y - 320, 640, 640);
      g.restore();
    }
    // ложка с хмели-сунели крупным планом
    const ss = bell(t, 22.55, 24.0);
    if (ss > 0) {
      const sx = 1180, sy = lerp(-260, 150, E.out(clamp(ss * 2)));
      drawSpoon(g, sx, sy, -0.35 + ramp(t, 22.9, 23.3) * 0.6, 1.6, ramp(t, 22.9, 23.6) < 1 ? '#a88a38' : null);
    }
    // вихрь ароматного пара
    const vort = ramp(t, 23.0, 24.2);
    if (vort > 0) {
      g.save(); g.globalCompositeOperation = 'screen';
      for (let i = 0; i < 46; i++) {
        const life = 2.4;
        const ph = ((t + R(i, 91) * life) / life) % 1;
        const ang = R(i, 92) * TAU + ph * 4.2 + t * 0.6;
        const rad = 120 + ph * 520;
        const x = 960 + Math.cos(ang) * rad, y = 640 + Math.sin(ang) * rad * 0.45 - ph * 520;
        drawSprite(g, A.steam[i % 4], x, y, 0.9 + ph * 1.3, ang, vort * Math.sin(Math.PI * ph) * 0.32);
      }
      g.restore();
    }
    drawSteam(g, 960, 900, 1700, 900, t, 0.3, 150, 20, 1.6);
  }

  // Финальная подача.
  const SERVE_ITEMS = BOWL_ITEMS.filter((it) => it.type !== 'leaf');
  const SERVE_LEAVES = makeItems(1600, [['leaf', 14, 0.5, 0.75]], 0.6).map((o, i) => Object.assign(o, { at: 27.5 + R(i, 99) * 0.5 }));

  function sceneServe(g, t) {
    const P = { cx: 960, cy: 560, rx: 560, k: 0.52 };
    const p = inv(25.8, 30, t);
    camera(g, 960, lerp(560, 690, E.sine(p)), lerp(1.0, 1.32, E.sine(p)), lerp(-0.03, 0.012, E.sine(p)));
    drawTable(g, 40, 1);
    const level = lerp(0.12, 1, ramp(t, 26.1, 27.45, E.out));
    const map = drawDeepPlate(g, P, {
      t, red: 1, level, boil: 0.06, scale: 0.62, swirl: t * 0.04 + (1 - level) * 1.2,
      items: SERVE_ITEMS.concat(SERVE_LEAVES),
      itemAlpha: (it) => it.at ? ramp(t, it.at, it.at + 0.3) : ramp(t, 26.3 + it.k * 0.009, 27.3 + it.k * 0.009),
      ripples: [{ u: -0.05, v: -0.05, t0: 26.2, s: 0.6 }, { u: -0.05, v: -0.05, t0: 26.8, s: 0.6 }],
    });
    // половник наливает суп
    const ld = bell(t, 25.7, 27.95);
    if (ld > 0) {
      const [ex, ey] = map(-0.05, -0.05, 0);
      const lx = ex - 250, ly = lerp(-260, ey - 360, E.out(clamp(ld * 2)));
      const pour = ramp(t, 26.05, 26.25) * (1 - ramp(t, 27.25, 27.5));
      drawStream(g, lx + 80, ly + 30, lx + 160, ly + 120, ex, ey, 60, 40, [196, 70, 30], t, pour);
      drawLadle(g, lx, ly, -2.35, 1.0, rgba([186, 64, 28]), pour);
    }
    drawSteam(g, P.cx, P.cy - 20, 760, 660, t, 0.6 + 0.4 * ramp(t, 26.5, 28), 160, 30, 1.6);
  }

  const DRAW = {
    hookBowl: sceneHookBowl, hookPot: sceneHookPot, ingredients: sceneIngredients, broth: sceneBroth,
    dressing: sceneDressing, assemble: sceneAssemble, spices: sceneSpices, serve: sceneServe,
  };

  // ---------------------------------------------------------------- титры
  function fitFont(g, text, weight, size, family, maxW) {
    let s = size;
    g.font = `${weight} ${s}px ${family}`;
    while (g.measureText(text).width > maxW && s > 20) { s -= 2; g.font = `${weight} ${s}px ${family}`; }
    return s;
  }

  function drawCaption(g, cap, t) {
    const a = ramp(t, cap.start, cap.start + 0.4, E.out) * (1 - ramp(t, cap.end - 0.3, cap.end));
    if (a <= 0.003) return;
    g.save();
    if (cap.style === 'main') {
      const size = fitFont(g, cap.text, 800, 76, 'Montserrat', W - 300);
      const tw = g.measureText(cap.text).width;
      const x = 96, y = H - 112, ph = size + 52, pw = tw + 96;
      const open = ramp(t, cap.start, cap.start + 0.45, E.out);
      g.globalAlpha = a;
      g.shadowColor = 'rgba(0,0,0,0.55)'; g.shadowBlur = 30; g.shadowOffsetY = 10;
      g.fillStyle = 'rgba(20,10,4,0.72)';
      roundRect(g, x, y - ph / 2, Math.max(24, pw * open), ph, 20); g.fill();
      g.shadowColor = 'transparent';
      g.fillStyle = '#f4a62a';
      roundRect(g, x, y - ph / 2, 12, ph, 6); g.fill();
      g.save();
      g.beginPath(); g.rect(x, y - ph / 2, pw * open, ph); g.clip();
      g.fillStyle = '#fff7ea';
      g.textBaseline = 'middle';
      g.fillText(cap.text, x + 48, y + 3 + (1 - open) * 30);
      g.restore();
    } else if (cap.style === 'top') {
      const size = fitFont(g, cap.text, 800, 64, 'Montserrat', W - 400);
      const tw = g.measureText(cap.text).width;
      const pw = tw + 110, ph = size + 44, x = W / 2, y = 96 + (1 - a) * -30;
      g.globalAlpha = a;
      g.shadowColor = 'rgba(0,0,0,0.5)'; g.shadowBlur = 26; g.shadowOffsetY = 8;
      g.fillStyle = 'rgba(20,10,4,0.78)';
      roundRect(g, x - pw / 2, y - ph / 2, pw, ph, ph / 2); g.fill();
      g.shadowColor = 'transparent';
      g.strokeStyle = '#f4a62a'; g.lineWidth = 4; g.stroke();
      g.fillStyle = '#fff7ea'; g.textAlign = 'center'; g.textBaseline = 'middle';
      g.fillText(cap.text, x, y + 3);
    } else if (cap.style === 'title') {
      const full = cap.accent + cap.text;
      const size = fitFont(g, full, 800, 92, 'Montserrat', W - 220);
      g.globalAlpha = a;
      g.fillStyle = linear(g, 0, H - 400, 0, H, [[0, 'rgba(10,4,0,0)'], [1, 'rgba(10,4,0,0.85)']]);
      g.fillRect(0, H - 400, W, 400);
      const tw = g.measureText(full).width, aw = g.measureText(cap.accent).width;
      const x = (W - tw) / 2, y = H - 130;
      g.textBaseline = 'alphabetic';
      g.shadowColor = 'rgba(0,0,0,0.7)'; g.shadowBlur = 24; g.shadowOffsetY = 6;
      // посимвольное появление
      let cx = x;
      const chars = Array.from(full);
      for (let i = 0; i < chars.length; i++) {
        const ch = chars[i];
        const ca = ramp(t, cap.start + i * 0.022, cap.start + i * 0.022 + 0.35, E.out);
        g.globalAlpha = a * ca;
        g.fillStyle = i < cap.accent.length ? '#ffb43c' : '#fff7ea';
        g.fillText(ch, cx, y + (1 - ca) * 24);
        cx += g.measureText(ch).width;
      }
      g.globalAlpha = a;
      g.shadowColor = 'transparent';
      g.fillStyle = '#f4a62a';
      const lw = 260 * ramp(t, cap.start + 0.3, cap.start + 0.9, E.out);
      roundRect(g, x, y + 30, lw, 8, 4); g.fill();
      void aw;
    } else if (cap.style === 'final') {
      g.globalAlpha = a;
      g.fillStyle = linear(g, 0, H - 460, 0, H, [[0, 'rgba(10,4,0,0)'], [1, 'rgba(10,4,0,0.88)']]);
      g.fillRect(0, H - 460, W, 460);
      g.textAlign = 'center'; g.textBaseline = 'alphabetic';
      g.shadowColor = 'rgba(0,0,0,0.75)'; g.shadowBlur = 26; g.shadowOffsetY = 6;
      fitFont(g, cap.text, 700, 66, 'Montserrat', W - 240);
      g.fillStyle = '#fff7ea';
      g.fillText(cap.text, W / 2, H - 220 + (1 - a) * 20);
      const b = ramp(t, cap.start + 0.6, cap.start + 1.1, E.out);
      g.globalAlpha = a * b;
      fitFont(g, cap.sub, 700, 118, 'PT Serif', W - 240);
      g.fillStyle = '#ffb43c';
      g.fillText(cap.sub, W / 2, H - 82 + (1 - b) * 26);
    }
    g.restore();
  }

  function drawCard(g, card, t) {
    const a = ramp(t, card.start, card.start + 0.4, E.out) * (1 - ramp(t, card.end - 0.3, card.end));
    if (a <= 0.003) return;
    const cw = 700, x = W - 72 - cw, y = 72, rowH = 58, pad = 34;
    const ch = 92 + card.rows.length * rowH + (card.note ? 56 : 0) + 18;
    g.save();
    g.globalAlpha = a;
    g.translate((1 - a) * 60, 0);
    g.shadowColor = 'rgba(0,0,0,0.55)'; g.shadowBlur = 34; g.shadowOffsetY = 12;
    g.fillStyle = 'rgba(20,10,4,0.76)';
    roundRect(g, x, y, cw, ch, 26); g.fill();
    g.shadowColor = 'transparent';
    g.strokeStyle = 'rgba(255,190,110,0.35)'; g.lineWidth = 2; g.stroke();
    g.textBaseline = 'middle';
    g.fillStyle = '#ffb43c';
    g.font = '800 34px Montserrat';
    g.fillText(card.title.toUpperCase(), x + pad, y + 50);
    g.fillStyle = 'rgba(255,190,110,0.35)';
    g.fillRect(x + pad, y + 82, cw - pad * 2, 2);
    card.rows.forEach(([name, qty, key], i) => {
      const ra = ramp(t, card.start + 0.15 + i * 0.09, card.start + 0.5 + i * 0.09, E.out);
      const ry = y + 92 + rowH * i + rowH / 2 + 6;
      g.globalAlpha = a * ra;
      const dx = (1 - ra) * 24;
      g.font = '800 36px Montserrat';
      const qw = g.measureText(qty).width;
      const nameSize = fitFont(g, name, 600, 36, 'Montserrat', cw - pad * 2 - qw - 50);
      void nameSize;
      if (key) {
        g.fillStyle = '#f4a62a';
        g.beginPath(); g.arc(x + pad + 8 + dx, ry, 7, 0, TAU); g.fill();
      }
      g.fillStyle = key ? '#ffffff' : '#f2e4cc';
      g.fillText(name, x + pad + (key ? 26 : 0) + dx, ry);
      g.font = '800 36px Montserrat';
      g.fillStyle = '#ffc25e';
      g.textAlign = 'right';
      g.fillText(qty, x + cw - pad + dx, ry);
      g.textAlign = 'left';
    });
    if (card.note) {
      const na = ramp(t, card.start + 0.5, card.start + 0.9, E.out);
      g.globalAlpha = a * na;
      fitFont(g, card.note, 500, 30, 'Montserrat', cw - pad * 2);
      g.fillStyle = '#e9d6b6';
      g.fillText(card.note, x + pad, y + 92 + card.rows.length * rowH + 40);
    }
    g.restore();
  }

  // Индикатор ускоренного времени в сцене бульона.
  function drawClock(g, t) {
    const a = ramp(t, 9.1, 9.5, E.out) * (1 - ramp(t, 11.6, 11.9));
    if (a <= 0.003) return;
    const x = 96, y = 80, r = 54;
    g.save();
    g.globalAlpha = a;
    g.shadowColor = 'rgba(0,0,0,0.5)'; g.shadowBlur = 26;
    g.fillStyle = 'rgba(20,10,4,0.76)';
    roundRect(g, x, y, 520, 150, 75); g.fill();
    g.shadowColor = 'transparent';
    const cx = x + 75, cy = y + 75;
    g.strokeStyle = 'rgba(255,255,255,0.18)'; g.lineWidth = 9;
    g.beginPath(); g.arc(cx, cy, r, 0, TAU); g.stroke();
    const prog = ramp(t, 9.3, 11.6, (v) => v);
    g.strokeStyle = '#f4a62a'; g.lineCap = 'round';
    g.beginPath(); g.arc(cx, cy, r, -Math.PI / 2, -Math.PI / 2 + TAU * prog); g.stroke();
    const ang = -Math.PI / 2 + (t - 9.1) * TAU * 2.2;
    g.strokeStyle = '#fff7ea'; g.lineWidth = 6;
    g.beginPath(); g.moveTo(cx, cy); g.lineTo(cx + Math.cos(ang) * r * 0.72, cy + Math.sin(ang) * r * 0.72); g.stroke();
    g.lineWidth = 8;
    g.beginPath(); g.moveTo(cx, cy); g.lineTo(cx + Math.cos(ang / 12) * r * 0.45, cy + Math.sin(ang / 12) * r * 0.45); g.stroke();
    g.fillStyle = '#fff7ea'; g.textBaseline = 'middle';
    g.font = '800 46px Montserrat';
    g.fillText('1,5–2 часа', cx + 92, cy - 16);
    g.font = '500 28px Montserrat'; g.fillStyle = '#e9d6b6';
    g.fillText('время ускорено', cx + 94, cy + 30);
    g.restore();
  }

  // ---------------------------------------------------------------- кадр
  let ctx = null, LA = null, LB = null, LC = null;

  function init(canvas) {
    canvas.width = W; canvas.height = H;
    ctx = canvas.getContext('2d');
    if (!A) buildAssets();
    LA = makeCanvas(W, H).getContext('2d');
    LB = makeCanvas(W, H).getContext('2d');
    LC = makeCanvas(W, H).getContext('2d');
  }

  function drawScene(g, sc, t) {
    g.save();
    g.setTransform(1, 0, 0, 1, 0, 0);
    g.globalAlpha = 1; g.globalCompositeOperation = 'source-over';
    g.fillStyle = '#120804'; g.fillRect(0, 0, W, H);
    DRAW[sc.id](g, t);
    g.restore();
  }

  // Размытие движения: среднее нескольких сдвинутых копий (каждая следующая
  // копия с альфой 1/(i+1) даёт точное среднее поверх первой, непрозрачной).
  function blurInto(g, img, dx, blur, scale = 1) {
    const n = blur > 2 ? 7 : 1;
    // при сдвиге слегка увеличиваем слой, чтобы у края не открывался фон
    scale *= 1 + (2 * Math.abs(dx) + blur) / W;
    g.save();
    for (let i = 0; i < n; i++) {
      const o = n === 1 ? 0 : (i / (n - 1) - 0.5) * blur;
      g.globalAlpha = 1 / (i + 1);
      g.setTransform(scale, 0, 0, scale, W / 2 * (1 - scale) + dx + o, H / 2 * (1 - scale));
      g.drawImage(img, 0, 0);
    }
    g.restore();
  }

  function composite(g, a, b, p, type) {
    g.save();
    g.setTransform(1, 0, 0, 1, 0, 0);
    g.fillStyle = '#120804'; g.fillRect(0, 0, W, H);
    LC.setTransform(1, 0, 0, 1, 0, 0);
    LC.fillStyle = '#120804'; LC.fillRect(0, 0, W, H);
    const e = E.inOut(p);
    let ba;
    if (type === 'whip') {
      const blur = Math.sin(Math.PI * p) * 160;
      ba = clamp((p - 0.3) / 0.4);
      blurInto(g, a.canvas, -e * W * 0.35, blur);
      blurInto(LC, b.canvas, (1 - e) * W * 0.35, blur);
    } else {
      ba = clamp((p - 0.25) / 0.5);
      blurInto(g, a.canvas, 0, 0, 1 + 0.25 * e);
      blurInto(LC, b.canvas, 0, 0, 1.18 - 0.18 * e);
    }
    g.globalAlpha = ba;
    g.drawImage(LC.canvas, 0, 0);
    g.globalAlpha = 1;
    g.globalCompositeOperation = 'screen';
    g.fillStyle = `rgba(255,190,120,${0.12 * Math.sin(Math.PI * p)})`;
    g.fillRect(0, 0, W, H);
    g.restore();
  }

  function sceneAt(t) {
    for (let i = 0; i < SCENES.length; i++) if (t < SCENES[i].end) return i;
    return SCENES.length - 1;
  }

  function renderFrame(t) {
    const g = ctx;
    const i = sceneAt(t);
    const sc = SCENES[i], half = TRANSITION / 2;
    let a = null, b = null, p = 0;
    if (i > 0 && sc.transitionIn && t < sc.start + half) {
      a = SCENES[i - 1]; b = sc; p = (t - (sc.start - half)) / TRANSITION;
    } else if (i < SCENES.length - 1 && SCENES[i + 1].transitionIn && t >= SCENES[i + 1].start - half) {
      a = sc; b = SCENES[i + 1]; p = (t - (b.start - half)) / TRANSITION;
    }
    g.setTransform(1, 0, 0, 1, 0, 0);
    g.globalAlpha = 1; g.globalCompositeOperation = 'source-over';
    if (a) {
      drawScene(LA, a, t); drawScene(LB, b, t);
      composite(g, LA, LB, clamp(p), b.transitionIn);
    } else {
      drawScene(g, sc, t);
    }
    // общий тёплый грейдинг
    g.save();
    g.globalCompositeOperation = 'soft-light';
    g.fillStyle = radial(g, W * 0.3, H * 0.2, 50, W, [[0, 'rgba(255,170,90,0.45)'], [1, 'rgba(60,20,0,0.35)']]);
    g.fillRect(0, 0, W, H);
    g.restore();
    g.drawImage(A.vignette, 0, 0);
    // мягкое появление из темноты в начале
    const fadeIn = 0.75 * (1 - ramp(t, 0, 0.3, E.out));
    if (fadeIn > 0) { g.fillStyle = `rgba(0,0,0,${fadeIn})`; g.fillRect(0, 0, W, H); }
    for (const c of CARDS) drawCard(g, c, t);
    drawClock(g, t);
    for (const c of CAPTIONS) drawCaption(g, c, t);
    // плёночное зерно
    g.save();
    g.globalCompositeOperation = 'overlay';
    g.globalAlpha = 0.07;
    const gi = Math.floor(t * FPS) % 4;
    const ox = -Math.floor(R(Math.floor(t * FPS), 1) * 300), oy = -Math.floor(R(Math.floor(t * FPS), 2) * 300);
    for (let x = ox; x < W; x += 384) for (let y = oy; y < H; y += 384) g.drawImage(A.grain[gi], x, y);
    g.restore();
  }

  root.KHARCHO = Object.assign({ init, renderFrame }, TIMELINE);
})(typeof window !== 'undefined' ? window : globalThis);
