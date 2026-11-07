"""Error types and secret scrubbing shared by every adsops module."""
import re


class ToolError(Exception):
    """A tool failed in a way the caller should see (message is shown, after scrub)."""


class RailViolation(ToolError):
    """A hard rail refused the request (identity mismatch, cap, disallowed status, ...)."""


_PATTERNS = [
    (re.compile(r"-----BEGIN [A-Z ]+-----.*?-----END [A-Z ]+-----", re.S), "[REDACTED-KEY]"),
    (re.compile(r'("private_key"\s*:\s*)"[^"]*"'), r'\1"[REDACTED]"'),
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]+"), "Bearer [REDACTED]"),
    (re.compile(r"ya29\.[A-Za-z0-9._\-]+"), "[REDACTED-TOKEN]"),
    (re.compile(r"ops_[A-Za-z0-9_\-]{20,}"), "[REDACTED-OP]"),
    (re.compile(r"[A-Za-z0-9+/_\-]{48,}={0,2}"), "[REDACTED-BLOB]"),
]


def scrub(text, limit=1500):
    """Remove anything that looks like a credential, then truncate."""
    s = str(text)
    for rx, rep in _PATTERNS:
        s = rx.sub(rep, s)
    return s if len(s) <= limit else s[:limit] + "…"


def selftest():
    assert "ya29" not in scrub("token ya29.abcDEF123-_x here")
    assert "[REDACTED]" in scrub('{"private_key": "abc"}')
    assert "Bearer [REDACTED]" in scrub("Authorization: Bearer abc.def")
    assert "BEGIN" not in scrub("-----BEGIN PRIVATE KEY-----\nMIIE\n-----END PRIVATE KEY-----")
    assert scrub("plain message") == "plain message"
    assert len(scrub("x " * 2000)) <= 1501
    assert issubclass(RailViolation, ToolError)
    return 6
