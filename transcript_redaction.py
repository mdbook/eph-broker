"""transcript_redaction.py: shared, pure secret-shaped text floor for broker and viewer records."""
import json
import re


# secret-shaped text redacted from a capture (a hermes tool result, a traceback echoing an env var...).
# Belt-and-braces: the streams should hold no secret, but a postmortem file must never be where one lands.
# ReDoS: py3.9 `re` holds the GIL, so a super-linear pattern on a 64K adversarial stream stalls the WHOLE
# broker. Every pattern that must backtrack starts only at a run boundary (lookbehind) and has bounded runs.
_SECRETISH_RES = (
    re.compile(r"-----BEGIN [A-Z ]{0,40}PRIVATE KEY-----.*?(?:-----END [A-Z ]{0,40}PRIVATE KEY-----|\Z)", re.S),
    re.compile(r"\b(?:glpat|gldt|glrt|ghp|gho|ghs|ghu|github_pat|xox[abprs]|sk|hf)[-_][A-Za-z0-9_\-]{12,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"(?<![A-Za-z0-9_\-])eyJ[A-Za-z0-9_\-]{8,4096}\.[A-Za-z0-9_\-]{8,4096}\.[A-Za-z0-9_\-]{8,}"),  # JWT
    re.compile(r"(?i)\b(bearer|basic)\s{1,8}[A-Za-z0-9._~+/=\-]{12,}"),
)
# Same bounded delimiters as the text floor, also tracked across decoded JSON strings.
_PRIVATE_KEY_MARKER_RE = re.compile(r"-----(BEGIN|END) [A-Z ]{0,40}PRIVATE KEY-----")
# KEY=value / "key": "value" / key: value where the key NAME looks secret -> the value is redacted, name kept.
# Limitation: key names are capped at 64 chars either side of the keyword; a longer name goes unredacted.
# token-COUNT metric keys (input_tokens, cache_*_tokens, ...) are an explicit allowlist and never match:
# redacting their ints made every captured stdout JSON unparseable and hid the counts.
_SECRET_KV_RE = re.compile(
    r"""(?ix)(?<![A-Za-z0-9_.\-"'])
          (?!["']?(?:input|output|max|total|cache_(?:read|creation)(?:_input)?)_tokens["']?\s{0,8}[:=])
          (["']?[A-Za-z0-9_.\-]{0,64}(?:token|secret|passw(?:or)?d|passwd|api[_-]?key|apikey|credential|private[_-]?key|auth)
          [A-Za-z0-9_.\-]{0,64}["']?\s{0,8}[:=]\s{0,8})(["']?)([^\s"',}]{4,})""")


def _redact_secretish_text(s):
    for rx in _SECRETISH_RES:
        s = rx.sub("[redacted]", s)
    return _SECRET_KV_RE.sub(lambda m: m.group(1) + m.group(2) + "[redacted]", s)


def _redact_secretish(text):
    """Replace secret-shaped substrings with a marker. Pure; never raises on str input."""
    s = "" if text is None else str(text)
    # Regex replacement on serialized JSON can eat escapes or replace a numeric value
    # with a bare [redacted]. Redact decoded values, then let the encoder own quoting.
    # Result strings may themselves contain JSON; use the same path for those too.
    try:
        obj = json.loads(s)
        private_key_open = False

        def private_key_span(value):
            # Per-string redaction alone loses PEM bodies split across JSON elements.
            # Walk in document order, including keys and values hidden by secret-key
            # redaction, and keep the floor through END (or the rest of the document).
            nonlocal private_key_open
            sensitive = private_key_open
            for marker in _PRIVATE_KEY_MARKER_RE.finditer(value):
                private_key_open = marker.group(1) == "BEGIN"
                sensitive = sensitive or private_key_open
            return sensitive

        def redact(value):
            if isinstance(value, dict):
                result = {}
                for k, v in value.items():
                    key = "[redacted]" if private_key_span(k) else _redact_secretish_text(k)
                    redacted = redact(v)
                    result[key] = "[redacted]" if _SECRET_KV_RE.search(json.dumps(k) + ": xxxx") else redacted
                return result
            if isinstance(value, list):
                return [redact(v) for v in value]
            if isinstance(value, str):
                return "[redacted]" if private_key_span(value) else _redact_secretish(value)
            return "[redacted]" if private_key_open else value
        # A JSON string scalar is already decoded; do not recursively decode it again.
        return json.dumps(_redact_secretish_text(obj) if isinstance(obj, str) else redact(obj))
    except json.JSONDecodeError:
        # Malformed/truncated streams still get the existing best-effort text redaction.
        pass
    except (ValueError, RecursionError):
        # Decoder limits (huge integers/deep nesting) must neither raise nor expose values.
        return '"[redacted]"'
    return _redact_secretish_text(s)
