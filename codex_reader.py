"""
Codex Desktop usage reader.

Reads Codex Desktop's local JSONL session logs under ``~/.codex`` and emits
records in the same shape as ``usage.py``.

What Codex stores locally:
- ``sessions/**/*.jsonl`` and ``archived_sessions/*.jsonl`` contain event logs.
- ``event_msg`` rows with ``payload.type == "token_count"`` include
  ``last_token_usage`` for the most recent model call and ``total_token_usage``
  for the cumulative thread total.

We use ``last_token_usage`` so each token-count event becomes one model-call
record without double-counting the cumulative totals.

Fork/replay filtering: when Codex Desktop forks a thread or spawns a subagent
(``session_meta.forked_from_id``, ``history_mode: legacy``), it replays the
parent thread's full event history — including historical ``token_count``
events — into the new rollout file with fresh timestamps. Those replayed
events are already counted from the parent's own rollout file, so counting
them again inflates usage (measured 7x on 2026-07-16) and mis-dates it to the
fork time. Replays are written in bulk, so they appear as long runs of events
spaced milliseconds apart, while genuine model calls with large prompts take
seconds each. We drop any run of ``_REPLAY_MIN_RUN`` or more consecutive
events with inter-event gaps under ``_REPLAY_MAX_GAP_MS``; the subagent's own
work after the replayed prefix resumes normal spacing and is kept.
"""

from __future__ import annotations

import glob
import json
import os
import re
from datetime import datetime
from typing import Optional


CODEX_HOME = os.environ.get("CODEX_HOME") or os.path.join(
    os.environ.get("USERPROFILE", os.path.expanduser("~")),
    ".codex",
)
SESSIONS_DIR = os.path.join(CODEX_HOME, "sessions")
ARCHIVED_SESSIONS_DIR = os.path.join(CODEX_HOME, "archived_sessions")
SESSION_INDEX = os.path.join(CODEX_HOME, "session_index.jsonl")

_SESSION_ID_IN_FILENAME = re.compile(r"-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$")


def discover_codex_session_files(codex_home: str = CODEX_HOME) -> list[str]:
    """Find Codex Desktop JSONL session logs."""
    sessions_dir = os.path.join(codex_home, "sessions")
    archived_dir = os.path.join(codex_home, "archived_sessions")
    paths: list[str] = []
    if os.path.isdir(sessions_dir):
        paths.extend(glob.glob(os.path.join(sessions_dir, "**", "*.jsonl"), recursive=True))
    if os.path.isdir(archived_dir):
        paths.extend(glob.glob(os.path.join(archived_dir, "*.jsonl")))
    return sorted(set(paths))


def _parse_timestamp_ms(value) -> int:
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        # Codex event payloads sometimes use epoch seconds; JSONL row
        # timestamps use ISO strings. Preserve ms if already in ms.
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


def _session_id_from_path(path: str) -> str:
    match = _SESSION_ID_IN_FILENAME.search(os.path.basename(path))
    return match.group(1) if match else "unknown"


