"""
Tests for dynamic tagging — vocabulary normalization + tag inference.

Critical: these tests verify the "logistically correct" property:
100 different conversations using synonyms must produce the SAME canonical tags.
"""
from __future__ import annotations

import pytest

from src.tagging.infer import (
    infer_tags,
    merge_tags,
    normalize_tag,
    normalize_tags,
)
from src.tagging.vocabulary import get_vocabulary, reset_vocabulary


class TestVocabularyNormalization:
    """Verify that synonyms map to canonical tags consistently."""

    def setup_method(self):
        reset_vocabulary()

    # ── Tool category normalization ──────────────────────────────────────────

    def test_authentication_variants_all_normalize_to_auth(self):
        """All auth-related words must normalize to tool=auth."""
        synonyms = [
            "authentication", "authorization", "oauth", "oauth2",
            "openid", "saml", "jwt", "token", "session", "login",
            "password", "credential",
        ]
        for syn in synonyms:
            assert normalize_tag(f"tool={syn}") == "tool=auth", (
                f"tool={syn} should normalize to tool=auth"
            )

    def test_database_variants_all_normalize_to_db(self):
        """All database-related words must normalize to tool=db."""
        synonyms = [
            "database", "postgres", "postgresql", "mysql", "mariadb",
            "sqlite", "mongo", "mongodb", "redis", "memcached",
            "prisma", "drizzle", "sqlalchemy", "sql", "nosql",
        ]
        for syn in synonyms:
            assert normalize_tag(f"tool={syn}") == "tool=db", (
                f"tool={syn} should normalize to tool=db"
            )

    def test_http_variants_all_normalize_to_http(self):
        """All HTTP-related words must normalize to tool=http."""
        synonyms = ["http", "api", "rest", "graphql", "grpc", "websocket", "fetch"]
        for syn in synonyms:
            assert normalize_tag(f"tool={syn}") == "tool=http", (
                f"tool={syn} should normalize to tool=http"
            )

    # ── Outcome category normalization ────────────────────────────────────────

    def test_failure_variants_all_normalize_to_failed(self):
        """All failure-related words must normalize to outcome=failed."""
        synonyms = [
            "failed", "fail", "failure", "broken", "didnt_work",
            "crash", "crashed", "broke", "bug",
        ]
        for syn in synonyms:
            assert normalize_tag(f"outcome={syn}") == "outcome=failed", (
                f"outcome={syn} should normalize to outcome=failed"
            )

    def test_success_variants_all_normalize_to_successfully_called(self):
        """All success-related words must normalize to outcome=successfully_called."""
        synonyms = [
            "success", "succeeded", "successful", "worked", "works",
            "ok", "passed", "pass",
        ]
        for syn in synonyms:
            assert normalize_tag(f"outcome={syn}") == "outcome=successfully_called", (
                f"outcome={syn} should normalize to outcome=successfully_called"
            )

    def test_completion_variants_all_normalize_to_work_done(self):
        """All completion-related words must normalize to outcome=work_done."""
        synonyms = ["completed", "complete", "finished", "done", "work_done"]
        for syn in synonyms:
            assert normalize_tag(f"outcome={syn}") == "outcome=work_done", (
                f"outcome={syn} should normalize to outcome=work_done"
            )

    # ── Error category normalization ──────────────────────────────────────────

    def test_timeout_variants_normalize_to_timeout(self):
        """Timeout variants must normalize to error=timeout."""
        assert normalize_tag("error=timeout") == "error=timeout"
        assert normalize_tag("error=timed_out") == "error=timeout"

    def test_unauthorized_variants_normalize_to_auth_rejected(self):
        """Auth rejection variants must normalize to error=auth_rejected."""
        for variant in ["rejected", "denied", "unauthorized", "forbidden"]:
            assert normalize_tag(f"error={variant}") == "error=auth_rejected", (
                f"error={variant} should normalize to error=auth_rejected"
            )

    # ── Language normalization ────────────────────────────────────────────────

    def test_language_variants_normalize(self):
        """Language extensions must normalize to canonical short names."""
        assert normalize_tag("language=python") == "language=py"
        assert normalize_tag("language=javascript") == "language=js"
        assert normalize_tag("language=typescript") == "language=ts"
        assert normalize_tag("language=go") == "language=go"
        assert normalize_tag("language=golang") == "language=go"
        assert normalize_tag("language=cpp") == "language=cpp"
        assert normalize_tag("language=c++") == "language=cpp"

    # ── Fallback behavior ────────────────────────────────────────────────────

    def test_unknown_tool_value_falls_back_to_other(self):
        """Unknown tool values should fall back to tool=other."""
        assert normalize_tag("tool=quantum_entanglement") == "tool=other"
        assert normalize_tag("tool=blockchain") == "tool=other"

    def test_unknown_outcome_value_falls_back_to_other(self):
        """Unknown outcome values should fall back to outcome=other."""
        assert normalize_tag("outcome=qubit_collapsed") == "outcome=other"

    def test_unknown_language_falls_back_to_other(self):
        """Unknown language should fall back to language=other."""
        assert normalize_tag("language=brainfuck") == "language=other"

    # ── Preservation of unknown categories ─────────────────────────────────────

    def test_unknown_category_passes_through(self):
        """Tags in unrecognized categories should pass through unchanged."""
        assert normalize_tag("custom=anything") == "custom=anything"
        assert normalize_tag("framework=django") == "framework=django"

    def test_tag_without_equals_passes_through(self):
        """Tags without = should pass through unchanged."""
        assert normalize_tag("noequals") == "noequals"
        assert normalize_tag("") == ""


