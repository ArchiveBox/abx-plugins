// Startup diagnostics must never serialize an arbitrary error message, URL or socket.
const classes = new Set(["Error", "TypeError", "RangeError", "SyntaxError", "AggregateError", "ErrorEvent", "TimeoutError", "ProtocolError", "TargetCloseError"]);
const codes = new Set(["ECONNREFUSED", "ECONNRESET", "ECONNABORTED", "ETIMEDOUT", "EHOSTUNREACH", "ENETUNREACH", "ENOTFOUND", "EAI_AGAIN", "EPIPE", "EACCES", "EPERM", "ENOENT"]);
const syscalls = new Set(["connect", "read", "write", "getaddrinfo", "open", "spawn"]);

const validationMessages = Object.freeze({
  missing_cdp_url: "CHROME_IS_LOCAL=false requires CHROME_CDP_URL or an upstream published chrome session",
  missing_auth_file: "Cookies file not found",
  empty_auth_json: "Invalid JSON cookie export: file is empty",
  invalid_auth_json: "Invalid JSON cookie export: expected a JSON object containing a cookies array",
});
const validationErrors = new WeakMap();

function startupValidationError(reason) {
  if (!Object.hasOwn(validationMessages, reason)) throw new TypeError("Unknown startup validation reason");
  const error = new Error(validationMessages[reason]);
  validationErrors.set(error, reason);
  return error;
}

function startupErrorDiagnostic(error) {
  const result = { errorClass: "unknown", code: "unknown", syscall: "unknown" };
  try {
    const reason = validationErrors.get(error);
    if (reason) {
      result.reason = reason;
      result.message = validationMessages[reason];
    }
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

module.exports = { startupErrorDiagnostic, startupValidationError };