def _load_session_titles(codex_home: str = CODEX_HOME) -> dict[str, str]:
    """Map Codex thread/session IDs to human-readable thread names."""
    index_path = os.path.join(codex_home, "session_index.jsonl")
    titles: dict[str, str] = {}
    if not os.path.exists(index_path):
        return titles
    try:
        with open(index_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if not isinstance(row, dict):
                    continue
                session_id = row.get("id")
                title = row.get("thread_name")
                if session_id and title:
                    titles[session_id] = str(title).strip()[:80]
    except OSError:
        return titles
    return titles


def _normalize_codex_model(raw: str, provider: str = "openai") -> str:
    """Map Codex Desktop model names to canonical provider/model form."""
    raw = (raw or "unknown").strip()
    provider = (provider or "unknown").strip() or "unknown"
    if "/" in raw:
        return raw
    if raw.startswith(("gpt", "o1", "o3", "o4", "codex")):
        return f"openai/{raw}"
    if provider not in {"unknown", "missing"}:
        return f"{provider}/{raw}"
    return f"unknown/{raw}"


# Replay runs are thousands of events apart by <10ms; real model calls with
# 100K+ token prompts take seconds. 200ms/10-run is conservative on both sides.
_REPLAY_MAX_GAP_MS = 200
_REPLAY_MIN_RUN = 10


def _replayed_indices(times_ms: list[int], max_gap_ms: int = _REPLAY_MAX_GAP_MS, min_run: int = _REPLAY_MIN_RUN) -> set[int]:
    """Return indices of token-count events that belong to a replay burst.

    A replay burst is a run of `min_run`-or-more consecutive events where every
    adjacent pair is less than `max_gap_ms` apart. Events without a usable
    timestamp (0) never join a run.
    """
    replayed: set[int] = set()
    run_start = 0
    for i in range(1, len(times_ms) + 1):
        in_run = (
            i < len(times_ms)
            and times_ms[i] > 0
            and times_ms[i - 1] > 0
            and abs(times_ms[i] - times_ms[i - 1]) < max_gap_ms
        )
        if not in_run:
            if i - run_start >= min_run:
                replayed.update(range(run_start, i))
            run_start = i
    return replayed


def read_codex_messages(
    codex_home: str = CODEX_HOME,
    mid: Optional[str] = None,
    *, paths: Optional[list[str]] = None, strict: bool = False,
    diagnostics: Optional[list[str]] = None,
    titles: Optional[dict[str, str]] = None,
) -> list[dict]:
    """Read Codex Desktop token-count events from local session JSONL logs."""
    if paths is None and not os.path.isdir(codex_home):
        return []

    # Import lazily to avoid circular dependency when usage.py imports us.
    from usage import machine_id

    mid = mid or machine_id()
    titles = _load_session_titles(codex_home) if titles is None else titles
    rows: list[dict] = []

    for path in paths if paths is not None else discover_codex_session_files(codex_home):
        session_id = _session_id_from_path(path)
        session_title = titles.get(session_id) or f"codex-{session_id[:8]}"
        session_dir = ""
        provider = "openai"
        cli_version = "codex"
        current_model = "openai/unknown"
        current_turn_id = ""
        pricing_metadata = {}
        token_event_index = 0
        file_rows: list[dict] = []

        try:
            f = open(path, "r", encoding="utf-8")
        except OSError:
            if strict:
                raise
            continue

        with f:
            for line_no, line in enumerate(f, start=1):
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
                payload = obj.get("payload") or {}
                row_type = obj.get("type")

                if row_type == "session_meta":
                    session_id = payload.get("id") or session_id
                    session_title = titles.get(session_id) or session_title or f"codex-{session_id[:8]}"
                    session_dir = payload.get("cwd") or session_dir
                    provider = payload.get("model_provider") or provider
                    cli_version = payload.get("cli_version") or cli_version
                    continue

                if row_type == "turn_context":
                    current_turn_id = payload.get("turn_id") or current_turn_id
                    session_dir = payload.get("cwd") or session_dir
                    current_model = _normalize_codex_model(payload.get("model") or "unknown", provider)
                    pricing_metadata = {key: payload[key] for key in ("service_tier", "processing_region") if payload.get(key) is not None}
                    continue

                if row_type != "event_msg" or payload.get("type") != "token_count":
                    continue

                info = payload.get("info")
                if not isinstance(info, dict):
                    continue
                usage = info.get("last_token_usage")
                if not isinstance(usage, dict):
                    continue
                if strict:
                    from usage import validate_token_value
                    for category in ("input_tokens", "output_tokens", "reasoning_output_tokens", "cached_input_tokens"):
                        validate_token_value(usage.get(category), category)

                raw_input_tokens = _safe_int(usage.get("input_tokens"))
                raw_output_tokens = _safe_int(usage.get("output_tokens"))
                reasoning_tokens = _safe_int(usage.get("reasoning_output_tokens"))
                cached_input_tokens = _safe_int(usage.get("cached_input_tokens"))
                input_tokens = max(raw_input_tokens - cached_input_tokens, 0)
                output_tokens = max(raw_output_tokens - reasoning_tokens, 0)
                if raw_input_tokens == 0 and raw_output_tokens == 0 and reasoning_tokens == 0 and cached_input_tokens == 0:
                    continue

                token_event_index += 1
                event_time = obj.get("timestamp")
                event_ms = _parse_timestamp_ms(event_time)
                stable_event_id = f"line-{line_no}:{event_time or token_event_index}"
                model = current_model
                row_provider = model.split("/", 1)[0] if "/" in model else provider
                rate_limits = payload.get("rate_limits") or {}

                file_rows.append({
                    "source": "codex",
                    "machine_id": mid,
                    "msg_id": f"codex:{session_id}:{stable_event_id}",
                    "model": model,
                    "provider": row_provider,
                    "billing_source": "codex",
                    "cash_cost": 0.0,
                    "implied_cost": 0.0,
                    "cost": 0.0,
                    "input": input_tokens,
                    "output": output_tokens,
                    "reasoning": reasoning_tokens,
                    "cache_read": cached_input_tokens,
                    "cache_write": 0,
                    "time": event_ms,
                    "session_id": f"codex:{session_id}",
                    "session_title": session_title,
                    "session_dir": session_dir,
                    "project_id": "codex",
                    "opencode_version": f"codex {cli_version}" if cli_version != "codex" else "codex",
                    "db_file": os.path.basename(path),
                    "message_count": 1,
                    "token_estimated": False,
                    "codex_turn_id": current_turn_id,
                    "codex_usage_mode": "last_token_usage",
                    "codex_plan_type": rate_limits.get("plan_type"),
                    "recorded_cost": None,
                    "original_usage": usage,
                    "pricing_metadata": {**pricing_metadata, **{key: info[key] for key in ("service_tier", "processing_region") if info.get(key) is not None}},
                    "source_line": line_no,
                    "codex_model_context_window": info.get("model_context_window"),
                })

        replayed = _replayed_indices([r["time"] for r in file_rows])
        rows.extend(r for i, r in enumerate(file_rows) if i not in replayed)

    return rows
