// Shared native CDP frame subscription for capture hooks and the agent viewer.
async function startPageScreencast(page, onFrame, { quality = 65, fps = 1, scale = 0.5, bringToFront = false } = {}) {
  if (bringToFront) await page.bringToFront();
  const session = await page.createCDPSession();
  let stopped = false, pending = null, timer = null, lastFrameAt = 0;
  let navigation = Promise.resolve();
  let captureOptions;
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
  const navigated = frame => {
    if (stopped || frame !== page.mainFrame()) return;
    clearTimeout(timer); timer = null; pending = null;
    // A new document can replace the renderer surface while leaving the CDP
    // target alive. Bind the native capture to that document before publishing
    // more frames, and serialize transitions with final subscription cleanup.
    navigation = navigation.then(async () => {
      if (stopped) return;
      await session.send("Page.stopScreencast");
      if (!stopped) await session.send("Page.startScreencast", captureOptions);
    }).catch(error => {
      if (!stopped) console.error(`Screencast navigation failed: ${error.message}`);
    });
  };
  const stop = async () => {
    stopped = true; clearTimeout(timer); pending = null;
    page.off("framenavigated", navigated);
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
      pending = frame;
      // Acknowledge receipt immediately: Chrome drops paints when its frame
      // window fills, so delaying ACKs can lose a static page's final image.
      // Throttle delivery below while retaining the latest received frame.
      void session.send("Page.screencastFrameAck", { sessionId: frame.sessionId }).catch(() => {});
      const wait = minFrameMs - (Date.now() - lastFrameAt);
      if (wait <= 0) { clearTimeout(timer); flush(); }
      else if (!timer) timer = setTimeout(flush, wait);
    });
    captureOptions = {
      format: "jpeg", quality,
      maxWidth: Math.max(1, Math.floor((viewport.clientWidth || 1440) * scale)),
      maxHeight: Math.max(1, Math.floor((viewport.clientHeight || 900) * scale)), everyNthFrame: 1,
    };
    await session.send("Page.startScreencast", captureOptions);
    page.on("framenavigated", navigated);
    return stop;
  } catch (error) { await stop(); throw error; }
}
module.exports = { startPageScreencast };
