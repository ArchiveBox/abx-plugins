use anyhow::{Result, bail, ensure};
use async_tungstenite::{
    tokio::{accept_hdr_async_with_config, client_async_tls_with_connector_and_config},
    tungstenite::{
        client::IntoClientRequest,
        handshake::server::{ErrorResponse, Request as WsRequest, Response as WsResponse},
        http::StatusCode,
        protocol::WebSocketConfig,
    },
};
use base64::{Engine, prelude::BASE64_STANDARD};
use clap::{Parser, Subcommand};
use futures::io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt};
use http_body_util::{BodyExt, Empty};
use hyper::{Request, body::Bytes};
use hyper_util::rt::TokioIo;
use std::{future::IntoFuture, path::PathBuf, sync::Arc, time::Duration};
use tlsn::{
    Session,
    attestation::{
        Attestation, AttestationConfig, CryptoProvider,
        request::{Request as AttestationRequest, RequestConfig},
        signing::Secp256k1Signer,
    },
    config::{
        prove::ProveConfig, prover::ProverConfig, tls::TlsClientConfig,
        tls_commit::mpc::MpcTlsConfig, verifier::VerifierConfig,
    },
    connection::{CertBinding, ConnectionInfo, HandshakeData, ServerName, TranscriptLength},
    transcript::{ContentType, TranscriptCommitConfig},
    verifier::VerifierCommitStart,
    webpki::RootCertStore,
};
use tokio_util::compat::{FuturesAsyncReadCompatExt, TokioAsyncReadCompatExt};
use ws_stream_tungstenite::WsStream;

#[derive(Parser)]
#[command(version)]
struct Args {
    #[command(subcommand)]
    command: Command,
}
#[derive(Subcommand)]
enum Command {
    Capture {
        #[arg(long)]
        url: String,
        #[arg(long)]
        notary: String,
        #[arg(long)]
        trusted_key: String,
        #[arg(long)]
        output: PathBuf,
        #[arg(long, default_value_t = 49152)]
        max_recv: usize,
        #[arg(long, default_value_t = 180)]
        timeout: u64,
        /// Stop after this many HTTP wire bytes; certify a clearly labelled prefix.
        #[arg(long, default_value_t = 49152)]
        prefix_bytes: usize,
    },
    Verify {
        artifact: PathBuf,
        #[arg(long)]
        trusted_key: String,
        #[arg(long)]
        expected_url: Option<String>,
    },
    Serve {
        #[arg(long, default_value = "127.0.0.1:7048")]
        bind: String,
        #[arg(long, env = "TLSNOTARY_SIGNING_KEY_FILE")]
        key_file: PathBuf,
        #[arg(long, default_value_t = 2)]
        concurrency: usize,
        #[arg(long, default_value_t = 49152)]
        max_recv: usize,
        #[arg(long, default_value_t = 180)]
        timeout: u64,
    },
    PublicKey {
        key_file: PathBuf,
    },
}
// WsStream uses max_message_size to split writes. Keep messages below the frame
// limit too: its defaults permit 64 MiB messages but only 16 MiB received frames.
const WIRE_PROTOCOL: &str = "abx-tlsnotary-batch-v1";

