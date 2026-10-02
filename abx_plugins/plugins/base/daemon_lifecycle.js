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
