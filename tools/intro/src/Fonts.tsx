import { continueRender, delayRender, staticFile } from "remotion";

let loaded = false;
export const ensureFonts = () => {
  if (loaded || typeof document === "undefined") return;
  loaded = true;
  const handle = delayRender("fonts");
  const faces = [
    new FontFace("Oswald", `url(${staticFile("Oswald.ttf")})`, { weight: "200 700" }),
    new FontFace("Montserrat", `url(${staticFile("Montserrat.ttf")})`, { weight: "100 900" }),
  ];
  Promise.all(faces.map((f) => f.load()))
    .then((list) => {
      list.forEach((f) => document.fonts.add(f));
      continueRender(handle);
    })
    .catch(() => continueRender(handle));
};

export const GOLD = "#FFB81C";
export const CYAN = "#3EC6F0";
export const INK = "#07080C";
