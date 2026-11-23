import { registerGlobals } from "@livekit/react-native";
// Native WebRTC and text/stream polyfills must exist before assistant modules load.
registerGlobals();
