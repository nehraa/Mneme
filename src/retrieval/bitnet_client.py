"""
[REAL] BitNet Client — local Falcon3-1B-Instruct inference via OpenAI-compatible HTTP.

Real path: IntentDetector._detect_real() → BitNetClient.detect_intent()

Talks to a `llama-server` (built by BitNet) over HTTP. The server exposes an
OpenAI-compatible API at /v1/chat/completions, so the same httpx pattern used
for every other LLM provider in this project works here unchanged.

SETUP (one-time):
    ./scripts/setup-bitnet.sh              # clone + build + download model
    ./scripts/start-llm-server.sh          # start llama-server on BITNET_PORT

USAGE:
    client = BitNetClient()
    if client.health_check():
        result = client.detect_intent("continue the auth flow")
        # result.intent → "continue_previous_work"
        # result.detected_tags → ["tool=auth", ...]
    else:
        result = client.fallback_intent("continue the auth flow")  # keyword fallback

ENV VARS:
    BITNET_HOST        bind address of llama-server (default: localhost)
    BITNET_PORT        listen port (default: 8081)
    BITNET_MODEL       model alias registered with the server (default: falcon3-1b-instruct)
    BITNET_TIMEOUT     request timeout in seconds (default: 60)
    BITNET_DISABLED    set to "1" to disable the client entirely (mock mode)
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)


# ── Paths (kept for backwards compat / debugging) ────────────────────────────

_REPO_ROOT = Path(__file__).parents[2]  # .../Mneme/
BITNET_DIR = _REPO_ROOT / "BitNet"
LLAMA_CLI_BIN = BITNET_DIR / "build" / "bin" / "llama-cli"
MODEL_GGUF = BITNET_DIR / "models" / "Falcon3-1B-Instruct-1.58bit" / "ggml-model-i2_s.gguf"
TOKENIZER_DIR = BITNET_DIR / "models" / "Falcon3-1B-Instruct-1.58bit"


# ── Config from environment ─────────────────────────────────────────────────

def _bool_env(key: str, default: bool = False) -> bool:
    val = os.environ.get(key, "").strip().lower()
    if val in ("1", "true", "yes", "on"):
        return True
    if val in ("0", "false", "no", "off"):
        return False
    return default


def _int_env(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, str(default)))
    except ValueError:
        return default


# ── Lazy config (read at runtime so load_dotenv() can run first) ───────────────


def _cfg_host() -> str:
    return os.environ.get("BITNET_HOST", "localhost")


def _cfg_port() -> int:
    val = os.environ.get("BITNET_PORT", "").strip()
    return int(val) if val else 8081


def _cfg_model() -> str:
    return os.environ.get("BITNET_MODEL", "bitnet-b1.58-2b-4t")


def _cfg_timeout() -> int:
    val = os.environ.get("BITNET_TIMEOUT", "").strip()
    return int(val) if val else 8


def _cfg_disabled() -> bool:
    val = os.environ.get("BITNET_DISABLED", "").strip().lower()
    return val in ("1", "true", "yes", "on")


def _base_url(host: str, port: int) -> str:
    """Build the base URL for the OpenAI-compatible API."""
    return f"http://{host}:{port}/v1"


# ── Intent detection prompt ─────────────────────────────────────────────────

INTENT_SYSTEM_PROMPT = """Detect intent for the user prompt. Reply ONLY with valid JSON, no prose, no fences.
Example shape: {"intent": "general", "detected_tags": ["tool=auth"]}
Pick exactly one intent from: continue_previous_work, retry_previous_attempt, fix_previous_failure, general.
Pick zero or more tags from: outcome=failed, outcome=work_done, outcome=successfully_called, outcome=stopped, outcome=no_tool_called, tool=auth, tool=db, tool=http, tool=file_io, tool=llm, error=token_expired, error=timeout, error=auth_rejected, error=network.
Output JSON only. No angle brackets. No placeholder text."""


# Valid intent labels. Keep this list as the single source of truth — both
# the parser fallbacks and the test taxonomy reference it. Don't duplicate
# these strings in `_parse_response`.
INTENT_TAXONOMY: tuple[str, ...] = (
    "continue_previous_work",
    "retry_previous_attempt",
    "fix_previous_failure",
    "general",
)


# ── Result type ──────────────────────────────────────────────────────────────


@dataclass
class IntentResult:
    """Structured result from BitNet intent detection."""

    intent: str
    detected_tags: list[str]
    raw_response: str = ""
    degraded: bool = False  # True if produced by keyword fallback

    def to_manifest(self) -> dict[str, Any]:
        """Convert to the manifest format used by RetrievalEngine."""
        impl_note = (
            "Real: retrieval/intent_detector.py::IntentDetector._detect_real() — "
            "BitNet local inference via llama-server OpenAI-compatible API "
            f"({_cfg_host()}:{_cfg_port()}) with {_cfg_model()}"
        )
        if self.degraded:
            impl_note += " [DEGRADED: fallback path used (server unreachable, malformed JSON, or unparseable LLM output)]"
        return {
            "degraded": self.degraded,
            "intent": self.intent,
            "detected_tags": self.detected_tags,
            "raw_response": self.raw_response,
            "_implementation_note": impl_note,
        }


# ── HTTP client ──────────────────────────────────────────────────────────────


def _chat_complete(
    system: str,
    user: str,
    *,
    model: str | None = None,
    temperature: float = 0.2,
    max_tokens: int = 60,  # intent JSON is ~40-60 tokens; less truncates mid-array
    timeout: float | None = None,
) -> str:
    """
    Call /v1/chat/completions on the local llama-server.

    Returns the assistant's content text. Raises httpx.HTTPError on failure.
    """
    payload = {
        "model": model or _cfg_model(),
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    url = f"{_base_url(_cfg_host(), _cfg_port())}/chat/completions"
    with httpx.Client(timeout=timeout or _cfg_timeout()) as client:
        response = client.post(url, json=payload)
        response.raise_for_status()
        data = response.json()
    # OpenAI shape: {"choices": [{"message": {"content": "..."}}]}
    return data["choices"][0]["message"]["content"]


# ── Output parsing ───────────────────────────────────────────────────────────

# Matches a bare taxonomy label in double quotes anywhere in the text. Anchored
# only at the boundaries of the captured group so it doesn't accidentally span
# into surrounding text. Used by fallback (b).
_QUOTED_LABEL_RE = re.compile(
    r'"(' + "|".join(re.escape(label) for label in INTENT_TAXONOMY) + r')"'
)

# Matches prose patterns like:
#   "intent is general"
#   "intent: retry_previous_attempt"
#   "intent = fix_previous_failure"
# Captured group is validated against INTENT_TAXONOMY before use. Used by
# fallback (c).
_PROSE_INTENT_RE = re.compile(
    r"\bintent\b\s*(?:is\s*)?[:=]?\s*[\"']?(\w+)[\"']?",
    re.IGNORECASE,
)


def _extract_from_parsed_json(parsed: dict, raw: str) -> IntentResult | None:
    """
    Build an IntentResult from an already-parsed JSON dict.

    Performs a case-insensitive lookup for the ``intent`` and ``detected_tags``
    keys (Falcon3/BitNet sometimes write ``detected_Tags``), and validates
    ``intent`` against ``INTENT_TAXONOMY``. Returns ``None`` when the parsed
    dict contains an intent value that isn't a real taxonomy label — this
    lets the caller fall through to regex/prose strategies instead of
    accepting the model's echoed template literal (e.g. the raw
    ``<continue_previous_work|...>`` string the model sometimes emits).

    Also rejects responses where the model echoed the ``"tag1"`` placeholder
    in ``detected_tags`` or returned a pipe-separated template string for
    ``intent`` (e.g. ``"a|b|c"``). These are not real values and contaminate
    the downstream tag-match scoring; falling through to the cascade is safer
    than emitting them as ``degraded=False``.
    """
    intent = next(
        (parsed[k] for k in parsed if k.lower() == "intent"),
        "general",
    )
    tags = next(
        (parsed[k] for k in parsed if k.lower() == "detected_tags"),
        [],
    )
    if intent not in INTENT_TAXONOMY:
        return None
    if isinstance(intent, str) and ("<" in intent or ">" in intent or "|" in intent):
        return None
    if isinstance(tags, list) and any(
        isinstance(t, str) and t.strip().lower() in ("tag1", "<tag1>", "tag2", "tag3")
        for t in tags
    ):
        return None
    return IntentResult(intent=intent, detected_tags=tags, raw_response=raw)


def _parse_response(raw: str) -> IntentResult:
    """
    Parse JSON intent from the LLM response, with graded fallbacks.

    BitNet (Falcon3-1B-Instruct) doesn't always emit clean JSON. The parser
    tries the following strategies IN ORDER, returning the first one that
    yields a usable result:

      1. Full-string JSON parse (clean LLM output → degraded=False)
      2. Regex-extracted ``{...}`` block (clean JSON wrapped in prose →
         degraded=False)
      3. Quoted taxonomy label anywhere in the text (fallback b — e.g.
         ``The intent is "general"`` or ``"retry_previous_attempt"`` →
         degraded=True)
      4. ``intent is X`` / ``intent: X`` prose pattern (fallback c →
         degraded=True)
      5. Last resort: ``intent="general"``, empty tags, degraded=True

    Strategies 1–2 return ``degraded=False`` because the LLM produced a
    structured payload. Strategies 3–5 mark ``degraded=True`` so callers can
    tell the result came from a parser fallback rather than the model.
    """
    text = raw.strip()

    # Strip code fences if the model wrapped its answer
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

    # Strategy 1: full-string JSON parse.
    try:
        parsed = json.loads(text)
        result = _extract_from_parsed_json(parsed, raw)
        if result is not None:
            return result
    except json.JSONDecodeError:
        pass

    # Strategy 2: extract a {...} block from prose and parse it.
    match = re.search(r"(\{[\s\S]+\})", text)
    if match:
        try:
            parsed = json.loads(match.group(1))
            result = _extract_from_parsed_json(parsed, raw)
            if result is not None:
                return result
        except json.JSONDecodeError:
            pass

    # Strategy 3 (fallback b): find a quoted taxonomy label anywhere in the
    # raw text. Picks the FIRST match; once we've found one, we stop, even if
    # it appears inside a tag string (taxonomy labels are long, distinct
    # snake_case strings, so the risk of confusion with tag values is low).
    quoted = _QUOTED_LABEL_RE.search(text)
    if quoted:
        return IntentResult(
            intent=quoted.group(1),
            detected_tags=[],
            raw_response=raw,
            degraded=True,
        )

    # Strategy 4 (fallback c): prose pattern "intent is X" / "intent: X".
    prose = _PROSE_INTENT_RE.search(text)
    if prose:
        candidate = prose.group(1).strip().lower()
        if candidate in INTENT_TAXONOMY:
            return IntentResult(
                intent=candidate,
                detected_tags=[],
                raw_response=raw,
                degraded=True,
            )

    # Strategy 5 (last resort): no structured signal at all.
    return IntentResult(
        intent="general",
        detected_tags=[],
        raw_response=raw,
        degraded=True,
    )


# ── Keyword fallback ─────────────────────────────────────────────────────────

_FALLBACK_CONTINUE_RE = re.compile(
    r"\b(continue|resume|pick\s*up|where\s+we\s+left\s+off|previous)\b", re.IGNORECASE
)
_FALLBACK_RETRY_RE = re.compile(
    r"\b(retry|try\s+again|redo|re-?run|again)\b", re.IGNORECASE
)
_FALLBACK_FIX_RE = re.compile(
    r"\b(fix|debug|broken|error\s+in|failure\s+in)\b", re.IGNORECASE
)


def _keyword_fallback(prompt_context: str) -> IntentResult:
    """Cheap regex-based intent guess used when the server is down."""
    tags: list[str] = []
    if _FALLBACK_FIX_RE.search(prompt_context):
        intent = "fix_previous_failure"
        tags.append("outcome=failed")
    elif _FALLBACK_RETRY_RE.search(prompt_context):
        intent = "retry_previous_attempt"
        tags.append("outcome=failed")
    elif _FALLBACK_CONTINUE_RE.search(prompt_context):
        intent = "continue_previous_work"
    else:
        intent = "general"
    return IntentResult(
        intent=intent,
        detected_tags=tags,
        raw_response="<keyword fallback — server unreachable>",
        degraded=True,
    )


# ── Cascade counters ─────────────────────────────────────────────────────────
#
# These counters track how many times each intent-detection path served a call.
# Exposed via get_intent_stats() and read by the /dev/stats endpoint so operators
# can monitor fallback behavior without grepping logs.
#
# Thread safety: a single lock guards the dict because concurrent requests on the
# retrieval server all share the same counters and Python dict updates are not
# atomic under the GIL for compound operations.

_INTENT_STATS: dict[str, int] = {
    "bitnet_ok": 0,
    "bitnet_fail": 0,
    "minimax_ok": 0,
    "minimax_fail": 0,
    "keyword": 0,
}
_INTENT_STATS_LOCK = threading.Lock()


def _inc(key: str) -> None:
    """Increment a counter under the stats lock."""
    with _INTENT_STATS_LOCK:
        _INTENT_STATS[key] = _INTENT_STATS.get(key, 0) + 1


def get_intent_stats() -> dict[str, int]:
    """Snapshot of the intent-detection counters (safe to call concurrently)."""
    with _INTENT_STATS_LOCK:
        return dict(_INTENT_STATS)


def reset_intent_stats() -> None:
    """Reset all intent counters to zero. Intended for tests."""
    with _INTENT_STATS_LOCK:
        for key in _INTENT_STATS:
            _INTENT_STATS[key] = 0


# ── MiniMax intent detection (cascade fallback) ──────────────────────────────


def _minimax_configured() -> bool:
    """True iff a non-blank MINIMAX_API_KEY is set in the environment."""
    return bool(os.environ.get("MINIMAX_API_KEY", "").strip())


def _minimax_intent_timeout() -> float:
    val = os.environ.get("MINIMAX_INTENT_TIMEOUT", "").strip()
    try:
        return float(val) if val else 60.0
    except ValueError:
        return 60.0


def _minimax_intent_model() -> str:
    return os.environ.get("MINIMAX_INTENT_MODEL", "MiniMax-M2.7")


def _minimax_base_url() -> str:
    """Strip trailing /v1 from MINIMAX_BASE_URL so we can append /v1/chat/completions."""
    raw = os.environ.get("MINIMAX_BASE_URL", "https://api.minimax.io").rstrip("/")
    if raw.endswith("/v1"):
        raw = raw[:-3]
    return raw


def _detect_via_minimax(prompt_context: str) -> IntentResult:
    """
    Call the MiniMax chat-completions API for intent detection.

    Uses the same INTENT_SYSTEM_PROMPT and shared _parse_response as BitNet,
    so the two providers produce interchangeable results. Raises RuntimeError
    on any failure so the cascade can fall through to the next path.

    Env vars:
        MINIMAX_API_KEY         required, else RuntimeError
        MINIMAX_BASE_URL        default https://api.minimax.io
        MINIMAX_INTENT_MODEL    default MiniMax-M2.7
        MINIMAX_INTENT_TIMEOUT  seconds, default 60
    """
    api_key = os.environ.get("MINIMAX_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("MINIMAX_API_KEY is not set — MiniMax cascade unavailable")

    url = f"{_minimax_base_url()}/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": _minimax_intent_model(),
        "max_tokens": 80,
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": INTENT_SYSTEM_PROMPT},
            {"role": "user", "content": prompt_context},
        ],
    }
    timeout = _minimax_intent_timeout()

    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()
        content = data["choices"][0]["message"]["content"]
    except (httpx.HTTPError, httpx.RequestError, KeyError, IndexError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"minimax_intent_failed: {type(exc).__name__}: {exc}") from exc

    result = _parse_response(content)
    result.raw_response = content
    return result


# ── Cascade: BitNet → MiniMax → keyword ─────────────────────────────────────


def detect_intent_cascade(prompt_context: str) -> IntentResult:
    """
    Try BitNet, then MiniMax, then keyword heuristics — return the first success.

    Updates INTENT_STATS counters and emits a single log line per call so the
    server log and /dev/stats endpoint agree on which path served each request.

    When BITNET_DISABLED=1, BitNet is skipped entirely and the cascade collapses
    to MiniMax (if configured) → keyword.
    """
    # 1) BitNet
    if not _cfg_disabled():
        try:
            client = BitNetClient()
            result = client._call_llm(prompt_context)
            _inc("bitnet_ok")
            logger.info(
                "intent_path=bitnet intent=%s tags=%s degraded=%s",
                result.intent, result.detected_tags, result.degraded,
            )
            return result
        except Exception as exc:
            _inc("bitnet_fail")
            logger.warning(
                "intent_path=bitnet_fail err=%s — trying MiniMax cascade", exc,
            )

    # 2) MiniMax
    if _minimax_configured():
        try:
            result = _detect_via_minimax(prompt_context)
            _inc("minimax_ok")
            logger.info(
                "intent_path=minimax intent=%s tags=%s degraded=%s",
                result.intent, result.detected_tags, result.degraded,
            )
            return result
        except Exception as exc:
            _inc("minimax_fail")
            logger.warning(
                "intent_path=minimax_fail err=%s — using keyword fallback", exc,
            )
    else:
        logger.debug("intent_path=minimax_skipped reason=no_api_key")

    # 3) Keyword heuristic (always succeeds)
    _inc("keyword")
    result = _keyword_fallback(prompt_context)
    logger.info(
        "intent_path=keyword intent=%s tags=%s",
        result.intent, result.detected_tags,
    )
    return result


# ── Public API ───────────────────────────────────────────────────────────────


def detect_intent(prompt_context: str) -> IntentResult:
    """
    Detect intent and tags from a prompt using BitNet via OpenAI-compatible API.

    Falls back to a keyword-based heuristic if the server is unreachable.
    The returned IntentResult.degraded flag tells the caller whether the
    response came from the LLM (False) or the fallback (True).
    """
    if _cfg_disabled():
        return _keyword_fallback(prompt_context)

    client = BitNetClient()
    return client.detect_intent(prompt_context)


class BitNetClient:
    """
    [REAL] BitNet client for local Falcon3-1B-Instruct intent detection.

    Talks to a llama-server (BitNet build) over an OpenAI-compatible HTTP API.
    All connection details come from environment variables:

        BITNET_HOST   (default: localhost)
        BITNET_PORT   (default: 8081)
        BITNET_MODEL  (default: falcon3-1b-instruct)

    Setup:
        ./scripts/setup-bitnet.sh          # install + download model
        ./scripts/start-llm-server.sh      # start llama-server

    Usage:
        client = BitNetClient()
        if client.health_check():
            result = client.detect_intent("continue the auth flow")
        else:
            result = client.fallback_intent("continue the auth flow")
    """

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        model: str | None = None,
        timeout: float | None = None,
    ) -> None:
        """
        Initialize BitNetClient. All args default to environment variables.
        """
        self.host = host if host is not None else _cfg_host()
        self.port = port if port is not None else _cfg_port()
        self.model = model if model is not None else _cfg_model()
        self.timeout = timeout if timeout is not None else float(_cfg_timeout())
        self._base_url = f"http://{self.host}:{self.port}/v1"
        self._health_url = f"http://{self.host}:{self.port}/health"

    def health_check(self) -> bool:
        """
        Check whether the llama-server is reachable and healthy.

        Returns True only if /health returns 200. Does not raise.
        """
        try:
            with httpx.Client(timeout=min(self.timeout, 5.0)) as client:
                response = client.get(self._health_url)
                return response.status_code == 200
        except (httpx.HTTPError, httpx.RequestError) as e:
            print(
                f"[BITNET] Health check failed ({self.host}:{self.port}): {e}\n"
                f"  Start the server with: ./scripts/start-llm-server.sh",
                file=sys.stderr,
            )
            return False

    def _call_llm(self, prompt_context: str) -> IntentResult:
        """
        Make the BitNet HTTP call and parse the response.

        Raises on any transport, HTTP, or parse failure so callers (notably
        detect_intent_cascade) can decide whether to fall through to MiniMax
        or to the keyword heuristic. Does NOT fall back internally — that's
        the cascade orchestrator's job.
        """
        content = _chat_complete(
            system=INTENT_SYSTEM_PROMPT,
            user=prompt_context,
            model=self.model,
            timeout=self.timeout,
        )
        result = _parse_response(content)
        result.raw_response = content
        return result

    def detect_intent(self, prompt_context: str) -> IntentResult:
        """
        Detect intent and tags from a prompt.

        Calls the LLM server. If the call fails (connection refused, timeout,
        malformed response), falls back to keyword heuristics and marks the
        result as `degraded=True`. Use `detect_intent_cascade()` instead if
        you want MiniMax as a fallback before keyword heuristics.
        """
        if _cfg_disabled():
            return _keyword_fallback(prompt_context)

        try:
            return self._call_llm(prompt_context)
        except (httpx.HTTPError, httpx.RequestError) as e:
            print(
                f"[BITNET] LLM call failed: {e}\n"
                f"  Falling back to keyword heuristics.",
                file=sys.stderr,
            )
            return _keyword_fallback(prompt_context)
        except (KeyError, IndexError, json.JSONDecodeError) as e:
            print(
                f"[BITNET] Malformed LLM response: {e}\n"
                f"  Falling back to keyword heuristics.",
                file=sys.stderr,
            )
            return _keyword_fallback(prompt_context)

    def fallback_intent(self, prompt_context: str) -> IntentResult:
        """Force the keyword-based fallback (useful for tests / degraded mode)."""
        return _keyword_fallback(prompt_context)
