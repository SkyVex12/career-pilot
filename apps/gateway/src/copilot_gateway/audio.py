"""Audio frame parsing with per-channel dedupe by frame_seq (ARCHITECTURE.md, "Audio frames").

Frames of one channel arrive in frame_seq order over a socket, and after a reconnect the client
resends its buffer in order, so "already seen" is everything up to the highest frame_seq
accepted. The high-water marks live in the session state, so a resume on another gateway task
also drops what this one already had.
"""

from dataclasses import dataclass

from copilot_protocol.audio import (
    CHANNEL_ME,
    CHANNEL_THEM,
    FLAG_SPEECH_END,
    FLAG_SPEECH_START,
    decode_frame,
)


@dataclass(frozen=True)
class Frame:
    channel: str  # "them" or "me"
    speech_start: bool
    speech_end: bool
    frame_seq: int
    capture_ms: int
    audio: bytes


def parse(data: bytes) -> Frame:
    """Decode a binary frame; raises ValueError on a malformed one."""
    header, audio = decode_frame(data)
    if header.channel == CHANNEL_ME and audio:
        raise ValueError("the me channel carries speech flags only, no audio")
    if len(audio) % 2:
        raise ValueError("pcm16 audio must have an even number of bytes")
    return Frame(
        channel="them" if header.channel == CHANNEL_THEM else "me",
        speech_start=bool(header.flags & FLAG_SPEECH_START),
        speech_end=bool(header.flags & FLAG_SPEECH_END),
        frame_seq=header.frame_seq,
        capture_ms=header.capture_ms,
        audio=audio,
    )


class FrameDedupe:
    def __init__(self, high_water: dict[str, int] | None = None) -> None:
        self.high_water: dict[str, int] = {"them": -1, "me": -1}
        if high_water:
            self.high_water.update(high_water)
        self.dropped = 0

    def accept(self, frame: Frame) -> bool:
        if frame.frame_seq <= self.high_water[frame.channel]:
            self.dropped += 1
            return False
        self.high_water[frame.channel] = frame.frame_seq
        return True
