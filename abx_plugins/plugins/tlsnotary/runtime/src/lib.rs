//! Shared offline verifier. Native CLI and browser WASM use identical checks.
use anyhow::{Result, bail, ensure};
use base64::{Engine, prelude::BASE64_STANDARD};
use bincode::Options;
use serde::Serialize;
use sha2::{Digest, Sha256};
use std::io::Read;
use tlsn_attestation::{CryptoProvider, presentation::Presentation, signing::KeyAlgId};

pub const MAX_ARTIFACT: usize = 32 * 1024 * 1024;
fn complete<T>(status: httparse::Status<T>) -> Result<T> {
    match status {
        httparse::Status::Complete(value) => Ok(value),
        httparse::Status::Partial => bail!("incomplete HTTP message"),
    }
}
pub fn decode<T: serde::de::DeserializeOwned>(bytes: &[u8]) -> Result<T> {
    ensure!(
        bytes.len() <= MAX_ARTIFACT,
        "artifact exceeds 32 MiB verification limit"
    );
    Ok(bincode::DefaultOptions::new()
        .with_fixint_encoding()
        .with_limit(MAX_ARTIFACT as u64)
        .reject_trailing_bytes()
        .deserialize(bytes)?)
}

#[derive(Serialize)]
pub struct Verified {
    pub format: &'static str,
    pub url: String,
    pub server_name: String,
    pub connection_time_unix: u64,
    pub notary_key: String,
    pub status: u16,
    pub content_type: String,
    pub body_sha256: String,
    pub body_base64: String,
    pub request_base64: String,
    pub response_base64: String,
}

pub fn verify(bytes: &[u8], trusted_key: &str) -> Result<Verified> {
    let presentation: Presentation = decode(bytes)?;
    let key = presentation.verifying_key();
    ensure!(
        key.alg == KeyAlgId::K256,
        "unsupported notary key algorithm"
    );
    ensure!(
        !trusted_key.is_empty() && hex::decode(trusted_key)? == key.data,
        "notary key is not trusted"
    );
    let notary_key = hex::encode(&key.data);
    let output = presentation.verify(&CryptoProvider::default())?;
    let server_name = output
        .server_name
        .ok_or_else(|| anyhow::anyhow!("missing server identity proof"))?
        .to_string();
    let transcript = output
        .transcript
        .ok_or_else(|| anyhow::anyhow!("missing transcript proof"))?;
    ensure!(
        transcript.is_complete(),
        "incomplete transcript: this artifact must authenticate every saved byte"
    );
    let sent = transcript.sent_unsafe();
    let recv = transcript.received_unsafe();
    let mut request_headers = [httparse::EMPTY_HEADER; 64];
    let mut request = httparse::Request::new(&mut request_headers);
    let request_end = complete(request.parse(sent)?)?;
    ensure!(
        request_end == sent.len() && request.method == Some("GET"),
        "expected exactly one GET without a request body"
    );
    let target = request
        .path
        .ok_or_else(|| anyhow::anyhow!("missing request target"))?;
    ensure!(
        target.starts_with('/') && !target.starts_with("//"),
        "invalid origin-form target"
    );
    let hosts: Vec<_> = request
        .headers
        .iter()
        .filter(|h| h.name.eq_ignore_ascii_case("host"))
        .collect();
    ensure!(
        hosts.len() == 1 && std::str::from_utf8(hosts[0].value)?.eq_ignore_ascii_case(&server_name),
        "HTTP Host does not match authenticated TLS identity"
    );
    let url = format!("https://{server_name}{target}");
    let mut response_headers = [httparse::EMPTY_HEADER; 128];
    let mut response = httparse::Response::new(&mut response_headers);
    let body_start = complete(response.parse(recv)?)?;
    let header = |name: &str| -> Result<Option<String>> {
        let values: Vec<_> = response
            .headers
            .iter()
            .filter(|h| h.name.eq_ignore_ascii_case(name))
            .collect();
        ensure!(values.len() <= 1, "duplicate {name} header");
        values
            .first()
            .map(|h| Ok(std::str::from_utf8(h.value)?.to_string()))
            .transpose()
    };
    let encoding = header("content-encoding")?
        .unwrap_or_else(|| "identity".into())
        .to_ascii_lowercase();
    ensure!(
        matches!(encoding.as_str(), "identity" | "gzip"),
        "unsupported content encoding"
    );
    let raw_body = &recv[body_start..];
    let body = match header("transfer-encoding")? {
        Some(value) => {
            ensure!(
                value.eq_ignore_ascii_case("chunked") && header("content-length")?.is_none(),
                "ambiguous/unsupported HTTP framing"
            );
            let mut remaining = raw_body;
            let mut body = Vec::new();
            loop {
                let (offset, size) = complete(
                    httparse::parse_chunk_size(remaining)
                        .map_err(|_| anyhow::anyhow!("invalid HTTP chunk size"))?,
                )?;
                remaining = &remaining[offset..];
                let size = usize::try_from(size)?;
                if size == 0 {
                    ensure!(
                        remaining == b"\r\n",
                        "unexpected HTTP trailers or trailing bytes"
                    );
                    break;
                }
                ensure!(size <= remaining.len().saturating_sub(2), "truncated chunk");
                body.extend_from_slice(&remaining[..size]);
                ensure!(
                    &remaining[size..size + 2] == b"\r\n",
                    "invalid chunk terminator"
                );
                remaining = &remaining[size + 2..];
            }
            body
        }
        None => {
            if let Some(length) = header("content-length")? {
                ensure!(
                    length.parse::<usize>()? == raw_body.len(),
                    "incomplete response body"
                );
            }
            raw_body.to_vec()
        }
    };
    let body = if encoding == "gzip" {
        let mut decoded = Vec::new();
        flate2::read::MultiGzDecoder::new(&body[..])
            .take(16 * 1024 * 1024 + 1)
            .read_to_end(&mut decoded)?;
        ensure!(
            decoded.len() <= 16 * 1024 * 1024,
            "decompressed response exceeds 16 MiB"
        );
        decoded
    } else {
        body
    };
    let status = response
        .code
        .ok_or_else(|| anyhow::anyhow!("missing HTTP status"))?;
    if status < 200 {
        bail!("interim response cannot be the archive");
    }
    Ok(Verified {
        format: "archivebox-tlsnotary-v1",
        url,
        server_name,
        connection_time_unix: output.connection_info.time,
        notary_key,
        status,
        content_type: header("content-type")?.unwrap_or_else(|| "application/octet-stream".into()),
        body_sha256: hex::encode(Sha256::digest(&body)),
        body_base64: BASE64_STANDARD.encode(body),
        request_base64: BASE64_STANDARD.encode(sent),
        response_base64: BASE64_STANDARD.encode(recv),
    })
}

#[cfg(feature = "web")]
#[wasm_bindgen::prelude::wasm_bindgen]
pub fn verify_artifact(bytes: &[u8], trusted_key: &str) -> Result<String, wasm_bindgen::JsValue> {
    verify(bytes, trusted_key)
        .and_then(|result| Ok(serde_json::to_string(&result)?))
        .map_err(|e| wasm_bindgen::JsValue::from_str(&e.to_string()))
}
