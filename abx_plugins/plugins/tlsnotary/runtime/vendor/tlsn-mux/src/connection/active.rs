use crate::{
    Config, Result,
    error::ConnectionError,
    frame::{
        self, Frame,
        header::{self, CONNECTION_ID, Data, GoAway, Header, Ping, StreamId, Tag, WindowUpdate},
    },
    tagged_stream::TaggedStream,
    traffic::{Counters, Traffic},
};
use futures::{
    channel::mpsc,
    prelude::*,
    stream::{Fuse, SelectAll},
    task::AtomicWaker,
};
use nohash_hasher::IntMap;
use parking_lot::Mutex;
use std::{
    collections::VecDeque,
    fmt,
    sync::Arc,
    task::{Context, Poll, Waker},
};

type PendingFrames = VecDeque<Frame<()>>;

use super::{
    Id, UserId,
    cleanup::Cleanup,
    closing::Closing,
    rtt,
    stream::{self, State, Stream},
};

/// Shared state for stream management.
///
/// This struct holds state that can be accessed by both the Connection's
/// poll loop and Handle for concurrent stream creation.
pub(crate) struct StreamRegistry {
    id: Id,
    /// Streams that are active on the wire and therefore eligible to buffer
    /// peer data. This is the bounded, peer-relevant resource: it never holds
    /// more than `config.max_num_streams` entries, which bounds the peer's
    /// buffering demand.
    ///
    /// Entries the peer has closed (RST/FIN) before any local handle adopted
    /// them are retained here — together with their buffered data and EOF —
    /// and keep holding their slot until a local handle claims the id and is
    /// dropped. Peer data is never discarded: it is the application's
    /// responsibility to open streams deterministically on both sides so
    /// every implicitly-created stream is eventually claimed.
    ///
    /// Local activation additionally gates on `active_slots()` (this map plus
    /// `owed_close`), while peer-driven implicit creation gates on the map
    /// size alone (owed closes hold no receive buffer), so the combined count
    /// can transiently exceed `max_num_streams` by the number of owed closes.
    streams: IntMap<StreamId, Arc<Mutex<stream::Shared>>>,
    /// Local handles that have not yet become active on the wire. These never
    /// buffer peer data and consume no slot until promoted into `streams`.
    inactive: IntMap<StreamId, Arc<Mutex<stream::Shared>>>,
    /// Close frames (RST/FIN) owed to the peer by streams that were dropped
    /// after activation, staged until flushed to the socket. These are never
    /// dropped and count toward the slot limit.
    owed_close: IntMap<StreamId, Frame<()>>,
    /// Wakers of writers blocked waiting for a slot to free.
    slot_wakers: Vec<Waker>,
    new_receiver_tx: mpsc::UnboundedSender<TaggedStream<StreamId, mpsc::Receiver<StreamCommand>>>,
    /// Waker for the task driving `Active::poll`. Fired by streams when
    /// they push a command, and by the registry when a new stream is
    /// created.
    driver_waker: Arc<AtomicWaker>,
    config: Arc<Config>,
    rtt: rtt::Rtt,
    accumulated_max_stream_windows: Arc<Mutex<usize>>,
}

impl StreamRegistry {
    fn new(
        id: Id,
        config: Arc<Config>,
        rtt: rtt::Rtt,
        accumulated_max_stream_windows: Arc<Mutex<usize>>,
        new_receiver_tx: mpsc::UnboundedSender<
            TaggedStream<StreamId, mpsc::Receiver<StreamCommand>>,
        >,
        driver_waker: Arc<AtomicWaker>,
    ) -> Self {
        Self {
            id,
            streams: IntMap::default(),
            inactive: IntMap::default(),
            owed_close: IntMap::default(),
            slot_wakers: Vec::new(),
            new_receiver_tx,
            driver_waker,
            config,
            rtt,
            accumulated_max_stream_windows,
        }
    }

    /// The number of slots currently occupied: streams active on the wire
    /// plus close frames owed to the peer but not yet flushed.
    fn active_slots(&self) -> usize {
        self.streams.len() + self.owed_close.len()
    }

    /// Register a waker to be notified when a slot becomes available.
    pub(crate) fn register_slot_waker(&mut self, waker: &Waker) {
        if !self.slot_wakers.iter().any(|w| w.will_wake(waker)) {
            self.slot_wakers.push(waker.clone());
        }
    }

