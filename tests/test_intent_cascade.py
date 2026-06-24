"""
Tests for the BitNet → MiniMax → keyword intent detection cascade.

The cascade is invoked when BITNET_INTENT=1 is set and the server is in
real-intent mode. On BitNet failure (timeout, connection error, malformed
response) the cascade falls through to MiniMax; on MiniMax failure it
falls through to keyword heuristics. Module-level counters track how
many times each path served an intent call.
"""
from __future__ import annotations

import os
import threading
from unittest.mock import MagicMock, patch

import pytest

from src.retrieval import bitnet_client


@pytest.fixture(autouse=True)
def _reset_cascade_state(monkeypatch):
    """Reset counters and disable BitNet/MiniMax network calls between tests."""
    bitnet_client.reset_intent_stats()
    monkeypatch.setattr(bitnet_client, "_cfg_disabled", lambda: True)
    yield
    bitnet_client.reset_intent_stats()


class TestGetIntentStats:
    """The stats dict starts at zero and reflects counter increments."""

    def test_starts_empty(self):
        assert bitnet_client.get_intent_stats() == {
            "bitnet_ok": 0,
            "bitnet_fail": 0,
            "minimax_ok": 0,
            "minimax_fail": 0,
            "keyword": 0,
        }

    def test_reset_clears_counters(self):
        bitnet_client._inc("bitnet_ok")
        bitnet_client._inc("minimax_ok")
        bitnet_client.reset_intent_stats()
        assert bitnet_client.get_intent_stats()["bitnet_ok"] == 0
        assert bitnet_client.get_intent_stats()["minimax_ok"] == 0

    def test_concurrent_increments_are_safe(self):
        """Multiple threads incrementing the same counter must not lose updates."""
        bitnet_client.reset_intent_stats()
        threads = []
        for _ in range(8):
            t = threading.Thread(target=lambda: [bitnet_client._inc("bitnet_ok") for _ in range(100)])
            threads.append(t)
            t.start()
        for t in threads:
            t.join()
        assert bitnet_client.get_intent_stats()["bitnet_ok"] == 800


class TestMiniMaxConfigGuard:
    """The MiniMax path is only attempted when an API key is configured."""

    def test_minimax_skipped_without_api_key(self, monkeypatch):
        monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
        assert bitnet_client._minimax_configured() is False

    def test_minimax_used_with_api_key(self, monkeypatch):
        monkeypatch.setenv("MINIMAX_API_KEY", "sk-test-key")
        assert bitnet_client._minimax_configured() is True

    def test_minimax_blank_key_treated_as_unset(self, monkeypatch):
        monkeypatch.setenv("MINIMAX_API_KEY", "   ")
        assert bitnet_client._minimax_configured() is False


class TestDetectViaMiniMax:
    """_detect_via_minimax posts to the OpenAI-compatible MiniMax endpoint."""

    def test_successful_call_parses_response(self, monkeypatch):
        """A 200 response with valid JSON intent is parsed via the shared parser."""
        monkeypatch.setenv("MINIMAX_API_KEY", "sk-test-key")
        monkeypatch.setenv("MINIMAX_BASE_URL", "https://api.minimax.io/v1")

        fake_response = MagicMock()
        fake_response.json.return_value = {
            "choices": [{"message": {"content": '{"intent": "general", "detected_tags": ["tool=http"]}'}}]
        }
        fake_response.raise_for_status = MagicMock()

        with patch("src.retrieval.bitnet_client.httpx.Client") as mock_client_cls:
            ctx = MagicMock()
            ctx.__enter__.return_value.post.return_value = fake_response
            mock_client_cls.return_value = ctx

            result = bitnet_client._detect_via_minimax("continue auth flow")

        assert result.intent == "general"
        assert result.detected_tags == ["tool=http"]
        assert result.degraded is False
        assert "tool=http" in result.raw_response

    def test_http_error_raises_runtimeerror(self, monkeypatch):
        """An HTTP error from MiniMax must raise so the cascade can fall through."""
        import httpx

        monkeypatch.setenv("MINIMAX_API_KEY", "sk-test-key")

        with patch("src.retrieval.bitnet_client.httpx.Client") as mock_client_cls:
            ctx = MagicMock()
            ctx.__enter__.return_value.post.side_effect = httpx.HTTPError("boom")
            mock_client_cls.return_value = ctx

            with pytest.raises(RuntimeError, match="minimax_intent_failed"):
                bitnet_client._detect_via_minimax("anything")

    def test_missing_api_key_raises(self, monkeypatch):
        monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="MINIMAX_API_KEY"):
            bitnet_client._detect_via_minimax("anything")


