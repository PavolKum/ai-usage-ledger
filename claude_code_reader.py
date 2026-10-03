"""
Claude Code usage reader.

Reads Claude Code's local JSONL project transcripts under ``~/.claude`` and
emits records in the same normalized shape as ``usage.py``.

Important Claude Code quirk: a single API response may be logged as multiple
assistant rows with the same ``message.id`` / ``requestId`` and identical
``message.usage``. We deduplicate on that pair so transcript fragments are not
counted as separate model calls.
"""

from __future__ import annotations

import glob
import json
import os
from datetime import datetime
from typing import Optional


CLAUDE_HOME = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(
    os.environ.get("USERPROFILE", os.path.expanduser("~")),
    ".claude",
)


def discover_claude_code_session_files(claude_home: str = CLAUDE_HOME) -> list[str]:
    """Find Claude Code project JSONL transcript files."""
    projects_dir = os.path.join(claude_home, "projects")
    if not os.path.isdir(projects_dir):
        return []
    return sorted(set(glob.glob(os.path.join(projects_dir, "**", "*.jsonl"), recursive=True)))


def _parse_timestamp_ms(value) -> int:
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(value if value > 10_000_000_000 else value * 1000)
    if not isinstance(value, str) or not value.strip():
        return 0
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        return int(datetime.fromisoformat(text).timestamp() * 1000)
    except ValueError:
        return 0


def _safe_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _extract_text(content) -> str:
    """Extract a compact title-like string from Claude message content."""
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""

    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict):
            text = item.get("text") or item.get("content")
            if isinstance(text, str):
                parts.append(text)
    return " ".join(part.strip() for part in parts if part and part.strip()).strip()


def _normalize_claude_model(raw: str) -> str:
    """Map Claude Code model names to canonical provider/model form."""
    raw = (raw or "unknown").strip()
    if not raw or raw == "unknown":
        return "anthropic/unknown"
    if raw.startswith("<") and raw.endswith(">"):
        return raw

    # Import lazily to avoid a top-level circular dependency when usage.py imports
    # this module inside load_all_messages().
    from usage import normalize_model

    return normalize_model(raw)


def read_claude_code_messages(
    claude_home: str = CLAUDE_HOME,
    mid: Optional[str] = None,
    *, paths: Optional[list[str]] = None, strict: bool = False,
    diagnostics: Optional[list[str]] = None,
) -> list[dict]:
    """Read Claude Code assistant usage events from local project JSONL logs."""
    if paths is None and not os.path.isdir(claude_home):
        return []

    from usage import machine_id

    mid = mid or machine_id()
    rows: list[dict] = []
    seen_calls: set[tuple[str, str]] = set()
    session_titles: dict[str, str] = {}

    for path in paths if paths is not None else discover_claude_code_session_files(claude_home):
        try:
            handle = open(path, "r", encoding="utf-8")
        except OSError:
            if strict:
                raise
            continue

        with handle:
            for line_no, line in enumerate(handle, start=1):
                if strict and not line.endswith("\n"):
                    if diagnostics is not None:
                        diagnostics.append("Incomplete trailing line; awaiting completion")
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    if strict:
                        raise ValueError(f"Malformed JSON at line {line_no}") from None
                    continue

                if strict and not isinstance(obj, dict):
                    raise ValueError(f"Expected JSON object at line {line_no}")

                session_id = obj.get("sessionId") or os.path.splitext(os.path.basename(path))[0]
                message = obj.get("message") or {}

                if obj.get("type") == "user" and session_id not in session_titles:
                    user_text = _extract_text(message.get("content"))
                    if user_text:
                        session_titles[session_id] = user_text[:80]
                    continue

                if obj.get("type") != "assistant":
                    continue

                usage = message.get("usage")
                if not isinstance(usage, dict):
                    continue
                if strict:
                    from usage import validate_token_value
                    for category in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
                        validate_token_value(usage.get(category), category)

                raw_model = (message.get("model") or "unknown").strip()
                if raw_model == "<synthetic>":
                    continue

                message_id = message.get("id") or ""
                request_id = obj.get("requestId") or ""
                if message_id and request_id:
                    call_key = (message_id, request_id)
                    msg_id = f"claude-code:{message_id}:{request_id}"
                else:
                    fallback_id = obj.get("uuid") or f"{os.path.basename(path)}:{line_no}"
                    call_key = (fallback_id, "")
                    msg_id = f"claude-code:{fallback_id}"

                if call_key in seen_calls:
                    continue

                input_tokens = _safe_int(usage.get("input_tokens"))
                output_tokens = _safe_int(usage.get("output_tokens"))
                cache_read = _safe_int(usage.get("cache_read_input_tokens"))
                cache_write = _safe_int(usage.get("cache_creation_input_tokens"))
                if input_tokens == 0 and output_tokens == 0 and cache_read == 0 and cache_write == 0:
                    continue
                seen_calls.add(call_key)

                cache_creation = usage.get("cache_creation") or {}
                model = _normalize_claude_model(raw_model)
                provider = model.split("/", 1)[0] if "/" in model else "anthropic"
                event_ms = _parse_timestamp_ms(obj.get("timestamp"))
                version = obj.get("version") or "claude-code"

                rows.append({
                    "source": "claude-code",
                    "machine_id": mid,
                    "msg_id": msg_id,
                    "model": model,
                    "provider": provider,
                    "billing_source": "subscription",
                    "cash_cost": float(obj.get("costUSD") or 0.0),
                    "implied_cost": 0.0,
                    "cost": 0.0,
                    "input": input_tokens,
                    "output": output_tokens,
                    "reasoning": 0,
                    "cache_read": cache_read,
                    "cache_write": cache_write,
                    "time": event_ms,
                    "session_id": f"claude-code:{session_id}",
                    "session_title": session_titles.get(session_id) or f"claude-code-{session_id[:8]}",
                    "session_dir": obj.get("cwd") or "",
                    "project_id": obj.get("cwd") or "claude-code",
                    "opencode_version": f"claude-code {version}",
                    "db_file": os.path.basename(path),
                    "message_count": 1,
                    "token_estimated": False,
                    "agent_version": version,
                    "is_sidechain": bool(obj.get("isSidechain")),
                    "claude_request_id": request_id,
                    "claude_usage_mode": "message_usage_deduped",
                    "cache_write_5m": _safe_int(cache_creation.get("ephemeral_5m_input_tokens")),
                    "cache_write_1h": _safe_int(cache_creation.get("ephemeral_1h_input_tokens")),
                    "recorded_cost": float(obj["costUSD"]) if obj.get("costUSD") is not None else None,
                    "original_usage": usage,
                    "source_line": line_no,
                })

    return rows