    /// Wake all writers blocked waiting for a slot to free.
    fn wake_slot_waiters(&mut self) {
        for waker in self.slot_wakers.drain(..) {
            waker.wake();
        }
    }

    /// Handle a peer reset of `stream_id`: transition it to closed and wake its
    /// wakers.
    ///
    /// The entry is retained in `streams` even when no local handle references
    /// it yet: its buffered data and EOF must survive until a local handle
    /// opens (adopts) the id, otherwise a late opener would wait forever for
    /// data the peer already delivered. The entry keeps holding its slot until
    /// it is claimed and the last handle is dropped.
    fn handle_peer_reset(&mut self, id: Id, stream_id: StreamId) {
        let Some(s) = self.streams.get(&stream_id) else {
            return;
        };
        let mut shared = s.lock();
        shared.update_state(id, stream_id, State::Closed);
        if let Some(w) = shared.reader.take() {
            w.wake()
        }
        if let Some(w) = shared.writer.take() {
            w.wake()
        }
    }

    /// If the peer has already created an active entry for `stream_id`, return
    /// the canonical [`Shared`] so a local handle can adopt it instead of
    /// claiming a new slot. Removes any duplicate inactive entry.
    pub(crate) fn adopt_active(
        &mut self,
        stream_id: StreamId,
    ) -> Option<Arc<Mutex<stream::Shared>>> {
        let shared = self.streams.get(&stream_id)?.clone();
        self.inactive.remove(&stream_id);
        Some(shared)
    }

    /// Attempt to claim a slot for `shared`, promoting it from the inactive
    /// set into the active `streams` map. Returns `false` (claiming nothing)
    /// when no slot is available.
    pub(crate) fn try_claim_slot(
        &mut self,
        stream_id: StreamId,
        shared: &Arc<Mutex<stream::Shared>>,
    ) -> bool {
        if self.active_slots() >= self.config.max_num_streams {
            return false;
        }
        self.inactive.remove(&stream_id);
        self.streams.insert(stream_id, shared.clone());
        true
    }

    fn new_stream(registry: &Arc<Mutex<StreamRegistry>>, user_id: &[u8]) -> Result<Stream> {
        let mut this = registry.lock();
        let user_id = UserId::new(user_id)?;
        let stream_id = StreamId::new(user_id.as_bytes());

        // Adopt the canonical `Shared` if one already exists, otherwise create
        // a fresh inactive one. The handle never inserts into `streams`: a
        // stream becomes slot-eligible only when it first writes.
        let shared = if let Some(existing) = this.streams.get(&stream_id) {
            log::trace!("{}/{}: merging with existing stream", this.id, stream_id);
            existing.clone()
        } else if let Some(existing) = this.inactive.get(&stream_id) {
            log::trace!(
                "{}/{}: merging with existing inactive stream",
                this.id,
                stream_id
            );
            existing.clone()
        } else {
            let shared = this.make_shared();
            this.inactive.insert(stream_id, shared.clone());
            shared
        };

        let stream = this.make_stream_with_shared(registry, stream_id, user_id, shared);

        log::debug!("{}: new stream {}", this.id, stream);

        this.driver_waker.wake();

        Ok(stream)
    }

    /// Create a Stream using existing Shared state.
    fn make_stream_with_shared(
        &mut self,
        registry: &Arc<Mutex<StreamRegistry>>,
        id: StreamId,
        user_id: UserId,
        shared: Arc<Mutex<stream::Shared>>,
    ) -> Stream {
        let (sender, receiver) = mpsc::channel(10);
        let _ = self
            .new_receiver_tx
            .unbounded_send(TaggedStream::new(id, receiver));

        Stream::with_shared(
            id,
            user_id,
            self.id,
            self.config.clone(),
            sender,
            shared,
            registry.clone(),
            self.driver_waker.clone(),
        )
    }

    /// Create a fresh inactive `Shared` (not yet active on the wire).
    fn make_shared(&self) -> Arc<Mutex<stream::Shared>> {
        Arc::new(Mutex::new(stream::Shared::new(
            State::Open,
            crate::DEFAULT_CREDIT,
            crate::DEFAULT_CREDIT,
            self.accumulated_max_stream_windows.clone(),
            self.rtt.clone(),
            self.config.clone(),
        )))
    }

