"""
Cursor usage reader.

Reads Cursor's local SQLite state (`state.vscdb`) to extract token usage
records from composer conversations.

What Cursor stores locally:
- `composerData:<composerId>` — conversation metadata, including `createdAt`
  and `modelConfig` (which model was selected).
- `bubbleId:<composerId>:<bubbleId>` — individual messages, with `tokenCount`
  (dict of `inputTokens` and `outputTokens`).

Observed schema drift on newer Cursor builds:
- `bubbleId.*.tokenCount` may exist but stay zero for every message.
- `composerData` can still carry `contextTokensUsed` / `contextTokenLimit`
  at the composer level.

Limitations vs OpenCode:
- **No per-message timestamps** — all bubbles in a composer share the
  composer's `createdAt`. Day-level accuracy is preserved; sub-session
  ordering is lost.
- **No cost data** (subscription model — Cursor doesn't expose per-request cost).
- **No cache token counts** (cache accounting isn't exposed).
- **No reasoning tokens.**

Output schema matches `usage.py` records (with `source="cursor"` and `None`
for fields Cursor doesn't expose).

Fallback behavior:
- Prefer real per-bubble token counts when any non-zero rows exist.
- If a composer has no non-zero bubble tokens but does have
  `contextTokensUsed`, emit one synthetic input-only record for that composer.
  This is a conservative estimate of usage intensity, not exact billable I/O.
"""

import json
import os
import re
import sqlite3
import time
from typing import Optional


STATE_VSCDB = os.path.join(
    os.environ.get("APPDATA", os.path.expanduser(r"~\AppData\Roaming")),
    "Cursor",
    "User",
    "globalStorage",
    "state.vscdb",
)

# bubbleId keys are `bubbleId:<composerUUID>:<bubbleUUID>`
_BUBBLE_KEY = re.compile(r"^bubbleId:([a-f0-9\-]+):([a-f0-9\-]+)$")


def _normalize_cursor_model(raw: str) -> str:
    """Map Cursor's model names to the canonical provider/model form.

    Cursor uses names like:
        claude-4.6-opus-max-thinking   -> anthropic/claude-opus-4.6-max-thinking
        claude-4.5-sonnet-thinking     -> anthropic/claude-sonnet-4.5-thinking
        gpt-5.1-codex-max-xhigh        -> openai/gpt-5.1-codex-max-xhigh
        gemini-3-pro                   -> google/gemini-3-pro
        moonshotai/kimi-k2-thinking    -> moonshotai/kimi-k2-thinking (already prefixed)
        composer-1                     -> cursor/composer-1
        composer-2-fast                -> cursor/composer-2-fast
    """
    if "/" in raw:
        return raw

    # Some Cursor builds store a composite label like
    # `composer-1.5,gpt-5.3-codex-xhigh`. Prefer the concrete model portion.
    if "," in raw:
        parts = [part.strip() for part in raw.split(",") if part.strip()]
        if parts:
            for candidate in reversed(parts):
                if not candidate.startswith("composer"):
                    return _normalize_cursor_model(candidate)
            return f"cursor/{parts[-1]}"

    bare = raw
    # Claude normalization: "claude-4.6-opus-..." -> "claude-opus-4.6-..."
    m = re.match(r"^claude-(\d+\.\d+)-(opus|sonnet|haiku)-?(.*)$", raw)
    if m:
        version, tier, rest = m.group(1), m.group(2), m.group(3)
        canonical = f"claude-{tier}-{version}"
        if rest:
            canonical += f"-{rest}"
        return f"anthropic/{canonical}"

    # Newer Cursor builds also emit `claude-opus-4-7` style IDs.
    m = re.match(r"^claude-(opus|sonnet|haiku)-(\d+)-(\d+)(?:-(.*))?$", raw)
    if m:
        tier, major, minor, rest = m.group(1), m.group(2), m.group(3), m.group(4)
        canonical = f"claude-{tier}-{major}.{minor}"
        if rest:
            canonical += f"-{rest}"
        return f"anthropic/{canonical}"

    # Short form "Claude 4 Opus" etc.
    m = re.match(r"^Claude\s+(\d+(?:\.\d+)?)\s+(Opus|Sonnet|Haiku)$", raw, re.IGNORECASE)
    if m:
        return f"anthropic/claude-{m.group(2).lower()}-{m.group(1)}"

    if bare.startswith("gpt") or bare.startswith("o1") or bare.startswith("o3") or bare.startswith("o4"):
        return f"openai/{bare}"
    if bare.startswith("gemini"):
        return f"google/{bare}"
    if bare.startswith("composer"):
        return f"cursor/{bare}"
    if bare == "default" or bare == "auto":
        return f"cursor/{bare}"

    return f"unknown/{bare}"


def _connect_readonly(db_path: str) -> sqlite3.Connection:
    """Read-only connection with one 500ms retry for transient locks."""
    try:
        return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
    except (sqlite3.OperationalError, PermissionError):
        time.sleep(0.5)
        return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)