fn websocket_config() -> WebSocketConfig {
    WebSocketConfig::default()
        .max_message_size(Some(1024 * 1024))
        .max_frame_size(Some(1024 * 1024))
        .max_write_buffer_size(2 * 1024 * 1024)
}
// Stop at a complete TLS record boundary. The TLSN engine still authenticates
// every admitted record; this transport only bounds acquisition, never verification.
struct RecordBudget<S> {
    inner: S,
    budget: usize,
    used: usize,
    header: [u8; 5],
    filled: usize,
    emitted: usize,
    remaining: usize,
    stopped: bool,
}
impl<S> RecordBudget<S> {
    fn new(inner: S, budget: usize) -> Self {
        Self {
            inner,
            budget,
            used: 0,
            header: [0; 5],
            filled: 0,
            emitted: 5,
            remaining: 0,
            stopped: false,
        }
    }
}
impl<S: AsyncRead + Unpin> AsyncRead for RecordBudget<S> {
    fn poll_read(
        self: std::pin::Pin<&mut Self>,
        cx: &mut std::task::Context<'_>,
        out: &mut [u8],
    ) -> std::task::Poll<std::io::Result<usize>> {
        use std::{
            pin::Pin,
            task::{Poll, ready},
        };
        let this = self.get_mut();
        if out.is_empty() || this.stopped {
            return Poll::Ready(Ok(0));
        }
        loop {
            if this.emitted < 5 {
                let count = out.len().min(5 - this.emitted);
                out[..count].copy_from_slice(&this.header[this.emitted..this.emitted + count]);
                this.emitted += count;
                return Poll::Ready(Ok(count));
            }
            if this.remaining > 0 {
                let limit = out.len().min(this.remaining);
                let count = ready!(Pin::new(&mut this.inner).poll_read(cx, &mut out[..limit]))?;
                if count == 0 {
                    return Poll::Ready(Err(std::io::ErrorKind::UnexpectedEof.into()));
                }
                this.remaining -= count;
                return Poll::Ready(Ok(count));
            }
            while this.filled < 5 {
                let count = ready!(
                    Pin::new(&mut this.inner).poll_read(cx, &mut this.header[this.filled..])
                )?;
                if count == 0 {
                    if this.filled != 0 {
                        return Poll::Ready(Err(std::io::ErrorKind::UnexpectedEof.into()));
                    }
                    return Poll::Ready(Ok(0));
                }
                this.filled += count;
            }
            let length = u16::from_be_bytes([this.header[3], this.header[4]]) as usize;
            if this.header[0] == 23 {
                if length > this.budget.saturating_sub(this.used) {
                    this.stopped = true;
                    return Poll::Ready(Ok(0));
                }
                this.used += length;
            }
            this.remaining = length;
            this.emitted = 0;
            this.filled = 0;
        }
    }
}
impl<S: AsyncWrite + Unpin> AsyncWrite for RecordBudget<S> {
    fn poll_write(
        self: std::pin::Pin<&mut Self>,
        cx: &mut std::task::Context<'_>,
        data: &[u8],
    ) -> std::task::Poll<std::io::Result<usize>> {
        std::pin::Pin::new(&mut self.get_mut().inner).poll_write(cx, data)
    }
    fn poll_flush(
        self: std::pin::Pin<&mut Self>,
        cx: &mut std::task::Context<'_>,
    ) -> std::task::Poll<std::io::Result<()>> {
        std::pin::Pin::new(&mut self.get_mut().inner).poll_flush(cx)
    }
    fn poll_close(
        self: std::pin::Pin<&mut Self>,
        cx: &mut std::task::Context<'_>,
    ) -> std::task::Poll<std::io::Result<()>> {
        std::pin::Pin::new(&mut self.get_mut().inner).poll_close(cx)
    }
}

fn signing_key(path: &PathBuf) -> Result<k256::ecdsa::SigningKey> {
    Ok(k256::ecdsa::SigningKey::from_slice(&hex::decode(
        std::fs::read_to_string(path)?.trim(),
    )?)?)
}
// Abort the mux driver when a timed-out request is dropped; detached tasks must not retain slots/resources.
struct DriverGuard(tokio::task::AbortHandle);
impl Drop for DriverGuard {
    fn drop(&mut self) {
        self.0.abort();
    }
}
async fn write_frame<S: AsyncWrite + Unpin, T: serde::Serialize>(
    socket: &mut S,
    item: &T,
) -> Result<()> {
    let data = bincode::serialize(item)?;
    ensure!(data.len() <= 65536, "attestation control message too large");
    socket.write_all(&(data.len() as u32).to_be_bytes()).await?;
    socket.write_all(&data).await?;
    socket.flush().await?;
    Ok(())
}
async fn read_frame<S: AsyncRead + Unpin, T: serde::de::DeserializeOwned>(
    socket: &mut S,
) -> Result<T> {
    let mut size = [0; 4];
    socket.read_exact(&mut size).await?;
    let size = u32::from_be_bytes(size) as usize;
    ensure!(size <= 65536, "attestation control message too large");
    let mut data = vec![0; size];
    socket.read_exact(&mut data).await?;
    abx_tlsnotary::decode(&data)
}

