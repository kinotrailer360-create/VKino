# Генератор треков в разных стилях (без авторских прав) под структуру эдита FootyVibe360.
# Секции (в долях): 0..build интро (приглушено) → build..freeze нарастание → freeze..roll «бум»+свип (стоп-кадр)
#                    → roll..drop дробь+райзер → drop..replay ДРОП → replay..outro приглушено → outro..total финал.
# Использование: python3 music_styles.py out.wav '{"style":"drill","bpm":140,"key":0,"seed":1,"build":10,"freeze":18,"roll":20,"drop":23,"replay":46,"outro":46,"total":51}'
# Стили: phonk, drill, funk, epic, house
import numpy as np, wave, sys, json
P = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
STYLE = P.get('style', 'drill'); SR = 44100; BPM = P.get('bpm', 140); B = 60 / BPM
I = P.get('build', 8); FR = P.get('freeze', 13); RO = P.get('roll', 15); DR = P.get('drop', 20)
RP = P.get('replay', 35); OU = P.get('outro', 41); TOT = P.get('total', 45); KEY = P.get('key', 0)
rng = np.random.default_rng(P.get('seed', 1))
N = int(TOT * B * SR) + SR

def at(b): return int(b * B * SR)
def buf(): return np.zeros(N)
def add(dst, sig, beat, g=1.0):
    i = at(beat); j = min(N, i + len(sig))
    if j > i: dst[i:j] += g * sig[:j - i]
def env(n, dec): return np.exp(-np.arange(n) / SR / dec)
def lp(x, hi=None, lo=None):
    if len(x) < 2: return x
    X = np.fft.rfft(x); f = np.fft.rfftfreq(len(x), 1 / SR); m = np.ones_like(f)
    if hi: m *= 1 / (1 + (f / hi) ** 4)
    if lo: m *= 1 / (1 + (lo / np.maximum(f, 1)) ** 4)
    return np.fft.irfft(X * m, len(x))
def note(m): return 440 * 2 ** ((m + KEY - 69) / 12)
def t_(n): return np.arange(n) / SR

# ---------- инструменты ----------
def kick(dur=0.45, f0=150, f1=50, drive=3):
    n = int(dur * SR); t = t_(n); f = f1 + (f0 - f1) * np.exp(-t / 0.03)
    return np.tanh(drive * np.sin(2 * np.pi * np.cumsum(f) / SR) * env(n, dur * 0.5))
def b808(freq, dur=0.9, glide_to=None, drive=2.5):
    n = int(dur * SR); t = t_(n)
    f = freq * np.ones(n) if glide_to is None else freq + (glide_to - freq) * np.clip((t - dur * 0.35) / (dur * 0.3), 0, 1)
    f = f + 90 * np.exp(-t / 0.02)
    return np.tanh(drive * np.sin(2 * np.pi * np.cumsum(f) / SR) * env(n, dur * 0.6))
def snare(dur=0.22, tone=200, noise_lo=1200):
    n = int(dur * SR); t = t_(n)
    return (lp(rng.standard_normal(n), lo=noise_lo, hi=9000) * 0.8 + np.sin(2 * np.pi * tone * t) * 0.5) * env(n, 0.05)
def clap():
    n = int(0.25 * SR); s = lp(rng.standard_normal(n), lo=900, hi=6000); e = env(n, 0.06)
    k = int(0.02 * SR); e[:k] *= np.tile([1, 0.3], k)[:k]; return s * e
def hat(open_=False, dec=None):
    n = int((0.25 if open_ else 0.05) * SR)
    return lp(rng.standard_normal(n), lo=7000) * env(n, dec or (0.08 if open_ else 0.012))
def tom(freq, dur=0.5):
    n = int(dur * SR); t = t_(n); f = freq * (1 + 0.6 * np.exp(-t / 0.05))
    return np.tanh(2 * np.sin(2 * np.pi * np.cumsum(f) / SR) * env(n, 0.18)) + lp(rng.standard_normal(n), hi=900) * env(n, 0.03) * 0.5
def cowbell(freq, dur=0.35):
    n = int(dur * SR); s = 0.6 * np.sign(np.sin(2 * np.pi * freq * t_(n))) + 0.4 * np.sign(np.sin(2 * np.pi * freq * 1.48 * t_(n)))
    return lp(s, lo=400, hi=3500) * env(n, 0.09)
def bell(freq, dur=0.9):   # тёмный «колокольчик/пиано» для дрилла
    n = int(dur * SR); t = t_(n)
    s = np.sin(2 * np.pi * freq * t) + 0.5 * np.sin(2 * np.pi * freq * 2.01 * t) * env(n, 0.15) + 0.25 * np.sin(2 * np.pi * freq * 3.98 * t) * env(n, 0.06)
    return s * env(n, 0.45) * (1 - np.exp(-t / 0.003))
