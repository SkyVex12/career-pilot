//! Realtime protocol types (ARCHITECTURE.md, section 1).
//!
//! `generated` comes from protocol.schema.json (`pnpm codegen`), like the Python and
//! TypeScript types; `audio` encodes and decodes binary audio frames. Parse incoming JSON with
//! [`parse_client`] or [`parse_server`], which check the schema's constraints as well as its
//! shape. Unknown fields are ignored, as the protocol requires.

pub mod audio;
#[rustfmt::skip]
#[allow(clippy::all)]
mod generated;
pub mod validate;

pub use generated::*;
pub use validate::{Validate, ValidationError};

#[derive(Debug, thiserror::Error)]
pub enum ParseError {
    #[error("malformed message: {0}")]
    Json(#[from] serde_json::Error),
    #[error("invalid message: {0}")]
    Invalid(#[from] ValidationError),
}

fn parse<T: serde::de::DeserializeOwned + Validate>(text: &str) -> Result<T, ParseError> {
    let message: T = serde_json::from_str(text)?;
    message.validate()?;
    Ok(message)
}

pub fn parse_client(text: &str) -> Result<ClientMessage, ParseError> {
    parse(text)
}

pub fn parse_server(text: &str) -> Result<ServerMessage, ParseError> {
    parse(text)
}

impl ClientMessage {
    /// Serialize for the wire. Validation first, so the client never sends what the gateway
    /// would reject.
    pub fn to_json(&self) -> Result<String, ParseError> {
        self.validate()?;
        Ok(serde_json::to_string(self)?)
    }
}