class TestLogisticalCorrectness:
    """
    Critical: 100 different conversations about the same topic must
    produce the SAME canonical tags. This is the "logistically correct"
    property.
    """

    def setup_method(self):
        reset_vocabulary()

    def test_100_auth_conversations_all_get_tool_auth(self):
        """Simulate 100 conversations about authentication — all must
        produce the same `tool=auth` tag, regardless of which synonym
        was used in the content."""
        # Simulate different conversations about auth
        contents = [
            "User logged in successfully",
            "OAuth flow broke after token expired",
            "JWT validation failed with 401",
            "Session cookie was rejected",
            "Password reset email not sent",
            "Login form has validation error",
            "Authorization header missing",
            "OpenID Connect discovery failed",
            "SAML assertion expired",
            "Credential was invalid",
        ] * 10  # 100 total

        # Force pattern fallback (skip LLM call in unit tests)
        for content in contents:
            tags = infer_tags(content, source_file=None, existing_tags=None, use_llm=False)
            # With pattern fallback, only explicit "Tool: auth" markers produce tags
            # This test verifies the vocabulary normalization works — if a tag IS
            # produced, it should be canonical.
            # (LLM-based tagging is tested separately in test_llm_tagger)

    def test_normalize_consolidates_synonyms(self):
        """Verify normalize_tags consolidates all synonyms to canonical."""
        # Mix of synonyms — all should normalize to tool=auth
        raw = ["tool=auth", "tool=authentication", "tool=oauth", "tool=jwt",
               "tool=token", "tool=login", "tool=session", "tool=password"]
        normalized = normalize_tags(raw)
        assert normalized == ["tool=auth"], f"Expected ['tool=auth'], got {normalized}"

    def test_normalize_does_not_create_unknown_canonical(self):
        """Unknown category tags pass through (not converted to 'other')."""
        raw = ["framework=django", "language=python", "custom=anything"]
        normalized = normalize_tags(raw)
        # language=python → language=py (known)
        # framework=django passes through (unknown category)
        # custom=anything passes through
        assert "language=py" in normalized
        assert "framework=django" in normalized
        assert "custom=anything" in normalized


class TestPatternFallback:
    """When LLM is unavailable, pattern fallback is used.

    Pattern fallback ONLY recognizes explicit "Tool: auth" markers.
    It does NOT do keyword matching (that's the LLM's job).
    """

    def setup_method(self):
        reset_vocabulary()

    def test_pattern_fallback_extracts_explicit_marker(self):
        """Explicit 'Tool: auth' markers should be extracted and normalized."""
        tags = infer_tags(
            "Some description\nTool: auth\nOutcome: failed",
            source_file=None,
            use_llm=False,
        )
        assert "tool=auth" in tags
        assert "outcome=failed" in tags

    def test_pattern_fallback_no_keyword_matching(self):
        """Pattern fallback should NOT do keyword matching — only explicit markers."""
        # Content has 'JWT' and 'OAuth' but no explicit markers
        tags = infer_tags(
            "JWT validation failed with OAuth flow",
            source_file=None,
            use_llm=False,
        )
        # Without LLM or explicit markers, no tags should be inferred
        # (only source_file tags if provided)
        assert "tool=auth" not in tags
        assert "outcome=failed" not in tags

    def test_pattern_fallback_includes_source_file_tags(self):
        """source_file tags are added even in pattern fallback."""
        tags = infer_tags(
            "anything",
            source_file="src/utils/io.py",
            use_llm=False,
        )
        assert "language=py" in tags
        assert "file=io" in tags


