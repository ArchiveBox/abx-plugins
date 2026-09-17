// Copyright (c) 2018-2019 Parity Technologies (UK) Ltd.
// Modifications Copyright (c) 2026 TLSNotary
//
// Licensed under the Apache License, Version 2.0 or MIT license, at your
// option.
//
// A copy of the Apache License, Version 2.0 is included in the software as
// LICENSE-APACHE and a copy of the MIT license is included in the software
// as LICENSE-MIT. You may also obtain a copy of the Apache License, Version 2.0
// at https://www.apache.org/licenses/LICENSE-2.0 and a copy of the MIT license
// at https://opensource.org/licenses/MIT.

use crate::frame::FrameDecodeError;

/// The various error cases a connection may encounter.
#[non_exhaustive]
#[derive(Debug)]
pub enum ConnectionError {
    /// An underlying I/O error occurred.
    Io(std::io::Error),
    /// Decoding a message frame failed.
    Decode(FrameDecodeError),
    /// An operation fails because the connection is closed.
    Closed,
    /// The peer terminated the connection reporting a protocol error: we sent
    /// something it does not allow.
    PeerProtocolError,
    /// The peer terminated the connection reporting an internal error of its
    /// own.
    PeerInternalError,
    /// The peer terminated the connection reporting a code this version does
    /// not know. Not a graceful close, but the cause cannot be named.
    PeerError(u32),
    /// Too many streams are open, so no further ones can be opened at this
    /// time.
    TooManyStreams,
    /// User ID exceeds maximum length (256 bytes).
    InvalidUserIdLength,
    /// Stream ID is already in use by another stream.
    DuplicateStreamId,
}

impl std::fmt::Display for ConnectionError {
    fn fmt(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
        match self {
            ConnectionError::Io(e) => write!(f, "i/o error: {e}"),
            ConnectionError::Decode(e) => write!(f, "decode error: {e}"),
            ConnectionError::Closed => f.write_str("connection is closed"),
            ConnectionError::PeerProtocolError => {
                f.write_str("peer terminated the connection: protocol error")
            }
            ConnectionError::PeerInternalError => {
                f.write_str("peer terminated the connection: internal error")
            }
            ConnectionError::PeerError(code) => {
                write!(f, "peer terminated the connection: unknown code {code}")
            }
            ConnectionError::TooManyStreams => f.write_str("maximum number of streams reached"),
            ConnectionError::InvalidUserIdLength => f.write_str("user ID exceeds maximum length"),
            ConnectionError::DuplicateStreamId => f.write_str("duplicate stream ID"),
        }
    }
}

impl std::error::Error for ConnectionError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            ConnectionError::Io(e) => Some(e),
            ConnectionError::Decode(e) => Some(e),
            ConnectionError::Closed
            | ConnectionError::PeerProtocolError
            | ConnectionError::PeerInternalError
            | ConnectionError::PeerError(_)
            | ConnectionError::TooManyStreams
            | ConnectionError::InvalidUserIdLength
            | ConnectionError::DuplicateStreamId => None,
        }
    }
}

impl From<std::io::Error> for ConnectionError {
    fn from(e: std::io::Error) -> Self {
        ConnectionError::Io(e)
    }
}

impl From<FrameDecodeError> for ConnectionError {
    fn from(e: FrameDecodeError) -> Self {
        ConnectionError::Decode(e)
    }
}

impl From<futures::channel::mpsc::SendError> for ConnectionError {
    fn from(_: futures::channel::mpsc::SendError) -> Self {
        ConnectionError::Closed
    }
}

impl From<futures::channel::oneshot::Canceled> for ConnectionError {
    fn from(_: futures::channel::oneshot::Canceled) -> Self {
        ConnectionError::Closed
    }
}
