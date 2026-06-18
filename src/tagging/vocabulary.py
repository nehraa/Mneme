"""
Canonical tag vocabulary — single source of truth for tag normalization.

Tags are organized by category. Each category has:
  - canonical values: the official, normalized tags
  - synonyms: alternative terms that map to a canonical value

The vocabulary is loaded from `tag_vocabulary.yaml` if present, otherwise
from the built-in defaults. Users can extend the vocabulary by editing
the YAML file — no code changes needed.

This is what makes tagging "logistically correct": 100 conversations about
authentication all get `tool=auth`, not 100 different `tool=*` tags.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class TagCategory:
    """A category of tags (e.g., 'tool', 'outcome', 'error')."""

    name: str
    canonical_values: tuple[str, ...]
    synonyms: dict[str, str] = field(default_factory=dict)

    def resolve(self, value: str) -> str:
        """
        Resolve a raw value to its canonical form.

        - "auth" → "auth" (already canonical)
        - "authentication" → "auth" (synonym → canonical)
        - "unknown_tool" → "other" (falls back to default if defined)
        - Returns the input unchanged if no resolution found.
        """
        value_lower = value.lower().strip()
        if value_lower in self.canonical_values:
            return value_lower
        if value_lower in self.synonyms:
            return self.synonyms[value_lower]
        # Fallback: if "other" is in canonical values, use it as the catch-all
        if "other" in self.canonical_values:
            return "other"
        return value_lower


# Default vocabulary (used if no YAML file exists).
# This is a STARTING set — extend via tag_vocabulary.yaml.
DEFAULT_VOCABULARY: dict[str, dict[str, Any]] = {
    "tool": {
        "canonical": [
            "auth", "db", "http", "file_io", "memory",
            "config", "log", "search", "queue", "cache",
            "test", "build", "deploy", "other",
        ],
        "synonyms": {
            # Auth
            "authentication": "auth",
            "authorization": "auth",
            "oauth": "auth",
            "oauth2": "auth",
            "openid": "auth",
            "saml": "auth",
            "jwt": "auth",
            "token": "auth",
            "session": "auth",
            "login": "auth",
            "log": "auth",  # "User logged in" → auth (not the log tool)
            "logged": "auth",
            "logging": "auth",  # disambiguate: "logging" often means auth-logging-in
            "password": "auth",
            "credential": "auth",
            # Database
            "database": "db",
            "postgres": "db",
            "postgresql": "db",
            "mysql": "db",
            "mariadb": "db",
            "sqlite": "db",
            "mongo": "db",
            "mongodb": "db",
            "redis": "db",
            "memcached": "db",
            "prisma": "db",
            "drizzle": "db",
            "sqlalchemy": "db",
            "sql": "db",
            "nosql": "db",
            # HTTP/API
            "http": "http",
            "api": "http",
            "rest": "http",
            "graphql": "http",
            "grpc": "http",
            "websocket": "http",
            "fetch": "http",
            "request": "http",
            "endpoint": "http",
            "route": "http",
            # File I/O
            "file": "file_io",
            "fs": "file_io",
            "disk": "file_io",
            "storage": "file_io",
            "s3": "file_io",
            "blob": "file_io",
            # Memory store (Mneme-specific)
            "memory_store": "memory",
            "memorystore": "memory",
            "memory-store": "memory",
            # Config
            "config": "config",
            "configuration": "config",
            "settings": "config",
            "env": "config",
            "environment": "config",
            # Logging (general — "log message", "logger.info", etc.)
            "logger": "log",
            # Search
            "search": "search",
            "elasticsearch": "search",
            "algolia": "search",
            # Queue
            "queue": "queue",
            "kafka": "queue",
            "rabbitmq": "queue",
            "sqs": "queue",
            "pubsub": "queue",
            # Cache
            "cache": "cache",
            "memcache": "cache",
            "redis_cache": "cache",
            # Tests
            "test": "test",
            "pytest": "test",
            "unittest": "test",
            "jest": "test",
        },
    },
    "outcome": {
        "canonical": [
            "work_done", "failed", "successfully_called",
            "no_tool_called", "stopped", "other",
        ],
        "synonyms": {
            # work_done
            "work_done": "work_done",
            "done": "work_done",
            "completed": "work_done",
            "complete": "work_done",
            "finished": "work_done",
            "success_work": "work_done",
            # failed
            "failed": "failed",
            "fail": "failed",
            "failure": "failed",
            "broken": "failed",
            "didnt_work": "failed",
            "didnt": "failed",
            "did_not_work": "failed",
            "not_work": "failed",
            "crash": "failed",
            "crashed": "failed",
            "broke": "failed",
            "bug": "failed",
            # successfully_called
            "success": "successfully_called",
            "succeeded": "successfully_called",
            "successful": "successfully_called",
            "worked": "successfully_called",
            "works": "successfully_called",
            "ok": "successfully_called",
            "pass": "successfully_called",
            "passed": "successfully_called",
            # no_tool_called
            "no_tool": "no_tool_called",
            "skipped": "no_tool_called",
            "noop": "no_tool_called",
            # stopped
            "stopped": "stopped",
            "cancelled": "stopped",
            "canceled": "stopped",
            "aborted": "stopped",
            "killed": "stopped",
            "halted": "stopped",
        },
    },
    "error": {
        "canonical": [
            "timeout", "token_expired", "auth_rejected", "not_found",
            "server_error", "service_unavailable", "rate_limited",
            "validation", "connection", "permission_denied", "other",
        ],
        "synonyms": {
            # timeout
            "timeout": "timeout",
            "timed_out": "timeout",
            "timedout": "timeout",
            "took_too_long": "timeout",
            "expired_wait": "timeout",
            # token_expired
            "expired": "token_expired",
            "token_expired": "token_expired",
            "session_expired": "token_expired",
            "jwt_expired": "token_expired",
            "auth_expired": "token_expired",
            # auth_rejected
            "rejected": "auth_rejected",
            "denied": "auth_rejected",
            "unauthorized": "auth_rejected",
            "forbidden": "auth_rejected",
            "invalid_credentials": "auth_rejected",
            "wrong_password": "auth_rejected",
            "invalid_token": "auth_rejected",
            # not_found
            "not_found": "not_found",
            "missing": "not_found",
            "absent": "not_found",
            "404": "not_found",
            # server_error
            "500": "server_error",
            "internal_error": "server_error",
            "exception": "server_error",
            "crash": "server_error",
            # service_unavailable
            "503": "service_unavailable",
            "unavailable": "service_unavailable",
            "down": "service_unavailable",
            "offline": "service_unavailable",
            # rate_limited
            "429": "rate_limited",
            "throttled": "rate_limited",
            "rate_limit": "rate_limited",
            "too_many_requests": "rate_limited",
            # validation
            "invalid": "validation",
            "validation_failed": "validation",
            "bad_request": "validation",
            "400": "validation",
            "schema_error": "validation",
            # connection
            "connection_refused": "connection",
            "econnrefused": "connection",
            "network_error": "connection",
            "dns_error": "connection",
            "unreachable": "connection",
            # permission_denied
            "403": "permission_denied",
            "eacces": "permission_denied",
        },
    },
    "language": {
        "canonical": [
            "py", "js", "ts", "tsx", "jsx", "go", "rs", "rb", "java",
            "kt", "swift", "c", "cpp", "h", "hpp", "cs", "php", "sh",
            "sql", "yaml", "yml", "json", "toml", "md", "other",
        ],
        "synonyms": {
            "python": "py",
            "javascript": "js",
            "typescript": "ts",
            "golang": "go",
            "rust": "rs",
            "ruby": "rb",
            "kotlin": "kt",
            "csharp": "cs",
            "c++": "cpp",
            "cpp": "cpp",
            "shell": "sh",
            "bash": "sh",
            "zsh": "sh",
            "markdown": "md",
        },
    },
}


def _load_vocabulary() -> dict[str, TagCategory]:
    """Load vocabulary from YAML or fall back to defaults."""
    vocab_data: dict[str, dict[str, Any]] = {}

    # Try to load from tag_vocabulary.yaml
    vocab_path = Path(__file__).parent.parent.parent / "tag_vocabulary.yaml"
    if vocab_path.exists():
        try:
            with open(vocab_path) as f:
                yaml_data = yaml.safe_load(f) or {}
            if isinstance(yaml_data, dict):
                vocab_data = yaml_data
        except Exception:
            vocab_data = {}

    # Merge with defaults (YAML overrides defaults)
    merged: dict[str, dict[str, Any]] = {}
    for category, default_data in DEFAULT_VOCABULARY.items():
        yaml_data = vocab_data.get(category, {})
        merged[category] = {
            "canonical": yaml_data.get("canonical", default_data["canonical"]),
            "synonyms": {**default_data["synonyms"], **yaml_data.get("synonyms", {})},
        }
    # Add any new categories from YAML
    for category, yaml_data in vocab_data.items():
        if category not in merged:
            merged[category] = yaml_data

    # Build TagCategory objects
    return {
        name: TagCategory(
            name=name,
            canonical_values=tuple(data["canonical"]),
            synonyms=data["synonyms"],
        )
        for name, data in merged.items()
    }


# Module-level cache (lazy loaded)
_VOCABULARY: dict[str, TagCategory] | None = None


def get_vocabulary() -> dict[str, TagCategory]:
    """Get the loaded vocabulary (lazy singleton)."""
    global _VOCABULARY
    if _VOCABULARY is None:
        _VOCABULARY = _load_vocabulary()
    return _VOCABULARY


def reset_vocabulary() -> None:
    """Reset cached vocabulary (for testing)."""
    global _VOCABULARY
    _VOCABULARY = None
