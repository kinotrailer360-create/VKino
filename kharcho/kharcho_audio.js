// Музыка и звуковые эффекты для харчо — чистый синтез PCM, без сэмплов.
// node kharcho_audio.js  →  kharcho_music.wav, kharcho_sfx.wav (48 кГц, стерео, 30 с)
//
// Музыка: 6/8, 72 уд./мин (такт 1,667 с, 18 тактов = ровно 30 с), ля минор с дорийской
// секстой и увеличенной секундой в мелодии; щипковый «пандури» (Карплус — Стронг),
// бурдон в квинту, свирель-«саламури» и мягкий «доли» (бочка + обод).
const fs = require('fs');
const path = require('path');
const T = require('./kharcho_animation.js');

const SR = 48000;
const N = Math.round(T.DURATION * SR);
const BAR = 60 / 72 * 2;          // такт 6/8 = две пунктирные четверти
const E8 = BAR / 6;               // восьмая

function rng(seed) { let s = seed >>> 0; return () => ((s = (s * 1664525 + 1013904223) >>> 0) / 4294967296); }
const midi = (m) => 440 * Math.pow(2, (m - 69) / 12);

function track() { return [new Float32Array(N), new Float32Array(N)]; }
function add(tr, i, v, pan) {
  if (i < 0 || i >= N) return;
  tr[0][i] += v * Math.cos((pan + 1) * Math.PI / 4);
  tr[1][i] += v * Math.sin((pan + 1) * Math.PI / 4);
}

// Простой бикуад (RBJ) для фильтрации шумов.
function biquad(type, f, q) {
  const w = 2 * Math.PI * f / SR, a = Math.sin(w) / (2 * q), c = Math.cos(w);
  let b0, b1, b2, a0, a1, a2;
  if (type === 'lp') { b0 = (1 - c) / 2; b1 = 1 - c; b2 = b0; }
  else if (type === 'hp') { b0 = (1 + c) / 2; b1 = -(1 + c); b2 = b0; }
  else { b0 = a; b1 = 0; b2 = -a; } // bp
  a0 = 1 + a; a1 = -2 * c; a2 = 1 - a;
  let x1 = 0, x2 = 0, y1 = 0, y2 = 0;
  return (x) => {
    const y = (b0 * x + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2) / a0;
    x2 = x1; x1 = x; y2 = y1; y1 = y;
    return y;
  };
}

// ---------------------------------------------------------------- музыка
const music = track();

// Аккорды по тактам (MIDI корня и квинты бурдона + терция).
const CHORDS = [
  [45, 52, 57, 60], [45, 52, 57, 60], [43, 50, 55, 59], [45, 52, 57, 60],
  [41, 48, 53, 57], [43, 50, 55, 59], [45, 52, 57, 60], [40, 47, 52, 56],
  [45, 52, 57, 60], [48, 55, 60, 64], [43, 50, 55, 59], [45, 52, 57, 60],
  [41, 48, 53, 57], [43, 50, 55, 59], [40, 47, 52, 56], [45, 52, 57, 60],
  [43, 50, 55, 59], [45, 52, 57, 64],
];

function pluck(t0, freq, dur, amp, pan, seed) {
  // Карплус — Стронг: тёплая щипковая струна.
  const r = rng(seed);
  const L = Math.max(2, Math.round(SR / freq));
  const buf = new Float32Array(L);
  for (let i = 0; i < L; i++) buf[i] = r() * 2 - 1;
  const start = Math.round(t0 * SR), len = Math.round(dur * SR);
  let p = 0, prev = 0;
  for (let i = 0; i < len; i++) {
    const cur = buf[p];
    const nxt = buf[(p + 1) % L];
    const v = 0.5 * (cur + nxt) * 0.996;
    buf[p] = v;
    p = (p + 1) % L;
    const out = 0.6 * cur + 0.4 * prev; prev = cur;
    const env = Math.min(1, i / 60) * (i > len - 2400 ? (len - i) / 2400 : 1);
    add(music, start + i, out * amp * env, pan);
  }
}

