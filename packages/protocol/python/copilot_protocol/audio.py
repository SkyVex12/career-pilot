"""Binary audio frame header (``AudioFrameHeader`` in protocol.schema.json).

Layout: [0] channel uint8, [1] flags uint8, [2..5] frame_seq uint32 big-endian,
[6..9] capture_ms uint32 big-endian, then PCM audio.
"""

import struct

from copilot_protocol.models import AudioFrameHeader

HEADER = struct.Struct(">BBII")
HEADER_SIZE = HEADER.size  # 10
MAX_FRAME_BYTES = 64 * 1024

CHANNEL_THEM = 0
CHANNEL_ME = 1
FLAG_SPEECH_START = 0b01
FLAG_SPEECH_END = 0b10


def encode_frame(header: AudioFrameHeader, audio: bytes = b"") -> bytes:
    frame = HEADER.pack(header.channel, header.flags, header.frame_seq, header.capture_ms) + audio
    if len(frame) > MAX_FRAME_BYTES:
        raise ValueError(f"audio frame is {len(frame)} bytes; the limit is {MAX_FRAME_BYTES}")
    return frame


def decode_frame(frame: bytes) -> tuple[AudioFrameHeader, bytes]:
    """Split a binary frame into its validated header and audio payload."""
    if len(frame) < HEADER_SIZE:
        raise ValueError(f"audio frame is {len(frame)} bytes; the header alone is {HEADER_SIZE}")
    if len(frame) > MAX_FRAME_BYTES:
        raise ValueError(f"audio frame is {len(frame)} bytes; the limit is {MAX_FRAME_BYTES}")
    channel, flags, frame_seq, capture_ms = HEADER.unpack_from(frame)
    header = AudioFrameHeader.model_validate(
        {"channel": channel, "flags": flags, "frame_seq": frame_seq, "capture_ms": capture_ms}
    )
    return header, frame[HEADER_SIZE:]
