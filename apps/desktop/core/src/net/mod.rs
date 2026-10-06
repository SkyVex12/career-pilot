//! The gateway connection: the client, its resend ring, reconnect backoff and close policy.

pub mod backoff;
pub mod client;
pub mod close;
pub mod ring;