function pad(t0, dur, freqs, amp) {
  const start = Math.round(t0 * SR), len = Math.round(dur * SR);
  const lp = [biquad('lp', 900, 0.7), biquad('lp', 900, 0.7)];
  const ph = freqs.map(() => [Math.random(), Math.random(), Math.random()]);
  for (let i = 0; i < len; i++) {
    const t = i / SR;
    const att = Math.min(1, t / 0.5), rel = Math.min(1, (dur - t) / 0.5);
    let l = 0, rr = 0;
    freqs.forEach((f, k) => {
      for (let d = 0; d < 3; d++) {
        const fr = f * (1 + (d - 1) * 0.004);
        ph[k][d] += fr / SR;
        const saw = 2 * (ph[k][d] % 1) - 1;
        if (d === 0) l += saw; else if (d === 2) rr += saw; else { l += saw * 0.5; rr += saw * 0.5; }
      }
    });
    const env = att * rel * amp / freqs.length;
    const idx = start + i;
    if (idx < N) { music[0][idx] += lp[0](l) * env; music[1][idx] += lp[1](rr) * env; }
  }
}

function flute(notes, amp) {
  // Свирель: синус + обертоны, вибрато, лёгкий шум дыхания, плавные связки.
  const r = rng(77);
  const breathF = biquad('bp', 2400, 1.2);
  for (const [t0, m, dur] of notes) {
    const f = midi(m), start = Math.round(t0 * SR), len = Math.round(dur * SR);
    let ph = 0;
    for (let i = 0; i < len; i++) {
      const t = i / SR;
      const vib = 1 + 0.006 * Math.sin(2 * Math.PI * 5.4 * t) * Math.min(1, t / 0.25);
      // короткий форшлаг сверху в начале ноты — восточное украшение
      const grace = t < 0.05 ? Math.pow(2, 2 / 12) : 1;
      ph += f * vib * grace / SR;
      const s = Math.sin(2 * Math.PI * ph) + 0.28 * Math.sin(4 * Math.PI * ph) + 0.1 * Math.sin(6 * Math.PI * ph);
      const env = Math.min(1, t / 0.06) * Math.min(1, (dur - t) / 0.12);
      const br = breathF(r() * 2 - 1) * 0.25;
      add(music, start + i, (s + br) * env * amp, 0.15);
    }
  }
}

function drum(t0, kind, amp) {
  const r = rng(Math.round(t0 * 1000) + (kind === 'dum' ? 1 : 2));
  const start = Math.round(t0 * SR);
  if (kind === 'dum') {
    let ph = 0;
    for (let i = 0; i < SR * 0.45; i++) {
      const t = i / SR;
      const f = 52 + 70 * Math.exp(-t * 28);
      ph += f / SR;
      add(music, start + i, Math.sin(2 * Math.PI * ph) * Math.exp(-t * 7) * amp, 0);
    }
  } else {
    const bp = biquad('bp', kind === 'tek' ? 2600 : 7000, kind === 'tek' ? 1.4 : 0.8);
    const dec = kind === 'tek' ? 45 : 30;
    for (let i = 0; i < SR * 0.15; i++) {
      const t = i / SR;
      add(music, start + i, bp(r() * 2 - 1) * Math.exp(-t * dec) * amp * 2.2, kind === 'tek' ? -0.25 : 0.35);
    }
  }
}

