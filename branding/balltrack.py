# Виртуальная камера за мячом + маркер над мячом (стандартный шаг монтажа FootyVibe360).
# Кроп zoom× плавно едет за мячом (точки по раскадровке → интерполяция → сглаживание), мяч чуть впереди по ходу движения,
# окно не выходит за кадр. Маркер (треугольник в цвете клуба) висит над мячом и пропадает в момент касания сетки.
# Использование:
#   python3 balltrack.py in.mp4 out.mp4 points.json --ss 1.8 --frames 39 [--speed 0.35] [--zoom 1.4] [--color EDBB00]
#                        [--marker-start 1.0] [--marker-end 2.22] [--pre "минтерполяция/грейд до кропа"] [--post "фильтры после кропа"]
# points.json: [{"t":1.85,"x":329,"y":374}, ...] — время ИСТОЧНИКА (с), координаты в кадре 720x960 источника.
import sys, json, argparse, subprocess, numpy as np
from PIL import Image, ImageDraw
W, H, FPS = 720, 960, 30
ap = argparse.ArgumentParser()
ap.add_argument('inp'); ap.add_argument('out'); ap.add_argument('points')
ap.add_argument('--ss', type=float, required=True); ap.add_argument('--frames', type=int, required=True)
ap.add_argument('--speed', type=float, default=1.0); ap.add_argument('--zoom', type=float, default=1.4)
ap.add_argument('--color', default='EDBB00'); ap.add_argument('--marker-end', type=float, default=None)
ap.add_argument('--marker-start', type=float, default=None, help='маркер только с этого времени источника (до него точки мяча приблизительные)')
ap.add_argument('--lead', type=float, default=40, help='насколько держать мяч позади центра по ходу (px)')
ap.add_argument('--smooth', type=float, default=5, help='сглаживание камеры, σ в кадрах')
ap.add_argument('--pre', default=''); ap.add_argument('--post', default='')
a = ap.parse_args()
pts = sorted(json.load(open(a.points)), key=lambda p: p['t'])
T = np.array([p['t'] for p in pts]); X = np.array([p['x'] for p in pts], float); Y = np.array([p['y'] for p in pts], float)
n = a.frames
src_t = a.ss + np.arange(n) / FPS * a.speed                       # время источника для каждого кадра выхода
bx, by = np.interp(src_t, T, X), np.interp(src_t, T, Y)            # мяч (для маркера)
def gsmooth(v, s):
    if s <= 0: return v
    k = np.arange(-int(3 * s), int(3 * s) + 1); g = np.exp(-k ** 2 / (2 * s * s)); g /= g.sum()
    p = np.pad(v, len(k) // 2, mode='edge'); return np.convolve(p, g, 'valid')
vx, vy = np.gradient(gsmooth(bx, a.smooth)), np.gradient(gsmooth(by, a.smooth))
sp = np.hypot(vx, vy) + 1e-6; lead = np.clip(sp / 4, 0, 1) * a.lead
cx = gsmooth(bx + vx / sp * lead, a.smooth); cy = gsmooth(by + vy / sp * lead, a.smooth)   # центр окна: чуть впереди мяча
cw, ch = W / a.zoom, H / a.zoom
x0 = np.clip(cx - cw / 2, 0, W - cw); y0 = np.clip(cy - ch / 2, 0, H - ch)
col = tuple(int(a.color[i:i + 2], 16) for i in (0, 2, 4))

pre = (f"setpts=(PTS-STARTPTS)/{a.speed}," if a.speed != 1 else "") + f"fps={FPS}" + (f",{a.pre}" if a.pre else "")
dec = subprocess.Popen(['ffmpeg', '-v', 'error', '-ss', str(a.ss), '-t', str(n / FPS * a.speed + 0.5), '-i', a.inp,
                        '-vf', f"{pre},scale={W}:{H},format=rgb24", '-frames:v', str(n), '-f', 'rawvideo', '-'], stdout=subprocess.PIPE)
post = f",{a.post}" if a.post else ""
enc = subprocess.Popen(['ffmpeg', '-v', 'error', '-y', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-s', f'{W}x{H}', '-r', str(FPS), '-i', '-',
                        '-vf', f"format=yuv420p{post}", '-frames:v', str(n), '-an', '-c:v', 'libx264', '-crf', '16', '-preset', 'medium', a.out],
                       stdin=subprocess.PIPE)
last = None
for i in range(n):
    buf = dec.stdout.read(W * H * 3)
    if len(buf) < W * H * 3: buf = last                            # исходник короче — держим последний кадр
    last = buf
    im = Image.frombytes('RGB', (W, H), buf)
    show = (a.marker_end is None or src_t[i] < a.marker_end) and (a.marker_start is None or src_t[i] >= a.marker_start)
    if show:                                                       # маркер: треугольник над мячом с мягким появлением
        al = min(1.0, (i + 1) / 6) if a.marker_start is None else min(1.0, (src_t[i] - a.marker_start) / (6 / FPS * a.speed) + 0.01)
        mx, my = bx[i], by[i] - 22 / a.zoom
        s = 20 / a.zoom
        L = Image.new('RGBA', (W, H), (0, 0, 0, 0)); d = ImageDraw.Draw(L)
        tri = [(mx - s, my - 1.7 * s), (mx + s, my - 1.7 * s), (mx, my)]
        d.polygon([(px, py + 2) for px, py in tri], fill=(0, 0, 0, int(140 * al)))
        d.polygon(tri, fill=col + (int(255 * al),))
        im = Image.alpha_composite(im.convert('RGBA'), L).convert('RGB')
    im = im.resize((W, H), Image.LANCZOS, box=(x0[i], y0[i], x0[i] + cw, y0[i] + ch))   # субпиксельный кроп — без рывков
    enc.stdin.write(im.tobytes())
enc.stdin.close(); enc.wait(); dec.stdout.close(); dec.wait()
print('balltrack', a.out, n, 'frames')
