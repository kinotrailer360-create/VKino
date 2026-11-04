import React from "react";
import { AbsoluteFill, Audio, Img, interpolate, Sequence, spring, staticFile, useCurrentFrame, useVideoConfig } from "remotion";
import { CYAN, ensureFonts, GOLD, INK } from "./Fonts";

ensureFonts();

const clamp = { extrapolateLeft: "clamp", extrapolateRight: "clamp" } as const;

// фон: тёмный, мягкое свечение в центре, редкие золотые искры
const Background: React.FC = () => {
  const frame = useCurrentFrame();
  const glow = interpolate(frame, [0, 30, 80], [0, 0.9, 0.7], clamp);
  return (
    <AbsoluteFill style={{ backgroundColor: INK }}>
      <AbsoluteFill
        style={{
          background: `radial-gradient(circle at 50% 42%, rgba(62,198,240,${0.22 * glow}) 0%, rgba(255,184,28,${0.10 * glow}) 28%, rgba(0,0,0,0) 60%)`,
        }}
      />
      {Array.from({ length: 36 }).map((_, i) => {
        const x = (i * 137.5) % 100;
        const y0 = (i * 61.8) % 100;
        const speed = 0.08 + (i % 5) * 0.03;
        const y = (y0 - frame * speed + 100) % 100;
        const tw = 0.25 + 0.75 * Math.abs(Math.sin(frame / 9 + i));
        return (
          <div
            key={i}
            style={{
              position: "absolute",
              left: `${x}%`,
              top: `${y}%`,
              width: 3 + (i % 3) * 2,
              height: 3 + (i % 3) * 2,
              borderRadius: "50%",
              background: i % 4 === 0 ? CYAN : GOLD,
              opacity: tw * glow * 0.7,
              boxShadow: `0 0 12px ${i % 4 === 0 ? CYAN : GOLD}`,
            }}
          />
        );
      })}
    </AbsoluteFill>
  );
};

// две золотые линии разъезжаются от центра, как шторки
const Lines: React.FC<{ y: number }> = ({ y }) => {
  const frame = useCurrentFrame();
  const w = interpolate(frame, [4, 26], [0, 1400], { ...clamp, easing: (t) => 1 - Math.pow(1 - t, 3) });
  const o = interpolate(frame, [4, 12, 70, 84], [0, 1, 1, 0.6], clamp);
  return (
    <>
      {[-1, 1].map((s) => (
        <div
          key={s}
          style={{
            position: "absolute",
            top: y + s * 6,
            left: 960 - w / 2,
            width: w,
            height: 2,
            opacity: o,
            background: `linear-gradient(90deg, rgba(255,184,28,0), ${GOLD}, rgba(255,184,28,0))`,
          }}
        />
      ))}
    </>
  );
};

export const Intro: React.FC = () => {
  const frame = useCurrentFrame();
  const { fps, durationInFrames } = useVideoConfig();
  const land = spring({ frame: frame - 10, fps, config: { damping: 12, stiffness: 110, mass: 0.9 } });
  const scale = interpolate(land, [0, 1], [2.4, 1]);
  const logoOpacity = interpolate(frame, [10, 18], [0, 1], clamp);
  const rotate = interpolate(land, [0, 1], [-14, 0]);
  const pulse = 1 + 0.015 * Math.sin(frame / 5);
  // блик пробегает по логотипу
  const sweep = interpolate(frame, [30, 52], [-120, 120], clamp);
  const title = spring({ frame: frame - 34, fps, config: { damping: 200 } });
  const sub = spring({ frame: frame - 46, fps, config: { damping: 200 } });
  const out = interpolate(frame, [durationInFrames - 10, durationInFrames], [1, 0], clamp);
  const zoomOut = interpolate(frame, [durationInFrames - 10, durationInFrames], [1, 1.08], clamp);
  return (
    <AbsoluteFill style={{ opacity: out, transform: `scale(${zoomOut})` }}>
      <Background />
      <Lines y={760} />
      <AbsoluteFill style={{ alignItems: "center", top: 120 }}>
        <div style={{ position: "relative", width: 640, height: 484, transform: `scale(${scale * pulse}) rotate(${rotate}deg)`, opacity: logoOpacity }}>
          <Img src={staticFile("logo.png")} style={{ width: 640, height: 484, filter: `drop-shadow(0 0 ${30 * land}px rgba(255,184,28,0.55))` }} />
          <div
            style={{
              position: "absolute",
              inset: 0,
              WebkitMaskImage: `url(${staticFile("logo.png")})`,
              WebkitMaskSize: "100% 100%",
              background: `linear-gradient(105deg, rgba(255,255,255,0) ${40 + sweep / 3}%, rgba(255,255,255,0.75) ${50 + sweep / 3}%, rgba(255,255,255,0) ${60 + sweep / 3}%)`,
              mixBlendMode: "screen",
            }}
          />
        </div>
      </AbsoluteFill>
      <div
        style={{
          position: "absolute",
          top: 700,
          width: "100%",
          textAlign: "center",
          fontFamily: "Oswald",
          fontWeight: 700,
          fontSize: 120,
          color: "white",
          letterSpacing: interpolate(title, [0, 1], [60, 14]),
          opacity: title,
          transform: `translateY(${interpolate(title, [0, 1], [30, 0])}px)`,
          textShadow: "0 6px 30px rgba(0,0,0,0.8)",
        }}
      >
        НОВОСТИ <span style={{ color: GOLD }}>КИНО</span>
      </div>
      <div
        style={{
          position: "absolute",
          top: 860,
          width: "100%",
          textAlign: "center",
          fontFamily: "Montserrat",
          fontWeight: 700,
          fontSize: 44,
          letterSpacing: 12,
          color: CYAN,
          opacity: sub,
          transform: `translateY(${interpolate(sub, [0, 1], [20, 0])}px)`,
        }}
      >
        ПРЕМЬЕРЫ · ТРЕЙЛЕРЫ · СЛУХИ
      </div>
      <Sequence from={6}>
        <Audio src={staticFile("whoosh.wav")} volume={0.6} />
      </Sequence>
      <Sequence from={18}>
        <Audio src={staticFile("boom.wav")} volume={0.7} />
      </Sequence>
    </AbsoluteFill>
  );
};

