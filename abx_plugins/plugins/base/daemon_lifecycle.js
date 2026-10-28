"use strict";

/**
 * Capture SIGTERM/SIGINT immediately, then hand them to a daemon's real
 * shutdown handler once browser setup is far enough along to clean up safely.
 */
function captureShutdownSignals() {
  let pendingSignal = null;
  let shutdownHandler = null;

  const dispatch = (signal) => {
    if (shutdownHandler) {
      shutdownHandler(signal);
    } else if (pendingSignal === null) {
      pendingSignal = signal;
    }
  };
  const onSigterm = () => dispatch("SIGTERM");
  const onSigint = () => dispatch("SIGINT");
  process.on("SIGTERM", onSigterm);
  process.on("SIGINT", onSigint);

  // Signal listeners and unresolved Promises do not keep Node's event loop
  // alive. Keep daemon owners running if their browser connection disappears,
  // so the orchestrator can still deliver the normal shutdown signal and let
  // the owner drain writes and emit its terminal ArchiveResult.
  setInterval(() => {}, 60 * 60 * 1000);

  return function installShutdownHandler(handler) {
    // Keep the OS signal watchers installed: replacing the last listener can
    // discard a signal queued while synchronous startup blocks the event loop.
    shutdownHandler = handler;
    if (pendingSignal !== null) {
      const signal = pendingSignal;
      pendingSignal = null;
      setImmediate(() => handler(signal));
    }
  };
}

module.exports = { captureShutdownSignals };