    /// Create a `Shared` for a stream implicitly created by the peer, marked
    /// active because inserting it into `streams` claims a slot.
    fn make_implicit_stream_shared(&mut self) -> Arc<Mutex<stream::Shared>> {
        let shared = self.make_shared();
        shared.lock().set_activated();
        shared
    }
}

/// A handle for creating streams concurrently.
///
/// This type can be cloned and used from multiple tasks while the
/// Connection is being polled.
#[derive(Clone)]
pub struct Handle {
    registry: Arc<Mutex<StreamRegistry>>,
    traffic: Arc<Counters>,
}

impl Handle {
    /// Create a new stream with the given user ID.
    ///
    /// The stream ID is computed from the user ID using BLAKE3.
    pub fn new_stream(&self, user_id: &[u8]) -> Result<Stream> {
        StreamRegistry::new_stream(&self.registry, user_id)
    }

    /// Payload counters for this connection, cumulative since it was created.
    ///
    /// Sampling takes no locks and blocks nothing, so a watchdog may poll it
    /// as often as it likes. The counters keep their final values once the
    /// connection is gone.
    pub fn traffic(&self) -> Traffic {
        self.traffic.snapshot()
    }
}

/// `Stream` to `Connection` commands.
#[derive(Debug)]
pub(crate) enum StreamCommand {
    /// A new frame should be sent to the remote.
    SendFrame(Frame<()>),
    /// Close a stream.
    CloseStream { stream_id: StreamId },
}

/// Possible actions as a result of incoming frame handling.
#[derive(Debug)]
pub(crate) enum Action {
    /// Nothing to be done.
    None,
    /// A ping with this nonce should be answered with a pong.
    Pong(u32),
    /// The connection should be terminated.
    Terminate(Frame<GoAway>),
}

/// A GoAway carries its reason in the header's length field. Report which one
/// the peer sent, so a connection killed for cause is distinguishable from an
/// ordinary shutdown.
fn go_away_error(id: Id, code: u32) -> ConnectionError {
    match code {
        0 => ConnectionError::Closed,
        1 => ConnectionError::PeerProtocolError,
        2 => ConnectionError::PeerInternalError,
        // A code this version does not know. It is not code 0, so the peer
        // did not hang up gracefully: report it as an error rather than let
        // it pass for a clean shutdown, but do not invent a cause for it.
        other => {
            log::warn!("{}: unknown go away code {}", id, other);
            ConnectionError::PeerError(other)
        }
    }
}

/// The active state of [`super::Connection`].
pub(crate) struct Active<T> {
    id: Id,
    pub(super) config: Arc<Config>,
    /// Payload counters, shared with every `Handle`.
    traffic: Arc<Counters>,
    socket: Fuse<frame::Io<T>>,

    registry: Arc<Mutex<StreamRegistry>>,
    stream_receivers: SelectAll<TaggedStream<StreamId, mpsc::Receiver<StreamCommand>>>,
    new_receiver_rx: mpsc::UnboundedReceiver<TaggedStream<StreamId, mpsc::Receiver<StreamCommand>>>,
    no_streams_waker: Option<Waker>,

    driver_waker: Arc<AtomicWaker>,

    /// Nonce of an inbound ping awaiting a reply, if any. Stored as a bare
    /// nonce (not a built frame) and coalesced — only the most recent ping
    /// is remembered — so a peer flooding pings costs O(1) memory. The pong
    /// is constructed lazily once the socket accepts writes.
    pending_pong: Option<u32>,
    /// A termination frame produced on a protocol error, awaiting send.
    pending_terminate: Option<Frame<()>>,
    /// Set once we have decided to terminate. Inbound frames are ignored from
    /// then on: the read side keeps draining (gating it would deadlock two
    /// peers with full send buffers), but re-deciding per frame would emit a
    /// GoAway and an error log for every frame the peer had already sent.
    terminating: bool,
    /// The current outbound stream frame awaiting the socket.
    ///
    /// The driver keeps reading the socket even while these are set. Coupling
    /// reads to a pending write deadlocks: when both peers' socket send
    /// buffers fill and each holds a control reply, each stops reading, so
    /// neither drains the other's buffer. Always draining the read side
    /// breaks that cycle.
    pending_write_frame: Option<Frame<()>>,
}

impl<T> fmt::Debug for Active<T> {
    fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result {
        f.debug_struct("Connection")
            .field("id", &self.id)
            .field("streams", &self.registry.lock().streams.len())
            .finish()
    }
}