#[tokio::main]
async fn main() -> Result<()> {
    match Args::parse().command {
        Command::PublicKey { key_file } => println!(
            "{}",
            hex::encode(
                signing_key(&key_file)?
                    .verifying_key()
                    .to_encoded_point(true)
                    .as_bytes()
            )
        ),
        Command::Verify {
            artifact,
            trusted_key,
            expected_url,
        } => {
            let result = abx_tlsnotary::verify(&std::fs::read(artifact)?, &trusted_key)?;
            if let Some(url) = expected_url {
                ensure!(
                    result.url == url,
                    "authenticated URL differs from expected URL"
                );
            }
            println!("{}", serde_json::to_string(&result)?);
        }
        Command::Capture {
            url,
            notary,
            trusted_key,
            output,
            max_recv,
            timeout,
            prefix_bytes,
        } => {
            tokio::time::timeout(
                Duration::from_secs(timeout),
                capture(&url, &notary, &trusted_key, &output, max_recv, prefix_bytes),
            )
            .await??;
        }
        Command::Serve {
            bind,
            key_file,
            concurrency,
            max_recv,
            timeout,
        } => {
            ensure!(
                (1..=2).contains(&concurrency),
                "public service supports at most two simultaneous MPC sessions"
            );
            let key = Arc::new(signing_key(&key_file)?);
            let slots = Arc::new(tokio::sync::Semaphore::new(concurrency));
            let listener = tokio::net::TcpListener::bind(bind).await?;
            eprintln!("notary_ready concurrency={concurrency} max_recv={max_recv}");
            loop {
                let (tcp, _) = listener.accept().await?;
                tcp.set_nodelay(true)?;
                let permit = slots.clone().try_acquire_owned();
                if permit.is_err() {
                    // Reject overload before any MPC allocation or WebSocket upgrade.
                    tokio::spawn(async move {
                        use tokio::io::AsyncWriteExt;
                        let mut tcp = tcp;
                        let _=tokio::time::timeout(Duration::from_secs(2),tcp.write_all(b"HTTP/1.1 503 Service Unavailable\r\nRetry-After: 10\r\nConnection: close\r\nContent-Length: 0\r\n\r\n")).await;
                    });
                    continue;
                }
                let key = key.clone();
                tokio::spawn(async move {
                    let _permit = permit.unwrap();
                    let started = std::time::Instant::now();
                    let outcome = tokio::time::timeout(Duration::from_secs(timeout), async {
                        let ws = accept_hdr_async_with_config(
                            tcp,
                            |req: &WsRequest, mut resp: WsResponse| {
                                if req.uri().path() != "/notarize" || req.uri().query().is_some()
                                    || req.headers().get("Sec-WebSocket-Protocol").and_then(|v| v.to_str().ok()) != Some(WIRE_PROTOCOL) {
                                    let mut error = ErrorResponse::new(Some(
                                        "Use the matching ArchiveBox TLSNotary runtime at /notarize without a query".into(),
                                    ));
                                    *error.status_mut() = StatusCode::BAD_REQUEST;
                                    return Err(error);
                                }
                                resp.headers_mut().insert("Sec-WebSocket-Protocol", WIRE_PROTOCOL.parse().unwrap());
                                Ok(resp)
                            },
                            Some(websocket_config()),
                        )
                        .await?;
                        notarize(WsStream::new(ws), &key, max_recv).await
                    })
                    .await;
                    // No paths, handshake records, request objects, transcripts, or raw protocol errors in logs.
                    eprintln!(
                        "session_finished success={} timeout={} elapsed_ms={}",
                        matches!(outcome, Ok(Ok(()))),
                        outcome.is_err(),
                        started.elapsed().as_millis()
                    );
                });
            }
        }
    }
    Ok(())
}

