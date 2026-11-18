// Startup diagnostics must never serialize an error's message, URL or socket.
const classes = new Set(["Error", "TypeError", "RangeError", "SyntaxError", "AggregateError", "ErrorEvent", "TimeoutError", "ProtocolError", "TargetCloseError"]);
const codes = new Set(["ECONNREFUSED", "ECONNRESET", "ECONNABORTED", "ETIMEDOUT", "EHOSTUNREACH", "ENETUNREACH", "ENOTFOUND", "EAI_AGAIN", "EPIPE", "EACCES", "EPERM", "ENOENT"]);
const syscalls = new Set(["connect", "read", "write", "getaddrinfo", "open", "spawn"]);

function startupErrorDiagnostic(error) {
  const result = { errorClass: "unknown", code: "unknown", syscall: "unknown" };
  try {
    const name = error?.constructor?.name;
    if (classes.has(name)) result.errorClass = name;
    // WebSocket libraries can reject with ErrorEvent rather than Error.
    // Inspect only a bounded chain, never stringify the surrounding event.
    const candidates = [error, error?.error, error?.cause, error?.error?.errors?.[0]];
    for (const candidate of candidates) {
      const code = candidate?.code;
      const syscall = candidate?.syscall;
      if (result.code === "unknown" && codes.has(code)) result.code = code;
      if (result.syscall === "unknown" && syscalls.has(syscall)) result.syscall = syscall;
    }
  } catch (_) {
    // Reporting must not replace the original failure or prevent cleanup.
  }
  return result;
}

module.exports = { startupErrorDiagnostic };