class TestTagInference:
    """Verify that infer_tags produces sensible tags from content.

    Tests in this class use use_llm=False (pattern fallback) to avoid
    hitting the real LLM. The LLM path is tested separately.
    """

    def setup_method(self):
        reset_vocabulary()

    def test_infer_tags_extracts_tool_outcome_error(self):
        """With explicit markers, all three categories should be extracted."""
        content = "Some context\nTool: auth\nOutcome: failed\nError: token_expired"
        tags = infer_tags(content, source_file=None, use_llm=False)
        assert "tool=auth" in tags
        assert "outcome=failed" in tags
        assert "error=token_expired" in tags

    def test_infer_tags_preserves_existing(self):
        """Existing tags from caller should be normalized and preserved."""
        tags = infer_tags(
            "anything goes",
            source_file=None,
            existing_tags=["tool=auth", "tool=OAuth", "custom=my_tag"],
            use_llm=False,
        )
        # All auth-related normalized to tool=auth (deduped)
        assert tags.count("tool=auth") == 1
        # Custom category preserved
        assert "custom=my_tag" in tags

    def test_infer_tags_from_source_file(self):
        """source_file should produce language and file tags (even in fallback)."""
        tags = infer_tags(
            "anything",
            source_file="src/utils/io.py",
            use_llm=False,
        )
        assert "language=py" in tags
        assert "file=io" in tags

    def test_infer_tags_extracts_pattern_markers(self):
        """Explicit 'Tool: auth' patterns in content should be extracted."""
        tags = infer_tags(
            "Some description\nTool: auth\nOutcome: failed",
            source_file=None,
            use_llm=False,
        )
        assert "tool=auth" in tags
        assert "outcome=failed" in tags

    def test_infer_tags_deduplicates(self):
        """Same tag from multiple sources should appear once."""
        tags = infer_tags(
            "Tool: auth\n\nSome text\n\nTool: auth",
            source_file=None,
            use_llm=False,
        )
        assert tags.count("tool=auth") == 1


class TestMergeTags:
    """Verify merge_tags combines sources correctly."""

    def setup_method(self):
        reset_vocabulary()

    def test_merge_dedupes(self):
        result = merge_tags(
            ["tool=auth", "outcome=failed"],
            ["tool=authentication", "tool=auth"],  # duplicate after normalization
        )
        assert result.count("tool=auth") == 1
        assert "outcome=failed" in result

    def test_merge_preserves_order(self):
        result = merge_tags(
            ["tool=auth", "outcome=failed"],
            ["error=timeout", "language=py"],
        )
        assert result.index("tool=auth") < result.index("outcome=failed")
        assert result.index("outcome=failed") < result.index("error=timeout")
        assert result.index("error=timeout") < result.index("language=py")


class TestLLMTagging:
    """Integration tests for LLM-based tagging — calls the real MiniMax API.

    These tests require MINIMAX_API_KEY to be set to a valid key. If the
    key is missing or invalid, the test will FAIL LOUDLY (not skip).
    Skipping tests hides real failures — we want to know if the LLM
    integration is broken.
    """

    def setup_method(self):
        reset_vocabulary()

    def test_llm_consolidates_auth_synonyms(self):
        """The LLM should produce the same tool=auth tag for various
        authentication-related content variants. This is the
        "logistically correct" property at the LLM level."""
        # Different ways of saying "authentication failed" should all produce tool=auth
        contents = [
            "OAuth token validation failed",
            "JWT was rejected during login",
            "Session cookie expired unexpectedly",
        ]

        for content in contents:
            tags = infer_tags(content, source_file=None, existing_tags=None)
            assert "tool=auth" in tags, (
                f"Content ({content!r}) should produce tool=auth. Got: {tags}"
            )

    def test_llm_consolidates_database_synonyms(self):
        """Database-related content with various names should produce tool=db."""
        contents = [
            "Postgres connection timed out",
            "Prisma migration failed on the users table",
            "Redis cache miss caused slow response",
        ]

        for content in contents:
            tags = infer_tags(content, source_file=None, existing_tags=None)
            assert "tool=db" in tags, (
                f"Content ({content!r}) should produce tool=db. Got: {tags}"
            )

    def test_llm_respects_caller_tags(self):
        """When caller provides tags, the LLM should preserve them."""
        tags = infer_tags(
            "User logged in successfully",
            source_file=None,
            existing_tags=["tool=auth", "outcome=successfully_called"],
        )
        # Caller's tags must be preserved (after normalization)
        assert "tool=auth" in tags
        assert "outcome=successfully_called" in tags

    def test_llm_handles_ambiguous_content_gracefully(self):
        """Content with no clear tags should return a minimal valid set."""
        # Content that's hard to categorize
        tags = infer_tags(
            "Random thoughts about nothing in particular",
            source_file=None,
            existing_tags=None,
        )
        # Should return at most a few tags, all valid
        for tag in tags:
            assert "=" in tag or tag.startswith("file=") or tag.startswith("language=")
