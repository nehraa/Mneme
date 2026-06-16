"""
[MOCK] Intent Detector — maps prompt → detected tags + intent label.
Phase 4 uses Ollama 100-200M local model for intent detection.
Real implementation → retrieval/intent_detector.py::IntentDetector
"""
from __future__ import annotations

from typing import Any


# Tag prefixes that map to specific outcome/tool tags
TAG_PREFIX_MAP = {
    "failed": "outcome=failed",
    "error": "error=",
    "success": "outcome=successfully_called",
    "work_done": "outcome=work_done",
    "stopped": "outcome=stopped",
    "no_tool": "outcome=no_tool_called",
    "auth": "tool=auth",
    "db": "tool=db",
    "http": "tool=http",
    "file": "tool=file_io",
}


INTENT_KEYWORDS = {
    "continue": "continue_previous_work",
    "retry": "retry_previous_attempt",
    "fix": "fix_previous_failure",
    "again": "retry_previous_attempt",
    "上次": "continue_previous_work",
    "继续": "continue_previous_work",
    "修复": "fix_previous_failure",
    "重试": "retry_previous_attempt",
}


class IntentDetector:
    """
    [MOCK] Detects intent and tags from a user prompt.

    Uses simple keyword matching for Phase 4 mock.
    Real implementation uses Ollama 100-200M local model:
        ollama chat model --prompt <prompt> → structured JSON {intent, tags}
    """

    def detect(self, prompt_context: str) -> dict[str, Any]:
        """
        [MOCK] Detect intent and tags from prompt text.
        Real implementation → Ollama 100-200M local inference.
        """
        prompt_lower = prompt_context.lower()
        detected_tags: list[str] = []
        intent = "general"

        # Detect intent from keywords
        for keyword, intent_label in INTENT_KEYWORDS.items():
            if keyword in prompt_lower:
                intent = intent_label
                break

        # Detect tags from prefixes
        for prefix, tag_template in TAG_PREFIX_MAP.items():
            if prefix in prompt_lower:
                if "=" in tag_template:
                    # Extract the word after the prefix as the tag value
                    idx = prompt_lower.find(prefix)
                    rest = prompt_lower[idx + len(prefix) :].strip()
                    word = rest.split()[0] if rest else prefix
                    detected_tags.append(f"{tag_template}{word}")
                else:
                    detected_tags.append(tag_template)

        # If auth-related keywords found, tag as auth tool
        if any(k in prompt_lower for k in ["auth", "token", "login", "oauth", "jwt"]):
            if "tool=auth" not in detected_tags:
                detected_tags.append("tool=auth")

        # If failure keywords found, tag as failed outcome
        if any(k in prompt_lower for k in ["failed", "error", "expired", "timeout", "reject"]):
            if "outcome=failed" not in detected_tags:
                detected_tags.append("outcome=failed")

        # Fallback intent detection
        if intent == "general":
            if "continue" in prompt_lower or "上次" in prompt_context:
                intent = "continue_previous_work"
            elif "retry" in prompt_lower or "重试" in prompt_context:
                intent = "retry_previous_attempt"

        return {
            "_mock": True,
            "intent": intent,
            "detected_tags": detected_tags,
            "_implementation_note": (
                "Real: retrieval/intent_detector.py::IntentDetector — "
                "Ollama 100-200M local model inference"
            ),
        }
