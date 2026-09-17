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

use futures::future::Either;
use std::{fmt, hash::Hash};

/// The message frame header.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Header<T> {
    tag: Tag,
    flags: Flags,
    stream_id: StreamId,
    length: Len,
    _marker: std::marker::PhantomData<T>,
}

impl<T> fmt::Display for Header<T> {
    fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result {
        write!(
            f,
            "(Header {:?} {} (len {}) (flags {:?}))",
            self.tag,
            self.stream_id,
            self.length.val(),
            self.flags.val()
        )
    }
}

impl<T> Header<T> {
    pub fn tag(&self) -> Tag {
        self.tag
    }

    pub fn flags(&self) -> Flags {
        self.flags
    }

    pub fn stream_id(&self) -> StreamId {
        self.stream_id
    }

    pub fn len(&self) -> Len {
        self.length
    }

    #[cfg(test)]
    pub fn set_len(&mut self, len: u32) {
        self.length = Len(len)
    }

    /// Arbitrary type cast, use with caution.
    fn cast<U>(self) -> Header<U> {
        Header {
            tag: self.tag,
            flags: self.flags,
            stream_id: self.stream_id,
            length: self.length,
            _marker: std::marker::PhantomData,
        }
    }
}

impl<A: private::Sealed> From<Header<A>> for Header<()> {
    fn from(h: Header<A>) -> Header<()> {
        h.cast()
    }
}

impl Header<()> {
    pub(crate) fn into_data(self) -> Header<Data> {
        debug_assert_eq!(self.tag, Tag::Data);
        self.cast()
    }

    pub(crate) fn into_window_update(self) -> Header<WindowUpdate> {
        debug_assert_eq!(self.tag, Tag::WindowUpdate);
        self.cast()
    }

    pub(crate) fn into_ping(self) -> Header<Ping> {
        debug_assert_eq!(self.tag, Tag::Ping);
        self.cast()
    }
}

impl<T: HasSyn> Header<T> {
    /// Set the [`SYN`] flag.
    pub fn syn(&mut self) {
        self.flags.0 |= SYN.0
    }
}

impl<T: HasAck> Header<T> {
    /// Set the [`ACK`] flag.
    pub fn ack(&mut self) {
        self.flags.0 |= ACK.0
    }
}

impl<T: HasFin> Header<T> {
    /// Set the [`FIN`] flag.
    pub fn fin(&mut self) {
        self.flags.0 |= FIN.0
    }
}

impl<T: HasRst> Header<T> {
    /// Set the [`RST`] flag.
    pub fn rst(&mut self) {
        self.flags.0 |= RST.0
    }
}

impl Header<Data> {
    /// Create a new data frame header.
    pub fn data(id: StreamId, len: u32) -> Self {
        Header {
            tag: Tag::Data,
            flags: Flags(0),
            stream_id: id,
            length: Len(len),
            _marker: std::marker::PhantomData,
        }
    }
}

impl Header<WindowUpdate> {
    /// Create a new window update frame header.
    pub fn window_update(id: StreamId, credit: u32) -> Self {
        Header {
            tag: Tag::WindowUpdate,
            flags: Flags(0),
            stream_id: id,
            length: Len(credit),
            _marker: std::marker::PhantomData,
        }
    }

    /// The credit this window update grants to the remote.
    pub fn credit(&self) -> u32 {
        self.length.0
    }
}

impl Header<Ping> {
    /// Create a new ping frame header.
    pub fn ping(nonce: u32) -> Self {
        Header {
            tag: Tag::Ping,
            flags: Flags(0),
            stream_id: CONNECTION_ID,
            length: Len(nonce),
            _marker: std::marker::PhantomData,
        }
    }

    /// The nonce of this ping.
    pub fn nonce(&self) -> u32 {
        self.length.0
    }
}

impl Header<GoAway> {
    /// Terminate the session without indicating an error to the remote.
    pub fn term() -> Self {
        Self::go_away(0)
    }

    /// Terminate the session indicating a protocol error to the remote.
    pub fn protocol_error() -> Self {
        Self::go_away(1)
    }

    /// Terminate the session indicating an internal error to the remote.
    // No sender today: the stream limit, its last user, reports a protocol
    // error instead. Kept as the encoding counterpart of the code we decode.
    #[allow(dead_code)]
    pub fn internal_error() -> Self {
        Self::go_away(2)
    }

    fn go_away(code: u32) -> Self {
        Header {
            tag: Tag::GoAway,
            flags: Flags(0),
            stream_id: CONNECTION_ID,
            length: Len(code),
            _marker: std::marker::PhantomData,
        }
    }
}

/// Data message type.
#[derive(Clone, Debug)]
pub enum Data {}

/// Window update message type.
#[derive(Clone, Debug)]
pub enum WindowUpdate {}

/// Ping message type.
#[derive(Clone, Debug)]
pub enum Ping {}

/// Go Away message type.
#[derive(Clone, Debug)]
pub enum GoAway {}

/// Types which have an `ack` method.
pub trait HasAck: private::Sealed {}
impl HasAck for Ping {}
impl<A: HasAck, B: HasAck> HasAck for Either<A, B> {}

/// Types which have a [`SYN`] flag: a ping request, or the frame that opens a
/// stream.
pub trait HasSyn: private::Sealed {}
impl HasSyn for Data {}
impl HasSyn for Ping {}

/// Types which have a `fin` method.
pub trait HasFin: private::Sealed {}
impl HasFin for Data {}

/// Types which have a `rst` method.
pub trait HasRst: private::Sealed {}
impl HasRst for Data {}

pub(super) mod private {
    pub trait Sealed {}