class TestDetectIntentCascade:
    """The cascade orchestrates BitNet → MiniMax → keyword in order."""

    def test_disabled_mode_skips_to_keyword(self, monkeypatch):
        """When BITNET_DISABLED=1, only the keyword path runs and counter increments."""
        monkeypatch.setattr(bitnet_client, "_cfg_disabled", lambda: True)
        monkeypatch.setattr(bitnet_client, "_minimax_configured", lambda: False)
        result = bitnet_client.detect_intent_cascade("continue the auth flow")
        assert result.degraded is True
        assert bitnet_client.get_intent_stats() == {
            "bitnet_ok": 0, "bitnet_fail": 0,
            "minimax_ok": 0, "minimax_fail": 0,
            "keyword": 1,
        }

    def test_bitnet_success_short_circuits(self, monkeypatch):
        """Successful BitNet call increments bitnet_ok and skips MiniMax/keyword."""
        monkeypatch.setattr(bitnet_client, "_cfg_disabled", lambda: False)
        good = bitnet_client.IntentResult(
            intent="general", detected_tags=[], raw_response="{}",
        )
        with patch.object(bitnet_client.BitNetClient, "_call_llm", return_value=good):
            result = bitnet_client.detect_intent_cascade("hello")
        assert result is good
        assert bitnet_client.get_intent_stats()["bitnet_ok"] == 1
        assert bitnet_client.get_intent_stats()["minimax_ok"] == 0
        assert bitnet_client.get_intent_stats()["keyword"] == 0

    def test_bitnet_fail_falls_through_to_minimax(self, monkeypatch):
        """When BitNet raises, the cascade tries MiniMax and counts both events."""
        monkeypatch.setattr(bitnet_client, "_cfg_disabled", lambda: False)
        monkeypatch.setenv("MINIMAX_API_KEY", "sk-test-key")

        minimax_result = bitnet_client.IntentResult(
            intent="general", detected_tags=["tool=auth"], raw_response="{}",
        )
        with patch.object(bitnet_client.BitNetClient, "_call_llm", side_effect=RuntimeError("bitnet down")):
            with patch.object(bitnet_client, "_detect_via_minimax", return_value=minimax_result):
                result = bitnet_client.detect_intent_cascade("auth retry")

        assert result is minimax_result
        stats = bitnet_client.get_intent_stats()
        assert stats["bitnet_fail"] == 1
        assert stats["minimax_ok"] == 1
        assert stats["keyword"] == 0

    def test_both_fail_falls_to_keyword(self, monkeypatch):
        """When both BitNet and MiniMax fail, the keyword heuristic runs."""
        monkeypatch.setattr(bitnet_client, "_cfg_disabled", lambda: False)
        monkeypatch.setenv("MINIMAX_API_KEY", "sk-test-key")

        with patch.object(bitnet_client.BitNetClient, "_call_llm", side_effect=RuntimeError("bitnet down")):
            with patch.object(bitnet_client, "_detect_via_minimax", side_effect=RuntimeError("minimax too")):
                result = bitnet_client.detect_intent_cascade("retry previous attempt")

        assert result.degraded is True
        assert result.intent == "retry_previous_attempt"
        stats = bitnet_client.get_intent_stats()
        assert stats["bitnet_fail"] == 1
        assert stats["minimax_fail"] == 1
        assert stats["keyword"] == 1

    def test_bitnet_fail_no_minimax_key_falls_to_keyword(self, monkeypatch):
        """When BitNet fails and no MiniMax key is set, skip MiniMax and use keyword."""
        monkeypatch.setattr(bitnet_client, "_cfg_disabled", lambda: False)
        monkeypatch.delenv("MINIMAX_API_KEY", raising=False)

        with patch.object(bitnet_client.BitNetClient, "_call_llm", side_effect=RuntimeError("bitnet down")):
            result = bitnet_client.detect_intent_cascade("general query")

        assert result.degraded is True
        assert result.intent == "general"
        stats = bitnet_client.get_intent_stats()
        assert stats["bitnet_fail"] == 1
        assert stats["minimax_ok"] == 0
        assert stats["minimax_fail"] == 0
        assert stats["keyword"] == 1


class TestParseResponsePlaceholderGuard:
    """The parser must reject template/placeholder echoes from the model.

    BitNet sometimes returns the literal system-prompt template (angle
    brackets, pipe-separated labels, or the ``"tag1"`` placeholder) instead
    of a selected value. The parser must treat these as parse failures so
    the cascade falls through to MiniMax/keyword instead of contaminating
    tag-match scoring with garbage.
    """

    def test_rejects_intent_template_with_angle_brackets(self):
        """The pipe-separated template wrapped in <> is not a valid intent."""
        raw = '{"intent": "<continue_previous_work|retry_previous_attempt|fix_previous_failure|general>", "detected_tags": []}'
        result = bitnet_client._parse_response(raw)
        # Strategy 3 (quoted label) catches "general" from the template — degraded=True
        assert result.intent == "general"
        assert result.degraded is True

    def test_rejects_tag1_placeholder_in_tags(self):
        """The literal ``"tag1"`` placeholder must trigger fallback, not pass through."""
        raw = '{"intent": "general", "detected_tags": ["tag1"]}'
        result = bitnet_client._parse_response(raw)
        # Template echo isn't a real intent — fallback path runs
        assert result.degraded is True

    def test_rejects_tag1_among_real_tags(self):
        """If ``tag1`` is in a list with real tags, the whole response is suspect."""
        raw = '{"intent": "general", "detected_tags": ["tool=auth", "tag1"]}'
        result = bitnet_client._parse_response(raw)
        assert result.degraded is True

    def test_clean_json_passes_through(self):
        """A well-formed response is returned as degraded=False."""
        raw = '{"intent": "continue_previous_work", "detected_tags": ["tool=auth"]}'
        result = bitnet_client._parse_response(raw)
        assert result.intent == "continue_previous_work"
        assert result.detected_tags == ["tool=auth"]
        assert result.degraded is False

    def test_prose_with_template_echo_falls_through(self):
        """When the LLM responds with prose containing the template, regex catches a real label."""
        raw = 'I think the intent is "fix_previous_failure" for this prompt.'
        result = bitnet_client._parse_response(raw)
        assert result.intent == "fix_previous_failure"
        assert result.degraded is True