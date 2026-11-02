import { Composition } from "remotion";
import { Intro, Outro } from "./Intro";

export const RemotionRoot: React.FC = () => {
  return (
    <>
      <Composition id="Intro" component={Intro} durationInFrames={95} fps={25} width={1920} height={1080} />
      <Composition id="Outro" component={Outro} durationInFrames={115} fps={25} width={1920} height={1080} />
    </>
  );
};
