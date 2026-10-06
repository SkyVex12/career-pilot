//! Binary audio frame header (`AudioFrameHeader` in protocol.schema.json).
//!
//! Layout: [0] channel uint8, [1] flags uint8, [2..5] frame_seq uint32 big-endian,
//! [6..9] capture_ms uint32 big-endian, then PCM audio.

use crate::AudioFrameHeader;
use crate::validate::{Validate, ValidationError};

pub const HEADER_SIZE: usize = 10;
pub const MAX_FRAME_BYTES: usize = 64 * 1024;

pub const CHANNEL_THEM: u8 = 0;
pub const CHANNEL_ME: u8 = 1;
pub const FLAG_SPEECH_START: u8 = 0b01;
pub const FLAG_SPEECH_END: u8 = 0b10;

#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub enum FrameError {
    #[error("audio frame is {0} bytes; the header alone is {HEADER_SIZE}")]
    TooShort(usize),
    #[error("audio frame is {0} bytes; the limit is {MAX_FRAME_BYTES}")]
    TooLarge(usize),
    #[error("invalid audio frame header: {0}")]
    Header(#[from] ValidationError),
}

pub fn encode_frame(header: &AudioFrameHeader, audio: &[u8]) -> Result<Vec<u8>, FrameError> {
    header.validate()?;
    let len = HEADER_SIZE + audio.len();
    if len > MAX_FRAME_BYTES {
        return Err(FrameError::TooLarge(len));
    }
    let mut frame = Vec::with_capacity(len);
    frame.push(header.channel);
    frame.push(header.flags);
    frame.extend_from_slice(&header.frame_seq.to_be_bytes());
    frame.extend_from_slice(&header.capture_ms.to_be_bytes());
    frame.extend_from_slice(audio);
    Ok(frame)
}

/// Split a binary frame into its validated header and audio payload.
pub fn decode_frame(frame: &[u8]) -> Result<(AudioFrameHeader, &[u8]), FrameError> {
    if frame.len() < HEADER_SIZE {
        return Err(FrameError::TooShort(frame.len()));
    }
    if frame.len() > MAX_FRAME_BYTES {
        return Err(FrameError::TooLarge(frame.len()));
    }
    let header = AudioFrameHeader {
        channel: frame[0],
        flags: frame[1],
        frame_seq: u32::from_be_bytes([frame[2], frame[3], frame[4], frame[5]]),
        capture_ms: u32::from_be_bytes([frame[6], frame[7], frame[8], frame[9]]),
    };
    header.validate()?;
    Ok((header, &frame[HEADER_SIZE..]))
}
