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
    shutdownHandler = handler;
    process.removeListener("SIGTERM", onSigterm);
    process.removeListener("SIGINT", onSigint);
    process.on("SIGTERM", () => handler("SIGTERM"));
    process.on("SIGINT", () => handler("SIGINT"));
    if (pendingSignal !== null) {
      const signal = pendingSignal;
      pendingSignal = null;
      setImmediate(() => handler(signal));
    }
  };
}

module.exports = { captureShutdownSignals };