    impl Sealed for super::Data {}
    impl Sealed for super::WindowUpdate {}
    impl Sealed for super::Ping {}
    impl Sealed for super::GoAway {}
    impl<A: Sealed, B: Sealed> Sealed for super::Either<A, B> {}
}

/// A tag is the runtime representation of a message type.
#[derive(Copy, Clone, Debug, PartialEq, Eq)]
pub enum Tag {
    Data,
    WindowUpdate,
    Ping,
    GoAway,
}

/// The message length.
#[derive(Copy, Clone, Debug, PartialEq, Eq)]
pub struct Len(u32);

impl Len {
    pub fn val(self) -> u32 {
        self.0
    }
}

pub const CONNECTION_ID: StreamId = StreamId([0u8; 8]);

/// The ID of a stream.
///
/// Stream IDs are derived from user-defined identifiers using BLAKE3.
/// The value `[0u8; 8]` denotes no particular stream but the whole session.
#[derive(Copy, Clone, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub struct StreamId([u8; 8]);

impl StreamId {
    /// Create a new stream ID from a user-defined identifier.
    pub fn new(id: &[u8]) -> Self {
        let hash = blake3::hash(id);
        let bytes: [u8; 8] = hash.as_bytes()[..8]
            .try_into()
            .expect("hash is at least 8 bytes");
        StreamId(bytes)
    }

    pub fn is_session(self) -> bool {
        self == CONNECTION_ID
    }

    pub fn as_bytes(&self) -> &[u8; 8] {
        &self.0
    }
}

impl fmt::Display for StreamId {
    fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result {
        write!(
            f,
            "{:02x}{:02x}{:02x}{:02x}{:02x}{:02x}{:02x}{:02x}",
            self.0[0], self.0[1], self.0[2], self.0[3], self.0[4], self.0[5], self.0[6], self.0[7]
        )
    }
}

impl Hash for StreamId {
    fn hash<H: std::hash::Hasher>(&self, state: &mut H) {
        u64::from_le_bytes(self.0).hash(state);
    }
}

impl nohash_hasher::IsEnabled for StreamId {}

/// Possible flags set on a message.
#[derive(Copy, Clone, Debug, PartialEq, Eq)]
pub struct Flags(u8);

impl Flags {
    pub fn contains(self, other: Flags) -> bool {
        self.0 & other.0 == other.0
    }

    pub fn val(self) -> u8 {
        self.0
    }
}

/// Indicates the half-closing of a stream.
pub const FIN: Flags = Flags(0x01);

/// Indicates an immediate stream reset.
pub const RST: Flags = Flags(0x02);

/// Indicates a ping request.
pub const SYN: Flags = Flags(0x04);

/// Indicates a ping response.
pub const ACK: Flags = Flags(0x08);

/// The serialised header size in bytes.
pub const HEADER_SIZE: usize = 14;

/// Encode a [`Header`] value.
pub fn encode<T>(hdr: &Header<T>) -> [u8; HEADER_SIZE] {
    let mut buf = [0; HEADER_SIZE];
    buf[0] = hdr.tag as u8;
    buf[1] = hdr.flags.0;
    buf[2..6].copy_from_slice(&hdr.length.0.to_be_bytes());
    buf[6..14].copy_from_slice(&hdr.stream_id.0);
    buf
}

/// Decode a [`Header`] value.
pub fn decode(buf: &[u8; HEADER_SIZE]) -> Result<Header<()>, HeaderDecodeError> {
    let hdr = Header {
        tag: match buf[0] {
            0 => Tag::Data,
            1 => Tag::WindowUpdate,
            2 => Tag::Ping,
            3 => Tag::GoAway,
            t => return Err(HeaderDecodeError::Type(t)),
        },
        flags: Flags(buf[1]),
        length: Len(u32::from_be_bytes([buf[2], buf[3], buf[4], buf[5]])),
        stream_id: StreamId([
            buf[6], buf[7], buf[8], buf[9], buf[10], buf[11], buf[12], buf[13],
        ]),
        _marker: std::marker::PhantomData,
    };

    Ok(hdr)
}

/// Possible errors while decoding a message frame header.
#[non_exhaustive]
#[derive(Debug)]
pub enum HeaderDecodeError {
    /// An unknown frame type.
    Type(u8),
}

impl std::fmt::Display for HeaderDecodeError {
    fn fmt(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
        match self {
            HeaderDecodeError::Type(t) => write!(f, "unknown frame type: {t}"),
        }
    }
}

impl std::error::Error for HeaderDecodeError {}

#[cfg(test)]
mod tests {
    use super::*;
    use quickcheck::{Arbitrary, Gen, QuickCheck};

    impl Arbitrary for Header<()> {
        fn arbitrary(g: &mut Gen) -> Self {
            let tag = *g
                .choose(&[Tag::Data, Tag::WindowUpdate, Tag::Ping, Tag::GoAway])
                .unwrap();

            let mut stream_id_bytes = [0u8; 8];
            for byte in &mut stream_id_bytes {
                *byte = Arbitrary::arbitrary(g);
            }

            Header {
                tag,
                flags: Flags(Arbitrary::arbitrary(g)),
                stream_id: StreamId(stream_id_bytes),
                length: Len(Arbitrary::arbitrary(g)),
                _marker: std::marker::PhantomData,
            }
        }
    }

    #[test]
    fn encode_decode_identity() {
        fn property(hdr: Header<()>) -> bool {
            match decode(&encode(&hdr)) {
                Ok(x) => x == hdr,
                Err(e) => {
                    eprintln!("decode error: {e}");
                    false
                }
            }
        }
        QuickCheck::new()
            .tests(10_000)
            .quickcheck(property as fn(Header<()>) -> bool)
    }
}
