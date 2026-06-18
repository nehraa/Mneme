"""
Intent Detector — maps prompt → detected tags + intent label.

Uses simple keyword-based heuristics to extract tags and intent from a
user prompt. For higher-quality detection, swap in BitNetClient-based
real inference (see _detect_real below) once the BitNet server is running.
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
    Detects intent and tags from a user prompt.

    Uses simple keyword matching. The real implementation uses BitNet
    (Microsoft's 1-bit inference framework) with Falcon3-1B-Instruct
    from HuggingFace:
        # BitNet runs locally via llama-server (llama.cpp fork)
        # Model: tiiuae/Falcon3-1B-Instruct-1.58bit (I2_S, ~1.3GB)
        # Key: prompts MUST use Falcon3 chat template (<|user|>/<|assistant|>)
        #   Without it the pre-tokenizer produces gibberish.
        #   With proper template, even I2_S produces coherent output.
        result = client.detect_intent(prompt_context)
        # Expected real response shape:
        # {"intent": "<intent_label>", "detected_tags": ["outcome=failed", "tool=auth", ...]}
    """

    def __init__(self) -> None:
        """Initialize IntentDetector using keyword-based detection."""

    def detect(self, prompt_context: str) -> dict[str, Any]:
        """
        Detect intent and tags from a user prompt.

        Returns a dict with:
          - intent: intent label (e.g. "continue_previous_work", "retry_previous_attempt")
          - detected_tags: list of flat tags (e.g. ["outcome=failed", "tool=auth"])
          - source: "keyword" (always; swap with BitNet later)
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
            "intent": intent,
            "detected_tags": detected_tags,
            "source": "keyword",
        }
