// Copyright (c) 2026 TLSNotary
//
// Licensed under the Apache License, Version 2.0 or MIT license, at your
// option.

//! Payload counters for a connection.
//!
//! These are the probe points a caller needs to tell a wedged connection from
//! a busy one. They carry no policy: nothing here decides what "stalled"
//! means, times anything, or acts. A caller samples [`Traffic`] on its own
//! schedule and compares two samples; if the payload counters have not moved
//! while it is waiting for data, its peer has stopped producing.
//!
//! Only payload counts. Control traffic — pings, window updates — moves
//! nothing, so `keep_alive` cannot make a stalled connection look busy, and
//! neither can a bodyless frame: a stream opened by an empty write or closed
//! by a FIN is activity, not progress.

use std::sync::atomic::{AtomicU64, Ordering};

/// A snapshot of a connection's payload counters.
///
/// All counters are cumulative for the life of the connection and never
/// reset, so only the difference between two snapshots is meaningful.
#[derive(Debug, Default, Clone, Copy, PartialEq, Eq)]
#[non_exhaustive]
pub struct Traffic {
    /// Payload bytes delivered to streams, ready for the application to read.
    pub data_bytes_in: u64,
    /// Payload bytes handed to the socket.
    pub data_bytes_out: u64,
}

/// The live counters behind [`Traffic`].
///
/// Only the connection's driver writes these, and a sample that races with a
/// write simply belongs to one side of it, so `Relaxed` ordering is enough.
#[derive(Debug, Default)]
pub(crate) struct Counters {
    data_bytes_in: AtomicU64,
    data_bytes_out: AtomicU64,
}

impl Counters {
    /// Record payload delivered to a stream.
    pub(crate) fn record_in(&self, body_len: u32) {
        self.data_bytes_in
            .fetch_add(u64::from(body_len), Ordering::Relaxed);
    }

    /// Record payload handed to the socket.
    pub(crate) fn record_out(&self, body_len: u32) {
        self.data_bytes_out
            .fetch_add(u64::from(body_len), Ordering::Relaxed);
    }

    pub(crate) fn snapshot(&self) -> Traffic {
        Traffic {
            data_bytes_in: self.data_bytes_in.load(Ordering::Relaxed),
            data_bytes_out: self.data_bytes_out.load(Ordering::Relaxed),
        }
    }
}