impl<T> fmt::Display for Active<T> {
    fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result {
        write!(
            f,
            "(Connection {} (streams {}))",
            self.id,
            self.registry.lock().streams.len()
        )
    }
}

impl<T: AsyncRead + AsyncWrite + Unpin> Active<T> {
    /// Create a new `Connection` from the given I/O resource.
    pub(super) fn new(socket: T, cfg: Config) -> Self {
        let id = Id::random();
        log::debug!("new connection: {id}");
        let socket = frame::Io::new(id, socket).fuse();
        let config = Arc::new(cfg);
        let rtt = rtt::Rtt::new();
        let accumulated_max_stream_windows = Arc::new(Mutex::new(0));
        let (new_receiver_tx, new_receiver_rx) = mpsc::unbounded();
        let driver_waker = Arc::new(AtomicWaker::new());
        let traffic = Arc::new(Counters::default());
        let registry = Arc::new(Mutex::new(StreamRegistry::new(
            id,
            config.clone(),
            rtt,
            accumulated_max_stream_windows,
            new_receiver_tx,
            driver_waker.clone(),
        )));
        Active {
            id,
            config,
            traffic,
            socket,
            registry,
            stream_receivers: SelectAll::default(),
            new_receiver_rx,
            no_streams_waker: None,
            driver_waker,
            pending_pong: None,
            pending_terminate: None,
            terminating: false,
            pending_write_frame: None,
        }
    }

    /// Get a handle for creating streams concurrently.
    pub(super) fn handle(&self) -> Handle {
        Handle {
            registry: self.registry.clone(),
            traffic: self.traffic.clone(),
        }
    }

    /// Gracefully close the connection to the remote.
    pub(super) fn close(mut self) -> Closing<T> {
        self.prepare_close();
        let wait_for_reply = self.config.close_sync;
        let pending_frames = self.take_pending_frames();
        Closing::new(
            self.id,
            self.stream_receivers,
            pending_frames,
            self.socket,
            wait_for_reply,
            self.config.keep_alive,
        )
    }

    /// Prepare to leave the active state: close the stream command receivers so
    /// any blocked writer observes a closed channel, and wake all writers
    /// parked on slot availability. Once we leave the active state the driver
    /// no longer frees slots, so a parked writer would otherwise hang forever.
    ///
    /// Nothing is delivered to streams once we leave the active state, so every
    /// stream — active or not — is marked receive-closed and its parked
    /// readers/writers are woken: a reader drains its buffer and then observes
    /// EOF, and a writer observes the closed channel, instead of hanging.
    fn prepare_close(&mut self) {
        for stream in self.stream_receivers.iter_mut() {
            stream.inner_mut().close();
        }
        let mut registry = self.registry.lock();
        let registry = &mut *registry;
        for (id, s) in registry.streams.iter().chain(registry.inactive.iter()) {
            let mut shared = s.lock();
            shared.update_state(self.id, *id, State::RecvClosed);
            if let Some(w) = shared.reader.take() {
                w.wake()
            }
            if let Some(w) = shared.writer.take() {
                w.wake()
            }
        }
        registry.wake_slot_waiters();
    }

    /// Collect any control/data frames not yet flushed to the socket, in
    /// send-priority order, so a closing connection can drain them.
    fn take_pending_frames(&self) -> PendingFrames {
        let pong = self.pending_pong.map(|nonce| {
            let mut hdr = Header::ping(nonce);
            hdr.ack();
            Frame::new(hdr).into()
        });
        // Owed close frames are never dropped; flush any still staged.
        let owed_close = self
            .registry
            .lock()
            .owed_close
            .values()
            .cloned()
            .collect::<Vec<_>>();
        self.pending_terminate
            .clone()
            .into_iter()
            .chain(pong)
            .chain(self.pending_write_frame.clone())
            .chain(owed_close)
            .collect::<PendingFrames>()
    }

    /// Close the connection without waiting for a reply.
    pub(super) fn close_no_wait(mut self) -> Closing<T> {
        self.prepare_close();
        let pending_frames = self.take_pending_frames();
        Closing::new(
            self.id,
            self.stream_receivers,
            pending_frames,
            self.socket,
            false,
            self.config.keep_alive,
        )
    }

    /// Cleanup all our resources.
    pub(super) fn cleanup(mut self, error: ConnectionError) -> Cleanup {
        self.drop_all_streams();
        Cleanup::new(self.stream_receivers, error)
    }