def pluck(freq, dur=0.3, bright=4000):
    n = int(dur * SR); t = t_(n); s = 2 * ((freq * t) % 1) - 1
    return lp(s, hi=bright) * env(n, 0.08)
def saw_pad(freqs, dur, hi=1800, amp=0.08):
    n = int(dur * SR); t = t_(n)
    s = sum(2 * ((f * (1 + d) * t) % 1) - 1 for f in freqs for d in (-0.004, 0.004))
    a = np.minimum(1, t / 0.4) * np.minimum(1, (dur - t) / 0.3 + 0.0001)
    return lp(s, hi=hi) * a * amp
def chop(freq, dur=0.16):   # «вокальный чоп» для фанка: пила через два форманта
    n = int(dur * SR); t = t_(n); s = 2 * ((freq * t) % 1) - 1
    s = lp(s, lo=500, hi=1400) + 0.6 * lp(s, lo=2200, hi=3200)
    return s * env(n, 0.07) * (1 - np.exp(-t / 0.004))
def hit(dur=2.0):   # оркестровый удар
    n = int(dur * SR); t = t_(n)
    s = sum(2 * ((note(m) * t) % 1) - 1 for m in (26, 38, 45, 50, 57)) * 0.15
    return (lp(s, hi=2500) + lp(rng.standard_normal(n), hi=3000) * 0.3 * env(n, 0.05)) * env(n, 0.7)

