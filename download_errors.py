"""Allowlisted diagnostics safe to move between processes and write to logs."""

import json
import socket
import ssl


ERROR_CODES = frozenset({
    "creator_redirect_required", "definition_missing",
    "not_found", "http_error", "auth_required", "access_denied", "login_failed", "challenge_required",
    "browser_launch_failed", "browser_missing", "browser_sandbox_failed", "browser_request_failed",
    "dns_error", "tls_error", "network_error", "timeout", "permission_denied", "file_missing",
    "dependency_missing", "os_error", "invalid_response", "response_too_large", "unsafe_url",
    "redirect_refused", "compressed_response", "card_invalid", "image_invalid", "missing_image",
    "private_card", "visibility_unknown", "proxy_disabled", "configuration_error",
    "worker_failed", "worker_invalid_result", "internal_error",
})
ERROR_TYPES = frozenset({
    "SourceError", "Exception", "ValueError", "RuntimeError", "TypeError", "KeyError",
    "TimeoutError", "PermissionError", "FileNotFoundError", "ModuleNotFoundError", "ImportError",
    "ConnectionError", "ConnectionRefusedError", "ConnectionResetError", "OSError", "gaierror",
    "SSLError", "SSLCertVerificationError", "JSONDecodeError", "UnidentifiedImageError",
    "Error", "TargetClosedError", "HTTPError",
})


class SourceError(ValueError):
    def __init__(self, code, message=None, *, http_status=None, error_type="SourceError", exit_code=None):
        self.code = code if isinstance(code, str) and code in ERROR_CODES else "internal_error"
        self.error_type = error_type if isinstance(error_type, str) and error_type in ERROR_TYPES else "Exception"
        self.http_status = http_status if type(http_status) is int and 100 <= http_status <= 599 else None
        self.exit_code = exit_code if type(exit_code) is int and -128 <= exit_code <= 255 else None
        super().__init__(message or self.code)

    def to_payload(self):
        return {"code": self.code, "error_type": self.error_type,
                "http_status": self.http_status, "exit_code": self.exit_code}

    @classmethod
    def from_payload(cls, payload):
        if not isinstance(payload, dict):
            return cls("worker_invalid_result")
        return cls(payload.get("code"), http_status=payload.get("http_status"),
                   error_type=payload.get("error_type"), exit_code=payload.get("exit_code"))


def describe_error(error):
    if isinstance(error, SourceError):
        return error
    if type(error).__name__ == "UnidentifiedImageError" and type(error).__module__.startswith("PIL"):
        return SourceError("image_invalid", error_type="UnidentifiedImageError")
    if type(error).__name__ == "TimeoutError" and type(error).__module__.startswith("playwright."):
        return SourceError("timeout", error_type="TimeoutError")
    for kind, code in ((TimeoutError, "timeout"), (PermissionError, "permission_denied"),
                       (FileNotFoundError, "file_missing"), (ImportError, "dependency_missing"),
                       (socket.gaierror, "dns_error"), (ssl.SSLError, "tls_error"),
                       (ConnectionError, "network_error"), (json.JSONDecodeError, "invalid_response"),
                       (OSError, "os_error"), (ValueError, "invalid_response")):
        if isinstance(error, kind):
            return SourceError(code, error_type=type(error).__name__)
    return SourceError("internal_error", error_type=type(error).__name__)
