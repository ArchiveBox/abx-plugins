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

//! This module contains the `Connection` type and associated helpers.
//! A `Connection` wraps an underlying (async) I/O resource and multiplexes
//! `Stream`s over it.

mod active;
mod cleanup;
mod closing;
mod rtt;
mod stream;
mod user_id;

pub(crate) use user_id::UserId;

use crate::{Config, Result, error::ConnectionError};
use active::Active;
use cleanup::Cleanup;
use closing::Closing;
use futures::prelude::*;
use std::{
    fmt,
    task::{Context, Poll},
};

pub use active::Handle;
pub(crate) use active::{Action, StreamCommand, StreamRegistry};
pub use stream::Stream;

/// The connection identifier.
///
/// Randomly generated, this is mainly intended to improve log output.
#[derive(Clone, Copy)]
pub(crate) struct Id(u32);

impl Id {
    /// Create a random connection ID.
    pub(crate) fn random() -> Self {
        Id(rand::random())
    }
}

impl fmt::Debug for Id {
    fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result {
        write!(f, "{:08x}", self.0)
    }
}

impl fmt::Display for Id {
    fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result {
        write!(f, "{:08x}", self.0)
    }
}

/// A multiplexer connection object.
///
/// Wraps the underlying I/O resource and makes progress via its
/// [`Connection::poll`] method which must be called repeatedly
/// until `Ok(())` signals close or an error is encountered.
#[derive(Debug)]
pub struct Connection<T> {
    inner: ConnectionState<T>,
}

impl<T: AsyncRead + AsyncWrite + Unpin> Connection<T> {
    pub fn new(socket: T, cfg: Config) -> Self {
        Self {
            inner: ConnectionState::Active(Active::new(socket, cfg)),
        }
    }

    /// Returns `true` if the connection is complete.
    pub fn is_complete(&self) -> bool {
        matches!(self.inner, ConnectionState::Closed(_))
    }

    /// Get a handle for creating streams concurrently.
    ///
    /// The handle can be cloned and used from multiple tasks while the
    /// Connection is being polled.
    pub fn handle(&self) -> Result<Handle> {
        match &self.inner {
            ConnectionState::Active(active) => Ok(active.handle()),
            _ => Err(ConnectionError::Closed),
        }
    }

    /// Create a new stream with the given user ID.
    ///
    /// The stream ID is computed from the user ID using BLAKE3.
    /// Either side can create streams with the same user ID - they will
    /// automatically merge into the same stream.
    ///
    /// The `user_id` parameter is a required user-defined stream identifier
    /// (1-256 bytes). User IDs must be unique within the session.
    pub fn new_stream(&mut self, user_id: &[u8]) -> Result<Stream> {
        match &mut self.inner {
            ConnectionState::Active(active) => active.new_stream(user_id),
            _ => Err(ConnectionError::Closed),
        }
    }

    /// Initiate connection close.
    ///
    /// This transitions the connection to the Closing state.
    /// Continue calling `poll` to complete the close handshake.
    pub fn close(&mut self) {
        match std::mem::replace(&mut self.inner, ConnectionState::Poisoned) {
            ConnectionState::Active(active) => {
                self.inner = ConnectionState::Closing(active.close());
            }
            other => {
                self.inner = other;
            }
        }
    }

    /// Poll the connection.
    ///
    /// This drives the connection state machine, handling I/O and stream
    /// commands.
    ///
    /// Returns:
    /// - `Poll::Ready(Ok(()))` when the connection is closed gracefully
    /// - `Poll::Ready(Err(e))` on connection error
    /// - `Poll::Pending` when waiting for I/O
    pub fn poll(&mut self, cx: &mut Context<'_>) -> Poll<Result<()>> {
        loop {
            match std::mem::replace(&mut self.inner, ConnectionState::Poisoned) {
                ConnectionState::Active(mut active) => match active.poll(cx) {
                    Poll::Ready(Ok(())) => {
                        // This shouldn't happen in normal operation
                        self.inner = ConnectionState::Active(active);
                        return Poll::Pending;
                    }
                    Poll::Ready(Err(ConnectionError::Closed)) if active.config.close_sync => {
                        // Remote sent GoAway with close_sync enabled.
                        // Send our GoAway reply via Closing (no wait).
                        self.inner = ConnectionState::Closing(active.close_no_wait());
                        continue;
                    }
                    Poll::Ready(Err(e)) => {
                        self.inner = ConnectionState::Cleanup(active.cleanup(e));
                        continue;
                    }
                    Poll::Pending => {
                        self.inner = ConnectionState::Active(active);
                        return Poll::Pending;
                    }
                },
                ConnectionState::Closing(mut closing) => match closing.poll_unpin(cx) {
                    Poll::Ready(Ok(io)) => {
                        self.inner = ConnectionState::Closed(Some(io));
                        return Poll::Ready(Ok(()));
                    }
                    Poll::Ready(Err(e)) => {
                        self.inner = ConnectionState::Closed(None);
                        return Poll::Ready(Err(e));
                    }
                    Poll::Pending => {
                        self.inner = ConnectionState::Closing(closing);
                        return Poll::Pending;
                    }
                },
                ConnectionState::Cleanup(mut cleanup) => match cleanup.poll_unpin(cx) {
                    Poll::Ready(ConnectionError::Closed) => {
                        self.inner = ConnectionState::Closed(None);
                        return Poll::Ready(Ok(()));
                    }
                    Poll::Ready(other) => {
                        self.inner = ConnectionState::Closed(None);
                        return Poll::Ready(Err(other));
                    }
                    Poll::Pending => {
                        self.inner = ConnectionState::Cleanup(cleanup);
                        return Poll::Pending;
                    }
                },
                ConnectionState::Closed(io) => {
                    self.inner = ConnectionState::Closed(io);
                    return Poll::Ready(Ok(()));
                }
                ConnectionState::Poisoned => unreachable!(),
            }
        }
    }

    /// Returns the underlying IO if the connection is closed.
    ///
    /// Returns `Err(self)` if the connection is not in the closed state
    /// or if the IO is not available (e.g., after an error cleanup).
    #[allow(clippy::result_large_err)]
    pub fn try_into_io(mut self) -> std::result::Result<T, Self> {
        match &mut self.inner {
            ConnectionState::Closed(io) => io.take().ok_or(self),
            _ => Err(self),
        }
    }
}

impl<T> Drop for Connection<T> {
    fn drop(&mut self) {
        match &mut self.inner {
            ConnectionState::Active(active) => active.drop_all_streams(),
            ConnectionState::Closing(_) => {}
            ConnectionState::Cleanup(_) => {}
            ConnectionState::Closed(_) => {}
            ConnectionState::Poisoned => {}
        }
    }
}

#[allow(clippy::large_enum_variant)]
enum ConnectionState<T> {
    /// The connection is alive and healthy.
    Active(Active<T>),
    /// Our user requested to shutdown the connection, we are working on it.
    Closing(Closing<T>),
    /// An error occurred and we are cleaning up our resources.
    Cleanup(Cleanup),
    /// The connection is closed. Contains the IO if available.
    Closed(Option<T>),
    /// Something went wrong during our state transitions. Should never happen
    /// unless there is a bug.
    Poisoned,
}

impl<T> fmt::Debug for ConnectionState<T> {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            ConnectionState::Active(_) => write!(f, "Active"),
            ConnectionState::Closing(_) => write!(f, "Closing"),
            ConnectionState::Cleanup(_) => write!(f, "Cleanup"),
            ConnectionState::Closed(_) => write!(f, "Closed"),
            ConnectionState::Poisoned => write!(f, "Poisoned"),
        }
    }
}