function buildMusic() {
  // бурдон-пэд на всю длину
  CHORDS.forEach((ch, b) => pad(b * BAR, BAR + 0.02, [midi(ch[0]), midi(ch[1]), midi(ch[2] + 12)], 0.16));
  // пандури: арпеджио восьмыми
  const pattern = [0, 2, 1, 3, 2, 1];
  CHORDS.forEach((ch, b) => {
    const tones = [ch[0] + 12, ch[1] + 12, ch[2] + 12, ch[3] + 12];
    for (let k = 0; k < 6; k++) {
      const t = b * BAR + k * E8;
      if (b === 17 && k > 0) break;
      pluck(t, midi(tones[pattern[k]]), b === 17 ? 2.2 : E8 * 2.4, k === 0 ? 0.32 : 0.22, -0.35, b * 10 + k);
    }
  });
  // доли: дум на 1 и 4, тек — остальные доли, бубен на слабых
  for (let b = 1; b < 17; b++) {
    for (let k = 0; k < 6; k++) {
      const t = b * BAR + k * E8;
      if (k === 0 || (k === 3 && b % 2 === 1)) drum(t, 'dum', 0.5);
      else if (k === 3 || k === 5) drum(t, 'tek', 0.28);
      else if (k === 2 || k === 4) drum(t, 'jingle', 0.12);
    }
  }
  drum(17 * BAR, 'dum', 0.45);
  // мелодия свирели (оригинальная), ля минор с G# и F — увеличенная секунда
  const q = E8;
  const M = [];
  const phrase = (bar, notes) => { let t = bar * BAR; for (const [m, len] of notes) { if (m) M.push([t, m, len * q * 0.98]); t += len * q; } };
  phrase(2, [[76, 2], [74, 1], [72, 2], [71, 1], [69, 3], [0, 3]]);
  phrase(4, [[72, 1], [74, 1], [76, 1], [77, 2], [76, 1], [74, 3], [72, 3]]);
  phrase(6, [[76, 2], [77, 1], [80, 2], [77, 1], [76, 6]]);
  phrase(8, [[81, 3], [79, 2], [77, 1], [76, 2], [74, 1], [72, 3]]);
  phrase(10, [[74, 2], [72, 1], [71, 2], [72, 1], [69, 6]]);
  phrase(12, [[72, 1], [74, 1], [76, 1], [79, 3], [77, 2], [76, 1], [74, 3]]);
  phrase(14, [[76, 2], [74, 1], [71, 2], [68, 1], [69, 6]]);
  phrase(16, [[72, 2], [71, 1], [69, 3], [76, 6]]);
  flute(M, 0.11);

  // общая громкость, плавное начало и конец ровно на 30 с
  let peak = 0;
  for (let c = 0; c < 2; c++) for (let i = 0; i < N; i++) peak = Math.max(peak, Math.abs(music[c][i]));
  const g = 0.7 / peak; // −3 дБFS
  for (let i = 0; i < N; i++) {
    const t = i / SR;
    const fade = Math.min(1, t / 1.2) * Math.min(1, (T.DURATION - t) / 2.0);
    const f = Math.sin(Math.PI / 2 * fade);
    music[0][i] *= g * f; music[1][i] *= g * f;
  }
}

// ---------------------------------------------------------------- эффекты
const sfx = track();

function envAt(t, a, b, fade = 0.25) { return Math.min(1, (t - a) / fade, (b - t) / fade); }

function boil(a, b, level, seed) {
  const r = rng(seed);
  const lp = biquad('bp', 260, 0.8);
  const s0 = Math.round(a * SR), s1 = Math.round(b * SR);
  for (let i = s0; i < s1; i++) {
    const t = i / SR, e = Math.max(0, envAt(t, a, b, 0.4));
    add(sfx, i, lp(r() * 2 - 1) * 0.35 * e * level, 0);
  }
  // лопающиеся пузырьки
  for (let t = a; t < b;) {
    t += -Math.log(1 - r()) / 30;
    const f0 = 300 + r() * 700, len = 0.012 + r() * 0.03, amp = (0.2 + r() * 0.5) * level;
    const st = Math.round(t * SR), pan = r() * 1.2 - 0.6;
    let ph = 0;
    for (let i = 0; i < len * SR; i++) {
      const tt = i / SR;
      ph += f0 * (1 + tt / len * 1.5) / SR;
      add(sfx, st + i, Math.sin(2 * Math.PI * ph) * Math.exp(-tt / len * 3) * amp * Math.max(0, envAt(t, a, b, 0.4)), pan);
    }
  }
}

function sizzle(a, b, level, seed) {
  const r = rng(seed);
  const hp = biquad('hp', 3500, 0.7), hp2 = biquad('hp', 2000, 0.7);
  let mod = 0;
  for (let i = Math.round(a * SR); i < b * SR; i++) {
    const t = i / SR, e = Math.max(0, envAt(t, a, b, 0.35));
    mod += (r() - 0.5) * 0.02; mod = Math.max(-0.4, Math.min(0.4, mod));
    let v = hp(r() * 2 - 1) * (0.55 + mod) * 0.5;
    if (r() < 0.002) v += hp2(1) * 2; // треск
    else hp2(0);
    add(sfx, i, v * e * level, (r() - 0.5) * 0.3);
  }
}

function pour(a, b, level, seed) {
  const r = rng(seed);
  const bp = biquad('bp', 700, 1.2), bp2 = biquad('bp', 300, 2);
  for (let i = Math.round(a * SR); i < b * SR; i++) {
    const t = i / SR, e = Math.max(0, envAt(t, a, b, 0.2));
    const gurgle = 0.6 + 0.4 * Math.sin(2 * Math.PI * (7 + 3 * Math.sin(t * 2.1)) * t);
    const v = bp(r() * 2 - 1) * 0.7 * gurgle + bp2(r() * 2 - 1) * 0.5;
    add(sfx, i, v * e * level, 0.1);
  }
}

