// Shared native CDP frame subscription for capture hooks and the agent viewer.
async function startPageScreencast(page, onFrame, { quality = 65, fps = 1, scale = 0.5, bringToFront = false } = {}) {
  if (bringToFront) await page.bringToFront();
  const session = await page.createCDPSession();
  let stopped = false, pending = null, timer = null, lastFrameAt = 0;
  let navigation = Promise.resolve();
  let captureOptions, acquiring = false;
  const minFrameMs = 1000 / fps;
  const flush = () => {
    timer = null;
    try {
      if (!stopped && pending) {
        lastFrameAt = Date.now();
        onFrame(Buffer.from(pending.data, "base64"), pending.metadata);
      }
    } catch (error) { console.error(`Screencast frame failed: ${error.message}`); }
    finally { pending = null; }
  };
  const navigated = () => {
    if (stopped) return;
    clearTimeout(timer); timer = null; pending = null; acquiring = false;
    // Frame commit can precede an active renderer page, so restarting there
    // fails with "Not attached to an active page". DOMContentLoaded belongs to
    // the ready main document. Rebind its surface and serialize final cleanup.
    navigation = navigation.then(async () => {
      if (stopped) return;
      clearTimeout(timer); timer = null; pending = null;
      await session.send("Page.stopScreencast");
      if (!stopped) {
        acquiring = true;
        await session.send("Page.startScreencast", captureOptions);
      }
    }).catch(error => {
      if (!stopped) console.error(`Screencast navigation failed: ${error.message}`);
    });
  };
  const stop = async () => {
    stopped = true; acquiring = false; clearTimeout(timer); pending = null;
    page.off("domcontentloaded", navigated);
    await navigation;
    session.removeAllListeners("Page.screencastFrame");
    try { await session.send("Page.stopScreencast"); } catch {}
    try { await session.detach(); } catch {}
  };
  try {
    await session.send("Page.enable");
    const metrics = await session.send("Page.getLayoutMetrics");
    const viewport = metrics.visualViewport || metrics.layoutViewport || {};
    session.on("Page.screencastFrame", frame => {
      if (stopped) return;
      const firstFrame = acquiring;
      acquiring = false;
      if (firstFrame) pending = frame;
      // A late encoded frame can arrive after stop has completed and flush has
      // cleared pending. Only the first frame owns this acquisition cycle.
      // Send stop before ACK frees a native frame slot for another paint.
      const stopCapture = firstFrame ? session.send("Page.stopScreencast") : null;
      // Acknowledge receipt immediately: Chrome drops paints when its frame
      // window fills, so delaying ACKs can lose a static page's final image.
      // Stop native acquisition between samples instead of encoding frames
      // that delivery would discard. Restart also captures a static final paint.
      void session.send("Page.screencastFrameAck", { sessionId: frame.sessionId }).catch(() => {});
      if (!firstFrame) return;
      navigation = Promise.all([navigation, stopCapture]).then(() => {
        if (stopped) return;
        clearTimeout(timer); flush();
        timer = setTimeout(() => {
          timer = null;
          navigation = navigation.then(async () => {
            if (!stopped) {
              acquiring = true;
              await session.send("Page.startScreencast", captureOptions);
            }
          }).catch(error => {
            if (!stopped) console.error(`Screencast frame failed: ${error.message}`);
          });
        }, Math.max(0, minFrameMs - (Date.now() - lastFrameAt)));
      }).catch(error => {
        if (!stopped) console.error(`Screencast frame failed: ${error.message}`);
      });
    });
    captureOptions = {
      format: "jpeg", quality,
      maxWidth: Math.max(1, Math.floor((viewport.clientWidth || 1440) * scale)),
      maxHeight: Math.max(1, Math.floor((viewport.clientHeight || 900) * scale)), everyNthFrame: 1,
      // Chrome otherwise starts up to three encodes before our first-frame stop.
      maxFramesInFlight: 1,
    };
    acquiring = true;
    await session.send("Page.startScreencast", captureOptions);
    page.on("domcontentloaded", navigated);
    return stop;
  } catch (error) { await stop(); throw error; }
}
module.exports = { startPageScreencast };
