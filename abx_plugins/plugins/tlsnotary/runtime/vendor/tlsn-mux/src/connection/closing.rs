use crate::{
    Result, StreamId,
    connection::StreamCommand,
    frame::{self, Frame},
    tagged_stream::TaggedStream,
};
use futures::{
    AsyncRead, AsyncWrite, SinkExt, StreamExt,
    channel::mpsc,
    ready,
    stream::{Fuse, SelectAll},
};
use std::{
    collections::VecDeque,
    future::Future,
    pin::Pin,
    task::{Context, Poll},
};

/// A [`Future`] that gracefully closes the multiplexer connection.
#[must_use]
pub struct Closing<T> {
    id: super::Id,
    state: State,
    stream_receivers: SelectAll<TaggedStream<StreamId, mpsc::Receiver<StreamCommand>>>,
    pending_frames: VecDeque<Frame<()>>,
    socket: Option<Fuse<frame::Io<T>>>,
    wait_for_reply: bool,
    keep_alive: bool,
}

impl<T> Closing<T>
where
    T: AsyncRead + AsyncWrite + Unpin,
{
    pub(crate) fn new(
        id: super::Id,
        stream_receivers: SelectAll<TaggedStream<StreamId, mpsc::Receiver<StreamCommand>>>,
        pending_frames: VecDeque<Frame<()>>,
        socket: Fuse<frame::Io<T>>,
        wait_for_reply: bool,
        keep_alive: bool,
    ) -> Self {
        Self {
            id,
            state: State::ClosingStreamReceiver,
            stream_receivers,
            pending_frames,
            socket: Some(socket),
            wait_for_reply,
            keep_alive,
        }
    }
}