async fn capture(
    url: &str,
    notary: &str,
    trusted_key: &str,
    output: &PathBuf,
    max_recv: usize,
    prefix_bytes: usize,
) -> Result<()> {
    ensure!(
        prefix_bytes == 0 || (prefix_bytes >= 32768 && prefix_bytes <= max_recv),
        "prefix budget must be between 32 KiB and max_recv"
    );
    let url = url::Url::parse(url)?;
    ensure!(
        url.scheme() == "https"
            && url.port_or_known_default() == Some(443)
            && url.username().is_empty()
            && url.password().is_none(),
        "only HTTPS port 443 URLs without embedded credentials are supported"
    );
    let host = url
        .host_str()
        .ok_or_else(|| anyhow::anyhow!("missing hostname"))?;
    let uri = match url.query() {
        Some(q) => format!("{}?{}", url.path(), q),
        None => url.path().to_owned(),
    };
    let endpoint = url::Url::parse(notary)?;
    ensure!(
        endpoint.scheme() == "wss"
            || (endpoint.scheme() == "ws"
                && matches!(endpoint.host_str(), Some("127.0.0.1" | "localhost"))),
        "notary connection must use WSS (except loopback testing)"
    );
    let tcp = tokio::net::TcpStream::connect((
        endpoint
            .host_str()
            .ok_or_else(|| anyhow::anyhow!("missing notary host"))?,
        endpoint
            .port_or_known_default()
            .ok_or_else(|| anyhow::anyhow!("missing notary port"))?,
    ))
    .await?;
    tcp.set_nodelay(true)?;
    let mut request = notary.into_client_request()?;
    request
        .headers_mut()
        .insert("Sec-WebSocket-Protocol", WIRE_PROTOCOL.parse()?);
    let (ws, response) =
        client_async_tls_with_connector_and_config(request, tcp, None, Some(websocket_config()))
            .await?;
    ensure!(
        response
            .headers()
            .get("Sec-WebSocket-Protocol")
            .and_then(|v| v.to_str().ok())
            == Some(WIRE_PROTOCOL),
        "notary runtime protocol mismatch"
    );
    let (driver, mut handle) = Session::new(WsStream::new(ws)).split();
    let driver = tokio::spawn(driver);
    let _guard = DriverGuard(driver.abort_handle());
    let prover = handle
        .new_prover(ProverConfig::builder().build()?)?
        .commit(
            MpcTlsConfig::builder()
                .max_sent_data(1024)
                .max_recv_data(max_recv)
                .max_sent_records(8)
                .max_recv_records_online(8)
                .build()?,
        )
        .await?;
    let target = tokio::net::TcpStream::connect((host, 443)).await?;
    target.set_nodelay(true)?;
    let (mut tls, prover) = prover.connect(
        TlsClientConfig::builder()
            .server_name(ServerName::Dns(host.try_into()?))
            .root_store(RootCertStore::mozilla())
            .build()?,
        RecordBudget::new(
            target.compat(),
            if prefix_bytes > 0 {
                prefix_bytes
            } else {
                usize::MAX
            },
        ),
    )?;
    let prover_task = tokio::spawn(prover.into_future());
    let _prover_guard = DriverGuard(prover_task.abort_handle());
    if prefix_bytes > 0 {
        // Compression lets the same MPC byte budget authenticate more page content.
        // RecordBudget stops acquisition at an authenticated record boundary;
        // the exact captured length is derived from the signed transcript.
        let request = format!(
            "GET {uri} HTTP/1.1\r\nHost: {host}\r\nAccept: */*\r\nAccept-Encoding: gzip\r\nConnection: close\r\nUser-Agent: ArchiveBox-TLSNotary/0.1\r\n\r\n"
        );
        tls.write_all(request.as_bytes()).await?;
        let mut received = Vec::new();
        tls.read_to_end(&mut received).await?;
        tls.close().await?;
        drop(tls);
    } else {
        let (mut sender, connection) =
            hyper::client::conn::http1::handshake(TokioIo::new(tls.compat())).await?;
        let connection = tokio::spawn(connection);
        let _http_guard = DriverGuard(connection.abort_handle());
        let request = Request::builder()
            .uri(&uri)
            .header("Host", host)
            .header(
                "Accept",
                "text/html,application/json,text/plain;q=0.9,*/*;q=0.1",
            )
            .header("Accept-Encoding", "gzip")
            .header("Connection", "close")
            .header(
                "User-Agent",
                "Mozilla/5.0 (compatible; ArchiveBox TLSNotary/0.1)",
            )
            .body(Empty::<Bytes>::new())?;
        // No cookies are imported or sent in this first capture mode. The request is private from the notary.
        let response = sender.send_request(request).await?;
        let _body = response.into_body().collect().await?;
    }
    let mut prover = prover_task.await??;
    let transcript = prover.transcript().clone();
    let mut commits = TranscriptCommitConfig::builder(&transcript);
    commits
        .commit_sent(0..transcript.sent().len())?
        .commit_recv(0..transcript.received().len())?;
    let commits = commits.build()?;
    let mut config = RequestConfig::builder();
    config.transcript_commit(commits.clone());
    let config = config.build()?;
    let mut prove = ProveConfig::builder(&transcript);
    prove.transcript_commit(commits);
    let prove = prove.build()?;
    ensure!(
        prove.reveal().is_none() && !prove.server_identity(),
        "privacy invariant: no plaintext disclosure to notary"
    );
    let result = prover.prove(&prove).await?;
    let tls = prover.tls_transcript().clone();
    prover.close().await?;
    let mut request = AttestationRequest::builder(&config);
    request
        .server_name(ServerName::Dns(host.try_into()?))
        .handshake_data(HandshakeData {
            certs: tls
                .server_cert_chain()
                .ok_or_else(|| anyhow::anyhow!("missing certificates"))?
                .to_vec(),
            sig: tls
                .server_signature()
                .ok_or_else(|| anyhow::anyhow!("missing server signature"))?
                .clone(),
            binding: tls.certificate_binding().clone(),
        })
        .transcript(transcript.clone())
        .transcript_commitments(result.transcript_secrets, result.transcript_commitments);
    // The serialized request contains only a blinded certificate commitment, never the URL/certificate/HTML.
    let (request, secrets) = request.build(&CryptoProvider::default())?;
    handle.close();
    let mut socket = driver.await??;
    write_frame(&mut socket, &request).await?;
    let attestation: Attestation = read_frame(&mut socket).await?;
    request.validate(&attestation, &CryptoProvider::default())?;
    socket.close().await?;
    let mut proof = secrets.transcript_proof_builder();
    proof
        .reveal_sent(0..transcript.sent().len())?
        .reveal_recv(0..transcript.received().len())?;
    let provider = CryptoProvider::default();
    let mut presentation = attestation.presentation_builder(&provider);
    presentation
        .identity_proof(secrets.identity_proof())
        .transcript_proof(proof.build()?);
    let artifact = bincode::serialize(&presentation.build()?)?;
    let verified = abx_tlsnotary::verify(&artifact, trusted_key)?;
    ensure!(
        prefix_bytes > 0 || verified.response_complete,
        "incomplete HTTP response; use explicit prefix capture mode"
    );
    std::fs::create_dir_all(output)?;
    std::fs::write(output.join("capture.tlsn"), artifact)?;
    std::fs::write(
        output.join("response.body"),
        BASE64_STANDARD.decode(&verified.body_base64)?,
    )?;
    std::fs::write(
        output.join("verified.json"),
        serde_json::to_vec_pretty(&verified)?,
    )?;
    println!("{}", serde_json::to_string(&verified)?);
    Ok(())
}

