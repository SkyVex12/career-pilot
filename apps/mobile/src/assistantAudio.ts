import { AudioSession } from "@livekit/react-native";
import { AssistantAudioLease } from "./assistantAudioLease";
export const assistantAudio = new AssistantAudioLease(
  () => AudioSession.startAudioSession(),
  () => AudioSession.stopAudioSession(),
);