    pub(super) fn poll(&mut self, cx: &mut Context<'_>) -> Poll<Result<()>> {
        loop {
            // Poll for new stream receivers from Handle
            while let Poll::Ready(Some(receiver)) = self.new_receiver_rx.poll_next_unpin(cx) {
                self.stream_receivers.push(receiver);
                if let Some(waker) = self.no_streams_waker.take() {
                    waker.wake();
                }
            }

            self.driver_waker.register(cx.waker());

            if self.socket.poll_ready_unpin(cx).is_ready() {
                if let Some(frame) = self.registry.lock().rtt.next_ping() {
                    self.socket.start_send_unpin(frame.into())?;
                    continue;
                }

                // Control frames take priority over stream data: termination
                // first (we are tearing down), then the opportunistic pong,
                // built lazily from the stored nonce.
                if let Some(frame) = self.pending_terminate.take() {
                    self.socket.start_send_unpin(frame)?;
                    continue;
                }
                if let Some(nonce) = self.pending_pong.take() {
                    let mut hdr = Header::ping(nonce);
                    hdr.ack();
                    self.socket.start_send_unpin(Frame::new(hdr).into())?;
                    continue;
                }
                if let Some(frame) = self.pending_write_frame.take() {
                    // Every stream frame passes here: data, window updates and
                    // close frames alike. Only data carries payload.
                    if frame.header().tag() == Tag::Data {
                        self.traffic.record_out(frame.header().len().val());
                    }
                    self.socket.start_send_unpin(frame)?;
                    continue;
                }
            }

            match self.socket.poll_flush_unpin(cx)? {
                Poll::Ready(()) => {}
                Poll::Pending => {}
            }

            // Flush an owed close frame if one is staged. Moving it out of
            // `owed_close` into the outbound queue commits it to being sent and
            // frees the slot it was holding, so any blocked writer is woken.
            if self.pending_write_frame.is_none() {
                let mut registry = self.registry.lock();
                if let Some(stream_id) = registry.owed_close.keys().next().copied() {
                    let frame = registry
                        .owed_close
                        .remove(&stream_id)
                        .expect("owed close frame should be present");
                    registry.wake_slot_waiters();
                    drop(registry);
                    log::trace!(
                        "{}/{}: sending owed close: {}",
                        self.id,
                        stream_id,
                        frame.header()
                    );
                    self.pending_write_frame.replace(frame);
                    continue;
                }
            }

            if self.pending_write_frame.is_none() {
                match self.stream_receivers.poll_next_unpin(cx) {
                    Poll::Ready(Some((_, Some(StreamCommand::SendFrame(frame))))) => {
                        log::trace!(
                            "{}/{}: sending: {}",
                            self.id,
                            frame.header().stream_id(),
                            frame.header()
                        );
                        self.pending_write_frame.replace(frame);
                        continue;
                    }
                    Poll::Ready(Some((_, Some(StreamCommand::CloseStream { stream_id })))) => {
                        log::trace!("{}/{}: sending close", self.id, stream_id);
                        self.pending_write_frame
                            .replace(Frame::close_stream(stream_id).into());
                        continue;
                    }
                    Poll::Ready(Some((id, None))) => {
                        self.on_drop_stream(id);
                        continue;
                    }
                    Poll::Ready(None) => {
                        self.no_streams_waker = Some(cx.waker().clone());
                    }
                    Poll::Pending => {}
                }
            }

            // Always drain the read side, even with frames pending for write.
            // Gating reads on a pending control reply deadlocks two peers whose
            // socket send buffers are both full.
            match self.socket.poll_next_unpin(cx) {
                Poll::Ready(Some(frame)) => {
                    match self.on_frame(frame?)? {
                        Action::None => {}
                        Action::Pong(nonce) => {
                            log::trace!("{}: pong {}", self.id, nonce);
                            // Coalesce: only the most recent ping is answered.
                            self.pending_pong = Some(nonce);
                        }
                        Action::Terminate(f) => {
                            log::trace!("{}: sending term", self.id);
                            self.terminating = true;
                            self.pending_terminate = Some(f.into());
                        }
                    }
                    continue;
                }
                Poll::Ready(None) => {
                    return Poll::Ready(Err(ConnectionError::Closed));
                }
                Poll::Pending => {}
            }

            return Poll::Pending;
        }
    }