def _load_composer_index(conn: sqlite3.Connection, strict: bool = False) -> dict[str, dict]:
    """Map composerId -> composer metadata used for Cursor usage records."""
    index: dict[str, dict] = {}
    for key, value in conn.execute(
        "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'composerData:%'"
    ):
        if not value:
            continue
        composer_id = key.split(":", 1)[1]
        try:
            data = json.loads(value)
        except json.JSONDecodeError:
            if strict:
                raise ValueError("Malformed Cursor composer JSON") from None
            continue
        model_config = data.get("modelConfig") or {}
        raw_model = model_config.get("modelName") or model_config.get("modelId") or "unknown"
        # Try to extract a title — Cursor stores it in varying places
        title = data.get("text") or data.get("richText") or ""
        if isinstance(title, str):
            title = title.strip()[:80]
        index[composer_id] = {
            "created_at": data.get("createdAt") or 0,
            "model": _normalize_cursor_model(raw_model),
            "title": title or f"cursor-{composer_id[:8]}",
            "context_tokens_used": data.get("contextTokensUsed"),
            "context_token_limit": data.get("contextTokenLimit"),
            "context_usage_percent": data.get("contextUsagePercent"),
        }
    return index


def read_cursor_messages(
    db_path: str = STATE_VSCDB,
    mid: Optional[str] = None,
    *, strict: bool = False,
) -> list[dict]:
    """Read Cursor usage messages using the best locally available signal.

    Returns records in the same shape as `usage.read_messages_from_db`:
        - tokens only in input/output; cache_read/write and reasoning are 0
          (not None, to keep summation code simple — they're just missing)
        - cost is 0.0 (no per-request cost data exposed)
        - source="cursor" discriminator

    On newer Cursor builds, all bubble `tokenCount` values can be zero even
    when Cursor was heavily used. In that case we fall back to one synthetic
    composer-level record carrying `contextTokensUsed` as conservative
    input-only usage. This pools activity instead of dropping Cursor entirely,
    but should be interpreted as an estimate rather than exact token accounting.
    """
    if not os.path.exists(db_path):
        return []

    # Import lazily to avoid circular dependency
    from usage import machine_id
    mid = mid or machine_id()

    conn = _connect_readonly(db_path)
    try:
        composers = _load_composer_index(conn, strict=strict)
        rows: list[dict] = []
        composer_bubble_counts: dict[str, int] = {composer_id: 0 for composer_id in composers}
        composers_with_real_tokens: set[str] = set()
        for key, value in conn.execute(
            "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'bubbleId:%'"
        ):
            m = _BUBBLE_KEY.match(key)
            if not m:
                continue
            composer_id, bubble_id = m.group(1), m.group(2)
            if not value:
                continue
            try:
                data = json.loads(value)
            except json.JSONDecodeError:
                if strict:
                    raise ValueError("Malformed Cursor bubble JSON") from None
                continue

            tc = data.get("tokenCount")
            if not isinstance(tc, dict):
                continue
            composer_bubble_counts[composer_id] = composer_bubble_counts.get(composer_id, 0) + 1
            input_tokens = tc.get("inputTokens", 0) or 0
            output_tokens = tc.get("outputTokens", 0) or 0
            if input_tokens == 0 and output_tokens == 0:
                continue
            composers_with_real_tokens.add(composer_id)

            composer = composers.get(composer_id) or {
                "created_at": 0,
                "model": "unknown/unknown",
                "title": f"cursor-{composer_id[:8]}",
            }

            rows.append({
                "source": "cursor",
                "machine_id": mid,
                "msg_id": f"cursor:{composer_id}:{bubble_id}",
                "model": composer["model"],
                "provider": composer["model"].split("/")[0] if "/" in composer["model"] else "unknown",
                "cost": 0.0,
                "input": input_tokens,
                "output": output_tokens,
                "reasoning": 0,
                "cache_read": 0,
                "cache_write": 0,
                "time": composer["created_at"],
                "session_id": composer_id,
                "session_title": composer["title"],
                "session_dir": "",
                "project_id": "cursor",
                "opencode_version": "cursor",
                "db_file": "state.vscdb",
                "message_count": 1,
                "token_estimated": False,
                "cursor_usage_mode": "bubble_token_count",
            })

        for composer_id, composer in composers.items():
            if composer_id in composers_with_real_tokens:
                continue
            context_tokens_used = composer.get("context_tokens_used")
            if context_tokens_used is None:
                continue
            context_tokens_used = int(context_tokens_used or 0)
            if context_tokens_used <= 0:
                continue

            rows.append({
                "source": "cursor",
                "machine_id": mid,
                "msg_id": f"cursor-composer:{composer_id}",
                "model": composer["model"],
                "provider": composer["model"].split("/")[0] if "/" in composer["model"] else "unknown",
                "cost": 0.0,
                "input": context_tokens_used,
                "output": 0,
                "reasoning": 0,
                "cache_read": 0,
                "cache_write": 0,
                "time": composer["created_at"],
                "session_id": composer_id,
                "session_title": composer["title"],
                "session_dir": "",
                "project_id": "cursor",
                "opencode_version": "cursor",
                "db_file": "state.vscdb",
                "message_count": max(composer_bubble_counts.get(composer_id, 0), 1),
                "token_estimated": True,
                "cursor_usage_mode": "composer_context_estimate",
                "context_token_limit": composer.get("context_token_limit"),
                "context_usage_percent": composer.get("context_usage_percent"),
            })
        return rows
    finally:
        conn.close()