function sprinkle(a, b, level, seed) {
  const r = rng(seed);
  const hp = biquad('hp', 5000, 0.7);
  for (let t = a; t < b;) {
    t += -Math.log(1 - r()) / 90;
    const st = Math.round(t * SR), amp = r() * level;
    for (let i = 0; i < 300; i++) add(sfx, st + i, hp(r() * 2 - 1) * Math.exp(-i / 60) * amp, r() - 0.5);
  }
}

function plop(at, seed) {
  const r = rng(seed);
  const st = Math.round(at * SR);
  let ph = 0;
  const bp = biquad('bp', 900, 1);
  for (let i = 0; i < SR * 0.35; i++) {
    const t = i / SR;
    ph += (180 + 900 * t / 0.08) / SR;
    const tone = t < 0.08 ? Math.sin(2 * Math.PI * ph) * Math.exp(-t * 30) * 0.6 : 0;
    const spl = bp(r() * 2 - 1) * Math.exp(-t * 14) * 0.5;
    add(sfx, st + i, (tone + spl) * 0.8, (r() - 0.5) * 0.4);
  }
}

function whoosh(at, seed) {
  const r = rng(seed);
  const st = Math.round((at - 0.25) * SR), len = Math.round(0.5 * SR);
  for (let i = 0; i < len; i++) {
    const p = i / len;
    const bp = whoosh.f || (whoosh.f = {});
    const f = 400 + 2600 * Math.sin(Math.PI * p);
    if (i % 64 === 0) bp.cur = biquad('bp', f, 0.9);
    add(sfx, st + i, bp.cur(r() * 2 - 1) * Math.sin(Math.PI * p) ** 2 * 0.35, (p - 0.5) * 1.2);
  }
}

function buildSfx() {
  T.SFX.forEach((e, k) => {
    const seed = 1000 + k * 17;
    if (e.type === 'boil') boil(e.start, e.end, e.level, seed);
    else if (e.type === 'sizzle') sizzle(e.start, e.end, e.level, seed);
    else if (e.type === 'pour') pour(e.start, e.end, e.level, seed);
    else if (e.type === 'sprinkle') sprinkle(e.start, e.end, e.level, seed);
    else if (e.type === 'plop') plop(e.at, seed);
    else if (e.type === 'whoosh') whoosh(e.at, seed);
  });
  let peak = 0;
  for (let c = 0; c < 2; c++) for (let i = 0; i < N; i++) peak = Math.max(peak, Math.abs(sfx[c][i]));
  const g = 0.7 / peak;
  for (let i = 0; i < N; i++) {
    const t = i / SR, f = Math.min(1, t / 0.3) * Math.min(1, (T.DURATION - t) / 0.5);
    sfx[0][i] *= g * f; sfx[1][i] *= g * f;
  }
}

// ---------------------------------------------------------------- WAV
function writeWav(file, tr) {
  const buf = Buffer.alloc(44 + N * 4);
  buf.write('RIFF', 0); buf.writeUInt32LE(36 + N * 4, 4); buf.write('WAVE', 8);
  buf.write('fmt ', 12); buf.writeUInt32LE(16, 16); buf.writeUInt16LE(1, 20); buf.writeUInt16LE(2, 22);
  buf.writeUInt32LE(SR, 24); buf.writeUInt32LE(SR * 4, 28); buf.writeUInt16LE(4, 32); buf.writeUInt16LE(16, 34);
  buf.write('data', 36); buf.writeUInt32LE(N * 4, 40);
  for (let i = 0; i < N; i++) for (let c = 0; c < 2; c++) {
    const v = Math.max(-1, Math.min(1, tr[c][i]));
    buf.writeInt16LE(Math.round(v * 32767), 44 + i * 4 + c * 2);
  }
  fs.writeFileSync(file, buf);
}

// Детерминированный Math.random для пэда — одинаковый результат при каждой сборке.
Math.random = rng(4242);
buildMusic();
buildSfx();
writeWav(path.join(__dirname, 'kharcho_music.wav'), music);
writeWav(path.join(__dirname, 'kharcho_sfx.wav'), sfx);
console.log('kharcho_music.wav, kharcho_sfx.wav:', T.DURATION, 'с,', SR, 'Гц, стерео');