    /// Create a new stream.
    ///
    /// The stream ID is computed from the user ID using BLAKE3.
    pub(super) fn new_stream(&mut self, user_id: &[u8]) -> Result<Stream> {
        let stream = StreamRegistry::new_stream(&self.registry, user_id)?;
        // Drain new receivers immediately so they're available before poll
        while let Ok(receiver) = self.new_receiver_rx.try_recv() {
            self.stream_receivers.push(receiver);
        }
        Ok(stream)
    }

    fn on_drop_stream(&mut self, stream_id: StreamId) {
        let mut registry = self.registry.lock();

        // Multiple handles may share one `Shared` (a merged user id). Only the
        // last handle drop reaps the stream; while a sibling handle is alive
        // (`strong_count > 1`, i.e. more than just the map's reference) the
        // stream stays active and owes nothing yet.
        if registry
            .streams
            .get(&stream_id)
            .is_some_and(|s| Arc::strong_count(s) > 1)
        {
            return;
        }
        if registry
            .inactive
            .get(&stream_id)
            .is_some_and(|s| Arc::strong_count(s) > 1)
        {
            return;
        }

        let Some(s) = registry.streams.remove(&stream_id) else {
            // A never-activated stream lives only in the inactive set; dropping
            // it owes the peer nothing and frees no slot.
            if registry.inactive.remove(&stream_id).is_none() {
                log::warn!("{}: stream {} not found on drop", self.id, stream_id);
            }
            return;
        };

        log::trace!("{}: removing dropped stream {}", self.id, stream_id);
        let frame = {
            let mut shared = s.lock();
            let frame = match shared.update_state(self.id, stream_id, State::Closed) {
                State::Open => {
                    let mut header = Header::data(stream_id, 0);
                    header.rst();
                    Some(Frame::new(header))
                }
                State::RecvClosed => {
                    let mut header = Header::data(stream_id, 0);
                    header.fin();
                    Some(Frame::new(header))
                }
                State::SendClosed => None,
                State::Closed => None,
            };
            if let Some(w) = shared.reader.take() {
                w.wake()
            }
            if let Some(w) = shared.writer.take() {
                w.wake()
            }
            frame
        };

        // The per-stream buffer/window is freed now (the map's `Arc` is gone).
        // The slot, however, is held until the owed close frame is flushed: it
        // is staged in `owed_close` (counting toward the limit) and released
        // once the driver moves it into the outbound queue. A stream with no
        // owed frame frees its slot immediately.
        match frame {
            Some(frame) => {
                registry.owed_close.insert(stream_id, frame.into());
            }
            None => registry.wake_slot_waiters(),
        }
    }

    fn on_frame(&mut self, frame: Frame<()>) -> Result<Action> {
        if self.terminating {
            log::trace!("{}: ignoring frame after terminate", self.id);
            return Ok(Action::None);
        }

        log::trace!("{}: received: {}", self.id, frame.header());

        let action = match frame.header().tag() {
            Tag::Data => self.on_data(frame.into_data()),
            Tag::WindowUpdate => self.on_window_update(&frame.into_window_update()),
            Tag::Ping => self.on_ping(&frame.into_ping()),
            Tag::GoAway => return Err(go_away_error(self.id, frame.header().len().val())),
        };
        Ok(action)
    }