drums, bass, lead, pad, fx = buf(), buf(), buf(), buf(), buf()
MIN = [0, 2, 3, 5, 7, 8, 10]          # натуральный минор
def deg(d, octave=5): return 12 * octave + MIN[d % 7] + 12 * (d // 7)

# ---------- стили: паттерн одного такта (16 шагов) ----------
def bar_phonk(b, full=True):
    mel = [7, None, 9, 7, None, 6, None, 7, 4, None, 6, None, 7, None, 5, None]
    for k, d in enumerate(mel):
        if d is not None: add(lead, cowbell(note(deg(d, 5))), b + k * 0.25, 0.5)
    if not full: return
    for q in range(4):
        if q == 0: add(bass, b808(note(deg(0, 3)), 1.6), b + q, 1.0)
        if q == 2: add(bass, b808(note(deg(0, 3)), 0.5), b + q + 0.5, 0.8)
        if q in (1, 3): add(drums, clap(), b + q, 0.8)
        for h in (0, 0.25, 0.5, 0.75): add(drums, hat(h == 0.5 and q == 3), b + q + h, 0.35)

def bar_drill(b, full=True):
    # мрачная мелодия «колокольчиком», половинный темп: снейр на 3-ю долю
    mel = [(0, 0), (0.75, 2), (1.5, 3), (2.5, 2), (3.0, -1)]
    for off, d in mel: add(lead, bell(note(deg(d + 7, 4))), b + off, 0.35)
    if not full: return
    add(bass, b808(note(deg(0, 2)), 1.4, glide_to=note(deg(0, 2)) * 1.12), b, 1.0)
    add(bass, b808(note(deg(5, 1)), 0.7), b + 2.75, 0.9)
    add(drums, kick(0.3), b, 0.7); add(drums, kick(0.3), b + 2.75, 0.6)
    add(drums, snare(), b + 2, 0.9)
    k = 0.0
    while k < 4:                         # хэты с триольными «роллами»
        add(drums, hat(), b + k, 0.32)
        if k in (1.5, 3.5):
            for r in range(3): add(drums, hat(), b + k + r / 6, 0.22)
            k += 0.5
        else: k += 0.25

def bar_funk(b, full=True):
    # бразильский фанк: «тамборзан» + вокальные чопы
    chops = [(0, 4), (0.75, 4), (1.5, 6), (2.5, 4), (3.25, 2)]
    for off, d in chops: add(lead, chop(note(deg(d, 5))), b + off, 0.55)
    if not full: return
    for s in (0, 0.75, 1.5, 2, 2.75, 3.5): add(drums, kick(0.25, 170, 55, 4), b + s, 0.9)
    for s in (1, 3): add(drums, snare(0.18, 250, 1500), b + s, 0.8)
    for s in (0.5, 1.25, 2.25, 3.25, 3.75): add(drums, tom(note(deg(4, 3))), b + s, 0.35)
    for h in np.arange(0, 4, 0.5): add(drums, hat(), b + h, 0.3)
    add(bass, b808(note(deg(0, 2)), 0.5, drive=4), b, 0.9); add(bass, b808(note(deg(3, 2)), 0.5, drive=4), b + 2, 0.9)

def bar_epic(b, full=True):
    chord = [deg(0, 3), deg(2, 4), deg(4, 4)] if (b // 4) % 2 == 0 else [deg(5, 2), deg(0, 4), deg(2, 4)]
    add(pad, saw_pad([note(m) for m in chord], 4 * B, hi=1500, amp=0.07), b)
    for k, d in enumerate([4, 4, 5, 4, 2, 2, 4, 0]): add(lead, pluck(note(deg(d, 5)), 0.45, 2500), b + k * 0.5, 0.3)
    if not full: return
    for s in (0, 0.75, 1.5, 2, 3, 3.5): add(drums, tom(note(deg(0, 2)) * 1.5, 0.6), b + s, 0.7)
    add(drums, snare(0.3, 180, 800), b + 1, 0.7); add(drums, snare(0.3, 180, 800), b + 3, 0.7)
    add(bass, saw_pad([note(deg(0, 2))], 4 * B, hi=400, amp=0.25), b)

def bar_house(b, full=True):
    prog = [0, 5, 3, 4][(b // 4) % 4]
    for k in range(8):
        if k % 2: add(lead, sum(pluck(note(deg(prog + i, 5)), 0.25, 3000) for i in (0, 2, 4)), b + k * 0.5, 0.18)
    if not full: return
    for q in range(4):
        add(drums, kick(0.35, 130, 48, 2.5), b + q, 0.85)
        add(drums, hat(True, 0.05), b + q + 0.5, 0.3)
        if q in (1, 3): add(drums, clap(), b + q, 0.6)
        add(bass, pluck(note(deg(prog, 2)), 0.3, 700) * 2.2, b + q + 0.5, 0.6)

BAR = {'phonk': bar_phonk, 'drill': bar_drill, 'funk': bar_funk, 'epic': bar_epic, 'house': bar_house}[STYLE]

# ---------- аранжировка ----------
for b in range(0, I, 4): BAR(b, full=False)                                   # интро
n = at(I); pc = [note(deg(d, 3)) for d in (0, 4, 7)]
add(pad, saw_pad(pc, I * B, hi=900, amp=0.05), 0)
for b in range(I, FR, 4): BAR(b, full=True)                                   # нарастание (полный бит, но тише)
add(fx, hit(2.2) if STYLE == 'epic' else kick(1.5, 120, 32, 2), FR, 0.9)    # «бум» на стоп-кадре
n = at(RO - FR); add(fx, lp(rng.standard_normal(n), lo=2000) * np.linspace(0, 0.25, n), FR)
k = float(RO); step = 0.5; RL = max(0.5, DR - 0.5 - RO)                     # дробь
while k < DR - 0.5:
    add(drums, snare(0.15) if STYLE in ('drill', 'epic') else clap(), k, 0.25 + 0.5 * (k - RO) / RL)
    if k >= RO + RL * 0.45: step = 0.25
    if k >= RO + RL * 0.78: step = 0.125
    k += step
n = at(DR - 0.5 - RO); tt = t_(n)
if n > 10:
    f = 200 * 2 ** (tt / tt[-1] * 4)
    add(fx, np.sin(2 * np.pi * np.cumsum(f) / SR) * 0.15 * (tt / tt[-1]) ** 2 + lp(rng.standard_normal(n), lo=3000) * 0.2 * (tt / tt[-1]) ** 2, RO)
add(fx, lp(rng.standard_normal(at(2)), lo=500) * env(at(2), 0.4) * 0.5, DR)   # крэш на дроп
if STYLE == 'epic': add(fx, hit(2.5), DR, 1.0)
for b in range(DR, RP, 4): BAR(b, full=True)                                  # ДРОП
for b in range(RP, OU, 4): BAR(b, full=True)                                  # реплей (приглушим ниже)
for b in range(OU, TOT, 4): BAR(b, full=False)                                # финал без барабанов
add(pad, saw_pad(pc, max(0.5, (TOT - OU)) * B, hi=900, amp=0.05), OU)

mix = drums + bass * 0.9 + lead + pad + fx
i = at(I); mix[:i] = lp(mix[:i], hi=2500)                                     # интро глуше
i, j = at(I), at(FR); mix[i:j] *= 0.8
i, j = at(RP), at(OU)
if j > i: mix[i:j] = lp(mix[i:j], hi=900)
for d, g in ((0.11, 0.22), (0.23, 0.13), (0.37, 0.07)):                      # короткая «реверберация»
    s = int(d * SR); mix[s:] += g * lp(mix[:-s], hi=4000)
mix = np.tanh(mix * 1.2); mix /= np.max(np.abs(mix)) * 1.05
L = at(TOT); mix = mix[:L]; f = int(0.05 * SR); mix[-f:] *= np.linspace(1, 0, f)
with wave.open(sys.argv[1], 'wb') as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR); w.writeframes((mix * 32767).astype(np.int16).tobytes())
print('ok', STYLE, round(L / SR, 2))