export const Outro: React.FC = () => {
  const frame = useCurrentFrame();
  const { fps, durationInFrames } = useVideoConfig();
  const logo = spring({ frame, fps, config: { damping: 14 } });
  const t1 = spring({ frame: frame - 10, fps, config: { damping: 200 } });
  const t2 = spring({ frame: frame - 20, fps, config: { damping: 200 } });
  const btn = spring({ frame: frame - 30, fps, config: { damping: 9, stiffness: 140 } });
  const click = interpolate(frame, [62, 66, 72], [1, 0.9, 1], clamp);
  const subscribed = frame >= 66;
  const out = interpolate(frame, [durationInFrames - 8, durationInFrames], [1, 0], clamp);
  return (
    <AbsoluteFill style={{ opacity: out }}>
      <Background />
      <div style={{ position: "absolute", left: 170, top: 300, transform: `scale(${logo})`, opacity: logo }}>
        <Img src={staticFile("logo.png")} style={{ width: 560, height: 423, filter: "drop-shadow(0 0 28px rgba(255,184,28,0.5))" }} />
      </div>
      <div style={{ position: "absolute", left: 860, top: 250, width: 920 }}>
        <div style={{ fontFamily: "Oswald", fontWeight: 700, fontSize: 132, color: "white", lineHeight: 1.05, opacity: t1, transform: `translateX(${interpolate(t1, [0, 1], [60, 0])}px)` }}>
          СПАСИБО,<br />ЧТО <span style={{ color: GOLD }}>СМОТРИТЕ!</span>
        </div>
        <div style={{ marginTop: 26, fontFamily: "Montserrat", fontWeight: 600, fontSize: 44, color: "#E8E8E8", opacity: t2, transform: `translateX(${interpolate(t2, [0, 1], [60, 0])}px)` }}>
          Новые выпуски — по средам и воскресеньям
        </div>
        <div
          style={{
            marginTop: 48,
            display: "inline-flex",
            alignItems: "center",
            gap: 20,
            padding: "22px 46px",
            borderRadius: 60,
            background: subscribed ? "#2A2A2E" : "#E62117",
            color: "white",
            fontFamily: "Montserrat",
            fontWeight: 800,
            fontSize: 46,
            letterSpacing: 2,
            transform: `scale(${btn * click})`,
            boxShadow: subscribed ? "none" : "0 0 40px rgba(230,33,23,0.6)",
          }}
        >
          {subscribed ? "✓ ВЫ ПОДПИСАНЫ" : "ПОДПИСАТЬСЯ"}
        </div>
        <div style={{ marginTop: 40, fontFamily: "Montserrat", fontWeight: 600, fontSize: 36, color: CYAN, opacity: t2 }}>
          YouTube @kinomir360 · Telegram @KinoMir360
        </div>
      </div>
      <Sequence from={64}>
        <Audio src={staticFile("whoosh.wav")} volume={0.4} />
      </Sequence>
    </AbsoluteFill>
  );
};