    fn on_data(&mut self, frame: Frame<Data>) -> Action {
        let stream_id = frame.header().stream_id();
        let mut registry = self.registry.lock();

        if frame.header().flags().contains(header::RST) {
            registry.handle_peer_reset(self.id, stream_id);
            return Action::None;
        }

        if stream_id.is_session() {
            log::error!("{}: data frame for session stream ID 0", self.id);
            return Action::Terminate(Frame::protocol_error());
        }

        let is_syn = frame.header().flags().contains(header::SYN);
        let is_finish = frame.header().flags().contains(header::FIN);

        // SYN decides only whether a frame may *create* a stream, so it is read
        // in the branch below and nowhere else. A SYN for an id we already hold
        // needs no handling of its own: creation happened when our own first
        // write claimed the slot.
        if !registry.streams.contains_key(&stream_id) {
            // Only a SYN opens a stream. Any other frame for an id we do not
            // hold is a leftover from a stream we already reaped. Discarding
            // it is not a protocol violation.
            if !is_syn {
                log::trace!(
                    "{}/{}: frame for unknown stream, reaped earlier",
                    self.id,
                    stream_id
                );
                return Action::None;
            }

            // An opening frame arrives before any window negotiation, so it
            // cannot exceed the default credit.
            if frame.body_len() > crate::DEFAULT_CREDIT {
                log::error!(
                    "{}/{}: opening frame exceeds default credit",
                    self.id,
                    stream_id
                );
                return Action::Terminate(Frame::protocol_error());
            }

            // Only streams active on the wire buffer peer data, so the peer's
            // buffering demand is bounded by `streams.len()`, not by close
            // frames we still owe (those hold no receive buffer).
            if registry.streams.len() >= self.config.max_num_streams {
                log::warn!(
                    "{}: maximum number of streams reached ({})",
                    self.id,
                    self.config.max_num_streams
                );
                // The peer opened more streams than we admit. Nothing on the
                // wire was malformed, but it exceeded what this side allows,
                // which is the peer's doing and not a fault of our own.
                return Action::Terminate(Frame::protocol_error());
            }

            let shared = if let Some(shared) = registry.inactive.remove(&stream_id) {
                log::trace!("{}/{}: promoting local inactive stream", self.id, stream_id);
                shared.lock().set_activated();
                shared
            } else {
                log::trace!("{}/{}: opening stream from peer SYN", self.id, stream_id);
                registry.make_implicit_stream_shared()
            };
            registry.streams.insert(stream_id, shared);
        }

        if let Some(s) = registry.streams.get_mut(&stream_id) {
            let mut shared = s.lock();
            if frame.body_len() > shared.receive_window() {
                log::error!(
                    "{}/{}: frame body larger than window of stream",
                    self.id,
                    stream_id
                );
                return Action::Terminate(Frame::protocol_error());
            }
            if is_finish {
                shared.update_state(self.id, stream_id, State::RecvClosed);
            }
            shared.consume_receive_window(frame.body_len());
            self.traffic.record_in(frame.body_len());
            shared.buffer.push(frame.into_body());
            if let Some(w) = shared.reader.take() {
                w.wake()
            }
        }

        Action::None
    }

    fn on_window_update(&mut self, frame: &Frame<WindowUpdate>) -> Action {
        let stream_id = frame.header().stream_id();
        let mut registry = self.registry.lock();

        // An id we do not hold is one we have already released,
        // and this is a leftover the peer queued before it learned we were
        // done.
        if !registry.streams.contains_key(&stream_id) {
            // Window updates for the session id are simply ignored.
            if !stream_id.is_session() {
                log::trace!(
                    "{}/{}: window update for unknown stream",
                    self.id,
                    stream_id
                );
            }
            return Action::None;
        }

        if let Some(s) = registry.streams.get_mut(&stream_id) {
            let mut shared = s.lock();
            shared.increase_send_window_by(frame.header().credit());
            if let Some(w) = shared.writer.take() {
                w.wake()
            }
        }

        Action::None
    }

    fn on_ping(&mut self, frame: &Frame<Ping>) -> Action {
        let stream_id = frame.header().stream_id();
        let mut registry = self.registry.lock();
        if frame.header().flags().contains(header::ACK) {
            return registry.rtt.handle_pong(frame.nonce());
        }
        if stream_id == CONNECTION_ID || registry.streams.contains_key(&stream_id) {
            return Action::Pong(frame.header().nonce());
        }
        log::debug!(
            "{}/{}: ping for unknown stream, possibly dropped earlier",
            self.id,
            stream_id,
        );
        Action::None
    }
}

impl<T> Active<T> {
    /// Close and drop all `Stream`s and wake any pending `Waker`s.
    pub(super) fn drop_all_streams(&mut self) {
        // Close the stream command receivers before waking anyone: a woken
        // writer re-polls immediately and must observe a closed channel,
        // otherwise it could re-register on a slot that will never free now
        // that the driver is gone.
        for stream in self.stream_receivers.iter_mut() {
            stream.inner_mut().close();
        }
        let mut registry = self.registry.lock();
        let registry = &mut *registry;
        let drained: Vec<_> = registry
            .streams
            .drain()
            .chain(registry.inactive.drain())
            .collect();
        for (id, s) in drained {
            let mut shared = s.lock();
            shared.update_state(self.id, id, State::Closed);
            if let Some(w) = shared.reader.take() {
                w.wake()
            }
            if let Some(w) = shared.writer.take() {
                w.wake()
            }
        }
        registry.wake_slot_waiters();
    }
}