async fn notarize<S: AsyncRead + AsyncWrite + Send + Unpin + 'static>(
    socket: S,
    key: &k256::ecdsa::SigningKey,
    max_recv: usize,
) -> Result<()> {
    let (failed, failure) = tokio::sync::oneshot::channel();
    tokio::select! {
        result = notarize_session(socket, key, max_recv, failed) => result,
        _ = async {
            if failure.await.is_err() { std::future::pending::<()>().await; }
        } => bail!("notary transport disconnected"),
    }
}

async fn notarize_session<S: AsyncRead + AsyncWrite + Send + Unpin + 'static>(
    socket: S,
    key: &k256::ecdsa::SigningKey,
    max_recv: usize,
    failed: tokio::sync::oneshot::Sender<()>,
) -> Result<()> {
    let (driver, mut handle) = Session::new(socket).split();
    let driver = tokio::spawn(async move {
        let result = driver.await;
        if result.is_err() {
            let _ = failed.send(());
        }
        result
    });
    let _guard = DriverGuard(driver.abort_handle());
    let verifier = match handle
        .new_verifier(
            VerifierConfig::builder()
                .root_store(RootCertStore::mozilla())
                .build()?,
        )?
        .commit()
        .await?
    {
        VerifierCommitStart::Mpc(v) => {
            let c = v.config();
            if c.max_sent_data() > 1024
                || c.max_recv_data() > max_recv
                || c.max_recv_data_online() > 32
                || c.max_sent_records().is_none_or(|n| n > 8)
                || c.max_recv_records_online().is_none_or(|n| n > 8)
                || !c.defer_decryption_from_start()
            {
                v.reject(Some("resource limits exceeded")).await?;
                bail!("limits");
            }
            v.accept().await?.run().await?
        }
        VerifierCommitStart::Proxy(v) => {
            v.reject(Some("private MPC attestations only")).await?;
            bail!("proxy mode disabled");
        }
    };
    let start = verifier.verify().await?;
    if start.request().reveal().is_some() || start.request().server_identity() {
        start
            .reject(Some("plaintext disclosure is forbidden"))
            .await?;
        bail!("privacy");
    }
    if start
        .request()
        .transcript_commit()
        .is_none_or(|c| c.iter_hash().count() != 2)
    {
        start
            .reject(Some("exactly two transcript commitments required"))
            .await?;
        bail!("commitment limits");
    }
    let (verified, verifier) = start.accept().await?;
    ensure!(
        verified.server_name.is_none() && verified.transcript.is_none(),
        "privacy invariant violated"
    );
    let tls = verifier.tls_transcript().clone();
    verifier.close().await?;
    let len = |records: &[tlsn::transcript::Record]| {
        records
            .iter()
            .filter(|r| r.typ == ContentType::ApplicationData)
            .map(|r| r.ciphertext.len())
            .sum::<usize>()
    };
    let sent = len(tls.sent());
    let received = len(tls.recv());
    handle.close();
    let mut socket = driver.await??;
    let request: AttestationRequest = read_frame(&mut socket).await?;
    let mut provider = CryptoProvider::default();
    provider
        .signer
        .set_signer(Box::new(Secp256k1Signer::new(&key.to_bytes())?));
    let mut config = AttestationConfig::builder();
    config.supported_signature_algs(Vec::from_iter(provider.signer.supported_algs()));
    let config = config.build()?;
    let CertBinding::V1_2(binding) = tls.certificate_binding() else {
        bail!("TLS 1.2 required")
    };
    let mut attestation = Attestation::builder(&config).accept_request(request)?;
    attestation
        .connection_info(ConnectionInfo {
            time: tls.time(),
            version: tls.version(),
            transcript_length: TranscriptLength {
                sent: sent.try_into()?,
                received: received.try_into()?,
            },
        })
        .server_ephemeral_key(binding.server_ephemeral_key.clone())
        .transcript_commitments(verified.transcript_commitments);
    write_frame(&mut socket, &attestation.build(&provider)?).await?;
    socket.close().await?;
    Ok(())
}