impl<T> Future for Closing<T>
where
    T: AsyncRead + AsyncWrite + Unpin,
{
    type Output = Result<T>;

    fn poll(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Self::Output> {
        let this = self.get_mut();

        loop {
            match this.state {
                State::ClosingStreamReceiver => {
                    log::debug!("{}: draining streams", this.id);
                    for stream in this.stream_receivers.iter_mut() {
                        stream.inner_mut().close();
                    }
                    this.state = State::DrainingStreamReceiver;
                }

                State::DrainingStreamReceiver => {
                    match this.stream_receivers.poll_next_unpin(cx) {
                        Poll::Ready(Some((_, Some(StreamCommand::SendFrame(frame))))) => {
                            this.pending_frames.push_back(frame);
                        }
                        Poll::Ready(Some((_, Some(StreamCommand::CloseStream { stream_id })))) => {
                            this.pending_frames
                                .push_back(Frame::close_stream(stream_id).into());
                        }
                        Poll::Ready(Some((_, None))) => {}
                        Poll::Pending | Poll::Ready(None) => {
                            // No more frames from streams, append `Term` frame and flush them all.
                            this.pending_frames.push_back(Frame::term().into());
                            log::debug!(
                                "{}: flushing {} frames",
                                this.id,
                                this.pending_frames.len()
                            );
                            this.state = State::FlushingPendingFrames;
                            continue;
                        }
                    }
                }
                State::FlushingPendingFrames => {
                    let socket = this.socket.as_mut().expect("socket should be present");
                    ready!(socket.poll_ready_unpin(cx))?;

                    match this.pending_frames.pop_front() {
                        Some(frame) => socket.start_send_unpin(frame)?,
                        None => {
                            if this.wait_for_reply {
                                log::debug!("{}: awaiting goaway", this.id);
                                this.state = State::WaitingForReply;
                            } else {
                                log::debug!("{}: closing socket", this.id);
                                this.state = State::ClosingSocket;
                            }
                        }
                    }
                }
                State::WaitingForReply => {
                    // Wait for a GoAway frame from the remote before closing.
                    let socket = this.socket.as_mut().expect("socket should be present");
                    match socket.poll_next_unpin(cx) {
                        Poll::Ready(Some(Ok(frame))) => {
                            if frame.header().tag() == frame::header::Tag::GoAway {
                                log::debug!("{}: received goaway", this.id);
                                this.state = State::ClosingSocket;
                            }
                            // Ignore other frames while waiting for GoAway
                        }
                        Poll::Ready(Some(Err(e))) => {
                            return Poll::Ready(Err(e.into()));
                        }
                        Poll::Ready(None) => {
                            // Remote closed without sending GoAway, proceed to close
                            log::debug!("{}: remote closed without goaway", this.id);
                            this.state = State::ClosingSocket;
                        }
                        Poll::Pending => return Poll::Pending,
                    }
                }
                State::ClosingSocket => {
                    if this.keep_alive {
                        log::debug!("{}: keeping socket alive", this.id);
                    } else {
                        let socket = this.socket.as_mut().expect("socket should be present");
                        ready!(socket.poll_close_unpin(cx))?;
                        log::debug!("{}: socket closed", this.id);
                    }
                    let io = this
                        .socket
                        .take()
                        .expect("socket should be present")
                        .into_inner()
                        .into_inner();
                    return Poll::Ready(Ok(io));
                }
            }
        }
    }
}

enum State {
    ClosingStreamReceiver,
    DrainingStreamReceiver,
    FlushingPendingFrames,
    WaitingForReply,
    ClosingSocket,
}

#[cfg(test)]
mod tests {
    use super::*;
    use futures::{FutureExt, future::poll_fn};

    struct Socket {
        written: Vec<u8>,
        closed: bool,
    }
    impl AsyncRead for Socket {
        fn poll_read(
            self: Pin<&mut Self>,
            _: &mut Context<'_>,
            _: &mut [u8],
        ) -> Poll<std::io::Result<usize>> {
            unimplemented!()
        }
    }
    impl AsyncWrite for Socket {
        fn poll_write(
            mut self: Pin<&mut Self>,
            _: &mut Context<'_>,
            buf: &[u8],
        ) -> Poll<std::io::Result<usize>> {
            assert!(!self.closed);
            self.written.extend_from_slice(buf);
            Poll::Ready(Ok(buf.len()))
        }

        fn poll_flush(self: Pin<&mut Self>, _: &mut Context<'_>) -> Poll<std::io::Result<()>> {
            unimplemented!()
        }

        fn poll_close(mut self: Pin<&mut Self>, _: &mut Context<'_>) -> Poll<std::io::Result<()>> {
            assert!(!self.closed);
            self.closed = true;
            Poll::Ready(Ok(()))
        }
    }

    #[test]
    fn pending_frames() {
        let frame_pending = Frame::data(StreamId::new(b"stream1"), vec![2])
            .unwrap()
            .into();
        let frame_data = Frame::data(StreamId::new(b"stream3"), vec![4])
            .unwrap()
            .into();
        let frame_close = Frame::close_stream(StreamId::new(b"stream5")).into();
        let frame_term = Frame::term().into();
        fn encode(buf: &mut Vec<u8>, frame: &Frame<()>) {
            buf.extend_from_slice(&frame::header::encode(frame.header()));
            if frame.header().tag() == frame::header::Tag::Data {
                buf.extend_from_slice(frame.clone().into_data().body());
            }
        }
        let mut expected_written = vec![];
        encode(&mut expected_written, &frame_pending);
        encode(&mut expected_written, &frame_data);
        encode(&mut expected_written, &frame_close);
        encode(&mut expected_written, &frame_term);

        let receiver = |frame: &Frame<_>, command: StreamCommand| {
            TaggedStream::new(frame.header().stream_id(), {
                let (mut tx, rx) = mpsc::channel(1);
                tx.try_send(command).unwrap();
                rx
            })
        };

        let mut stream_receivers: SelectAll<_> = Default::default();
        stream_receivers.push(receiver(
            &frame_data,
            StreamCommand::SendFrame(frame_data.clone()),
        ));
        stream_receivers.push(receiver(
            &frame_close,
            StreamCommand::CloseStream {
                stream_id: StreamId::new(b"stream5"),
            },
        ));
        let pending_frames = vec![frame_pending];
        let mut socket = Socket {
            written: vec![],
            closed: false,
        };
        let mut closing = Closing::new(
            crate::connection::Id(0),
            stream_receivers,
            pending_frames.into(),
            frame::Io::new(crate::connection::Id(0), &mut socket).fuse(),
            false,
            false,
        );
        futures::executor::block_on(async { poll_fn(|cx| closing.poll_unpin(cx)).await.unwrap() });
        assert!(closing.pending_frames.is_empty());
        assert!(socket.closed);
        assert_eq!(socket.written, expected_written);
    }
}
