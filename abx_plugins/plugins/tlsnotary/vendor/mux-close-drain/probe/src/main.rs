use futures::{AsyncReadExt, AsyncWriteExt, future::poll_fn};
use tlsn_mux::{Config, Connection};
use tokio::net::{TcpListener, TcpStream};
use tokio_util::compat::TokioAsyncReadCompatExt;

#[tokio::main]
async fn main() {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let endpoint = listener.local_addr().unwrap();
    let (client_socket, server_socket) =
        tokio::join!(TcpStream::connect(endpoint), listener.accept());
    let mut cfg = Config::default();
    cfg.set_keep_alive(true);
    cfg.set_close_sync(true);
    let mut client = Connection::new(client_socket.unwrap().compat(), cfg.clone());
    let mut server = Connection::new(server_socket.unwrap().0.compat(), cfg);
    let mut cstream = client.handle().unwrap().new_stream(b"final-proof").unwrap();
    let mut sstream = server.new_stream(b"final-proof").unwrap();
    let payload = b"authenticated final proof frame";
    cstream.write_all(payload).await.unwrap();
    client.close();
    let drivers = tokio::spawn(async move {
        let (client_result, server_result) =
            tokio::join!(poll_fn(|cx| client.poll(cx)), poll_fn(|cx| server.poll(cx)),);
        client_result.unwrap();
        server_result.unwrap();
    });
    let mut received = vec![0; payload.len()];
    let result = tokio::time::timeout(
        std::time::Duration::from_secs(2),
        sstream.read_exact(&mut received),
    )
    .await;
    drivers.await.unwrap();
    assert!(
        result.is_ok(),
        "real TCP mux closed without delivering its accepted final frame"
    );
    result.unwrap().unwrap();
    assert_eq!(received, payload);
    println!("final proof frame preserved across real TCP mux close");
}
