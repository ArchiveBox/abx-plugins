// Shared native CDP frame subscription for capture hooks and the agent viewer.
async function startPageScreencast(page, onFrame, { quality = 65, fps = 1, scale = 0.5, bringToFront = false } = {}) {
  if (bringToFront) await page.bringToFront();
  const session = await page.createCDPSession();
  let stopped = false, pending = null, timer = null, lastFrameAt = 0;
  const acknowledgements = [];
  const minFrameMs = 1000 / fps;
  const flush = () => {
    timer = null;
    try {
      if (!stopped && pending) {
        lastFrameAt = Date.now();
        onFrame(Buffer.from(pending.data, "base64"), pending.metadata);
      }
    } catch (error) { console.error(`Screencast frame failed: ${error.message}`); }
    finally {
      pending = null;
      // Chrome permits only a bounded number of unacknowledged frames. Releasing
      // them at our delivery rate avoids encoding at 60 Hz and dropping most of
      // that work, while still delivering the first frame of a static page.
      for (const sessionId of acknowledgements) {
        void session.send("Page.screencastFrameAck", { sessionId }).catch(() => {});
      }
      acknowledgements.length = 0;
    }
  };
  const stop = async () => {
    stopped = true; clearTimeout(timer); pending = null;
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
      pending = frame; acknowledgements.push(frame.sessionId);
      const wait = minFrameMs - (Date.now() - lastFrameAt);
      if (wait <= 0) { clearTimeout(timer); flush(); }
      else if (!timer) timer = setTimeout(flush, wait);
    });
    await session.send("Page.startScreencast", {
      format: "jpeg", quality,
      maxWidth: Math.max(1, Math.floor((viewport.clientWidth || 1440) * scale)),
      maxHeight: Math.max(1, Math.floor((viewport.clientHeight || 900) * scale)), everyNthFrame: 1,
    });
    return stop;
  } catch (error) { await stop(); throw error; }
}
module.exports = { startPageScreencast };
