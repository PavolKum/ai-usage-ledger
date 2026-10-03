"""
OpenCode usage aggregation library.

Reads OpenCode's SQLite databases plus local Claude Code, Cursor, and Codex Desktop state
(read-only) and provides structured token/cost data across sources, sessions,
models, and time windows.

Cross-machine pooling: if `exports/*.jsonl` files exist in this project folder
(populated by `export.py` on other machines via OneDrive sync), their records
are merged into the local view, deduplicated by `(machine_id, source, msg_id)`.
"""

import glob
import json
import os
import re
import socket
import sqlite3
import time
from datetime import datetime, timedelta
from typing import Optional

import verified_pricing


# ---------------------------------------------------------------------------
# Paths & identity
# ---------------------------------------------------------------------------

DATA_DIR = os.path.join(os.environ.get("USERPROFILE", os.path.expanduser("~")), ".local", "share", "opencode")
REPO_DIR = os.path.dirname(os.path.abspath(__file__))
EXPORTS_DIR = os.path.join(REPO_DIR, "exports")


def machine_id() -> str:
    """Stable identifier for this machine. Env override, else hostname."""
    return os.environ.get("OPENCODE_USAGE_MACHINE_ID") or socket.gethostname()


def discover_databases(data_dir: str = DATA_DIR) -> list[str]:
    """Find all opencode *.db files in the data directory."""
    pattern = os.path.join(data_dir, "*.db")
    return sorted(glob.glob(pattern))


def discover_exports(exports_dir: str = EXPORTS_DIR) -> list[str]:
    """Find *.jsonl export files from other machines."""
    if not os.path.isdir(exports_dir):
        return []
    pattern = os.path.join(exports_dir, "*.jsonl")
    return sorted(glob.glob(pattern))


# ---------------------------------------------------------------------------
# Model ID normalization
# ---------------------------------------------------------------------------

# OpenCode stores model IDs inconsistently:
#   "anthropic/claude-opus-4.7", "claude-opus-4.7", "claude-opus-4-7"
# We normalize to "provider/canonical-name" form.

_MODEL_ALIASES: dict[str, str] = {}


def _build_alias(raw: str) -> str:
    """Normalize a raw model ID to canonical form."""
    # Strip leading provider prefix for matching
    bare = raw.split("/", 1)[-1] if "/" in raw else raw

    # Normalize separators: "claude-opus-4-7" -> "claude-opus-4.7"
    # Pattern: word-digit where the digit starts a version number
    bare = re.sub(r"-(\d+)-(\d+)", r"-\1.\2", bare)

    # Infer provider from name patterns
    provider = raw.split("/", 1)[0] if "/" in raw else None
    if not provider:
        if bare.startswith("claude"):
            provider = "anthropic"
        elif bare.startswith("gpt") or bare.startswith("o1") or bare.startswith("o3"):
            provider = "openai"
        elif bare.startswith("gemini"):
            provider = "google"
        elif bare.startswith("kimi"):
            provider = "moonshotai"
        else:
            provider = "unknown"

    return f"{provider}/{bare}"


def normalize_model(raw: str) -> str:
    """Return canonical model ID for a raw model ID string."""
    if raw not in _MODEL_ALIASES:
        _MODEL_ALIASES[raw] = _build_alias(raw)
    return _MODEL_ALIASES[raw]


def infer_billing_source(source: str, provider: Optional[str], raw_cost: float = 0.0) -> str:
    """Classify the billing rail behind a record.

    - Claude Code, Cursor, and Codex Desktop local state are flat-rate/prepaid rails.
    - OpenCode rows with provider `github-copilot` are GHCP-backed.
    - OpenCode rows with any other concrete provider, or any non-zero raw cost,
      are treated as metered API usage.
    - Anything else remains unknown.
    """
    if source == "claude-code":
        return "subscription"
    if source == "cursor":
        return "cursor"
    if source == "codex":
        return "codex"
    if provider == "github-copilot":
        return "ghcp"
    if raw_cost > 0:
        return "api"
    if provider and provider not in {"", "unknown", "missing", None}:
        return "api"
    return "unknown"


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

def _rate(
    input_per_million: float,
    cached_input_per_million: Optional[float],
    output_per_million: float,
    cache_write_per_million: Optional[float] = None,
) -> dict:
    """Convert published per-1M-token prices to per-token rates."""
    input_rate = input_per_million / 1_000_000
    cached_rate = (cached_input_per_million if cached_input_per_million is not None else input_per_million) / 1_000_000
    cache_write_rate = (cache_write_per_million if cache_write_per_million is not None else input_per_million) / 1_000_000
    return {
        "input": input_rate,
        "output": output_per_million / 1_000_000,
        "cache_read": cached_rate,
        "cache_write": cache_write_rate,
    }


# Published standard API list prices, per
# https://platform.openai.com/docs/pricing and https://developers.openai.com/codex/pricing
# (OpenAI, checked 2026-06-06) and https://cursor.com/blog/composer-2-5 (Cursor
# Composer, checked 2026-06-20). These published rates override derived OpenCode
# rates for matching models because the generic derivation is approximate for
# each vendor's output/cache ratios. Long-context tiers (below) are OpenAI-only;
# models absent from the long table simply resolve to their single short rate.
_LONG_CONTEXT_THRESHOLD_TOKENS = 270_000

_PUBLISHED_MODEL_RATES_SHORT: dict[str, dict] = {
    # GPT-5.6 lineup (sol/terra/luna tiers), per developers.openai.com/api/docs/pricing
    # (checked 2026-07-16), standard-processing tier. 5.6 has no long-context table;
    # it introduces a published "Cache writes" price (1.25x input), passed as the
    # fourth arg. Batch/Flex/Priority tiers are not modeled here.
    "openai/gpt-5.6-sol": _rate(5.00, 0.50, 30.00, 6.25),
    "openai/gpt-5.6-terra": _rate(2.50, 0.25, 15.00, 3.125),
    "openai/gpt-5.6-luna": _rate(1.00, 0.10, 6.00, 1.25),
    "openai/gpt-5.5": _rate(5.00, 0.50, 30.00),
    "openai/gpt-5.5-pro": _rate(30.00, None, 180.00),
    "openai/gpt-5.4": _rate(2.50, 0.25, 15.00),
    "openai/gpt-5.4-mini": _rate(0.75, 0.075, 4.50),
    "openai/gpt-5.4-nano": _rate(0.20, 0.02, 1.25),
    "openai/gpt-5.4-pro": _rate(30.00, None, 180.00),
    "openai/gpt-5.3-codex": _rate(1.75, 0.175, 14.00),
    # Cursor's own coding models -- the bare "cursor/composer" key prefix-matches
    # every variant (composer-1, composer-1.5, composer-2, composer-2.5, their
    # *-fast forms, and future composer-N). We bill all of them at Composer 2.5's
    # "Fast" tier (Cursor's in-IDE default), per cursor.com/blog/composer-2-5
    # (checked 2026-06-20): $3.00/M input, $15.00/M output. Cache pricing is not
    # published, so cache_read uses the table's 10%-of-input convention ($0.30/M).
    "cursor/composer": _rate(3.00, 0.30, 15.00),
}

_PUBLISHED_MODEL_RATES_LONG: dict[str, dict] = {
    "openai/gpt-5.5": _rate(10.00, 1.00, 45.00),
    "openai/gpt-5.5-pro": _rate(60.00, None, 270.00),
    "openai/gpt-5.4": _rate(5.00, 0.50, 22.50),
    "openai/gpt-5.4-pro": _rate(60.00, None, 270.00),
}

_PUBLISHED_MODEL_RATES: dict[str, dict] = _PUBLISHED_MODEL_RATES_SHORT

# Static fallback rates for Claude Code / Anthropic subscription usage, per
# Anthropic API list prices per 1M tokens. These are used only when OpenCode did
# not provide a metered row for the exact model. Cash cost stays zero; this is an
# API-equivalent intensity estimate.
_STATIC_FALLBACK_MODEL_RATES: dict[str, dict] = {
    # Opus 4 and 4.1 predate the 4.5 price cut and keep the old $15/MTok input,
    # $75/MTok output rates. The bare family key covers both.
    "anthropic/claude-opus-4": _rate(15.00, 1.50, 75.00, 18.75),
    # Opus 4.5 cut Opus pricing to $5/MTok input, $0.50/MTok cache read,
    # $25/MTok output, $6.25/MTok 5m cache write, and 4.6-4.8 held it. Listed
    # per version because the family key above still has to price 4/4.1 old.
    "anthropic/claude-opus-4.5": _rate(5.00, 0.50, 25.00, 6.25),
    "anthropic/claude-opus-4.6": _rate(5.00, 0.50, 25.00, 6.25),
    "anthropic/claude-opus-4.7": _rate(5.00, 0.50, 25.00, 6.25),
    "anthropic/claude-opus-4.8": _rate(5.00, 0.50, 25.00, 6.25),
    # Sonnet 4/4.5/4.6 and Haiku 4.5 all still sit at their family rates.
    "anthropic/claude-sonnet-4": _rate(3.00, 0.30, 15.00, 3.75),
    "anthropic/claude-haiku-4": _rate(1.00, 0.10, 5.00, 1.25),
    # Per platform.claude.com/docs/en/about-claude/pricing (checked 2026-07-04):
    # $10/MTok input, $1/MTok cache read, $50/MTok output, $12.50/MTok 5m cache write.
    "anthropic/claude-fable-5": _rate(10.00, 1.00, 50.00, 12.50),
    "anthropic/claude-mythos-5": _rate(10.00, 1.00, 50.00, 12.50),
    # Opus 5 (released 2026-07-23) holds Opus 4.8's prices, per
    # platform.claude.com/docs/en/about-claude/pricing (checked 2026-07-25):
    # $5/MTok input, $0.50/MTok cache read, $25/MTok output, $6.25/MTok 5m cache write.
    # Fast mode bills Opus 5 and 4.8 at $10/$50 and would need its own key if it
    # ever lands as a distinct model ID (the bare key below would underprice it).
    "anthropic/claude-opus-5": _rate(5.00, 0.50, 25.00, 6.25),
    # Sonnet 5 standard pricing, effective 2026-09-01 and identical to Sonnet 4.6.
    # Introductory pricing ($2/MTok input, $10/MTok output) runs through
    # 2026-08-31; this table is date-blind, so Sonnet 5 usage logged before that
    # date is valued about 50% high.
    "anthropic/claude-sonnet-5": _rate(3.00, 0.30, 15.00, 3.75),
}

_UNPRICED_MODEL_PREFIXES: tuple[()] = ()


def published_model_rates(context: str = "short") -> dict[str, dict]:
    """Return a copy of the built-in published model-rate fallback table."""
    table = _PUBLISHED_MODEL_RATES_LONG if context == "long" else _PUBLISHED_MODEL_RATES_SHORT
    result = {model: rates.copy() for model, rates in table.items()}
    result.update({r["model"]: r["rates"].copy() for r in verified_pricing.catalog()
                   if r["model"] == verified_pricing.ASTRA and r["service_tier"] == "standard" and r["context_tier"] == context})
    return result


def static_fallback_model_rates() -> dict[str, dict]:
    """Return a copy of built-in fallback rates used for subscription rails."""
    result = {model: rates.copy() for model, rates in _STATIC_FALLBACK_MODEL_RATES.items()}
    result[verified_pricing.FABLE] = verified_pricing.quote({"model": verified_pricing.FABLE})["rates"]
    return result


def _model_prefix_matches(model: str, key: str) -> bool:
    """Return true when `key` is a safe model-family prefix for `model`."""
    if verified_pricing.protects(model) or verified_pricing.protects(key):
        return False
    if key == "anthropic/claude-fable-5" and model.startswith(key + "."):
        return False
    return model.startswith(key + "-") or model.startswith(key + ".")


def _find_published_rate(model: str, long_context: bool = False) -> Optional[dict]:
    """Find a published OpenAI rate, including variant-prefix matches."""
    if model in verified_pricing.MODELS:
        return verified_pricing.quote({"model": model, "input": 272001 if long_context else 0})["rates"]
    if any(model.startswith(prefix) for prefix in _UNPRICED_MODEL_PREFIXES):
        return None

    tables = [_PUBLISHED_MODEL_RATES_LONG, _PUBLISHED_MODEL_RATES_SHORT] if long_context else [_PUBLISHED_MODEL_RATES_SHORT]
    for table in tables:
        if model in table:
            return table[model].copy()
        best: Optional[str] = None
        best_len = 0
        for key in table:
            if _model_prefix_matches(model, key) and len(key) > best_len:
                best = key
                best_len = len(key)
        if best:
            return table[best].copy()
    return None


def _uses_long_context(msg: dict) -> bool:
    """Return true when a message's prompt context crosses OpenAI's long-context threshold."""
    prompt_tokens = (msg.get("input", 0) or 0) + (msg.get("cache_read", 0) or 0) + (msg.get("cache_write", 0) or 0)
    if msg.get("model") == verified_pricing.FABLE:
        return False
    threshold = 272000 if msg.get("model") == verified_pricing.ASTRA else _LONG_CONTEXT_THRESHOLD_TOKENS
    return prompt_tokens > threshold


def empty_tokens() -> dict:
    return {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0, "cache_write": 0}


def validate_token_value(value, category: str) -> None:
    """Strict ingestion must not turn malformed usage into a reliable zero."""
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ValueError(f"Invalid token count: {category}")
    try:
        if int(value) != value:
            raise ValueError(f"Non-integral token count: {category}")
    except (OverflowError, ValueError):
        raise ValueError(f"Invalid token count: {category}") from None


def empty_bucket() -> dict:
    return {
        "tokens": empty_tokens(),
        "cash_cost": 0.0,
        "implied_cost": 0.0,
        "cost": 0.0,
        "messages": 0,
        "sessions": set(),
    }


def merge_bucket(target: dict, source: dict) -> None:
    for k in target["tokens"]:
        target["tokens"][k] += source["tokens"][k]
    target["cash_cost"] += source.get("cash_cost", source.get("cost", 0.0))
    target["implied_cost"] += source.get("implied_cost", source.get("cost", 0.0))
    target["cost"] = target["implied_cost"]
    target["messages"] += source["messages"]
    target["sessions"] |= source["sessions"]


# ---------------------------------------------------------------------------
# Message reading
# ---------------------------------------------------------------------------

def _connect_readonly(db_path: str) -> sqlite3.Connection:
    """Open SQLite in read-only URI mode with one retry on transient locks.

    OneDrive sync and opencode WAL activity can briefly lock a DB. A single
    500ms retry handles the common transient cases without masking real bugs.
    """
    try:
        return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
    except (sqlite3.OperationalError, PermissionError):
        time.sleep(0.5)
        return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)


def read_messages_from_db(db_path: str, mid: Optional[str] = None, *, strict: bool = False) -> list[dict]:
    """Read all assistant messages with token data from a single DB.

    Each row is tagged with `machine_id` (defaults to this machine if not
    supplied) and `msg_id` (the SQLite primary key) for deduping across
    pooled sources. `source="opencode"` discriminates from Cursor records.
    """
    mid = mid or machine_id()
    conn = _connect_readonly(db_path)
    conn.row_factory = sqlite3.Row
    rows = []
    for row in conn.execute(
        "SELECT m.id AS msg_id, m.data, m.session_id, "
        "s.title, s.version, s.directory, s.project_id, s.time_created AS session_created "
        "FROM message m JOIN session s ON m.session_id = s.id"
    ):
        try:
            data = json.loads(row["data"])
        except (json.JSONDecodeError, TypeError):
            if strict:
                conn.close()
                raise ValueError("Malformed OpenCode message JSON") from None
            continue
        tokens = data.get("tokens")
        if not tokens:
            continue
        cache = tokens.get("cache") or {}
        model_raw = data.get("modelID") or (data.get("model") or {}).get("modelID") or (data.get("model") or {}).get("id") or "unknown"
        provider = data.get("providerID") or (data.get("model") or {}).get("providerID") or "unknown"
        raw_cost = float(data.get("cost", 0) or 0)
        billing_source = infer_billing_source("opencode", provider, raw_cost)
        rows.append({
            "source": "opencode",
            "machine_id": mid,
            "msg_id": row["msg_id"],
            "model": normalize_model(model_raw),
            "provider": provider,
            "billing_source": billing_source,
            "cash_cost": raw_cost if billing_source == "api" else 0.0,
            "implied_cost": raw_cost if billing_source == "api" else 0.0,
            "cost": raw_cost if billing_source == "api" else 0.0,
            "input": tokens.get("input", 0) or 0,
            "output": tokens.get("output", 0) or 0,
            "reasoning": tokens.get("reasoning", 0) or 0,
            "cache_read": cache.get("read", 0) or 0,
            "cache_write": cache.get("write", 0) or 0,
            "time": data.get("time", {}).get("created") or data.get("time", 0),
            "session_id": row["session_id"],
            "session_title": row["title"],
            "session_dir": row["directory"],
            "project_id": row["project_id"],
            "opencode_version": row["version"],
            "db_file": os.path.basename(db_path),
            "recorded_cost": float(data["cost"]) if data.get("cost") is not None else None,
            "original_usage": tokens,
        })
    conn.close()
    return rows


# Backward-compat alias (private name is still imported by tests / old callers)
_read_messages = read_messages_from_db


def _read_export_file(path: str) -> list[dict]:
    """Read an exported JSONL file from another machine.

    The first line may be a `{"_meta": ...}` header; it's skipped if so.
    Transient OneDrive locks get one retry.
    """
    for attempt in range(2):
        try:
            with open(path, "r", encoding="utf-8") as f:
                rows = []
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if "_meta" in obj:
                        continue
                    rows.append(obj)
                return rows
        except (OSError, PermissionError):
            if attempt == 0:
                time.sleep(0.5)
                continue
            return []
    return []


def derive_rates_from_opencode(messages: list[dict], include_published_fallbacks: bool = True) -> dict[str, dict]:
    """Derive per-model per-token rates from OpenCode's own historical cost data.

    OpenCode stores a `cost` field on every assistant message, computed from
    the model's published per-token rates. We back-solve for the implied
    input rate per model using the standard Anthropic-style pricing ratio
    (output = 5x input, cache_read = 0.1x input, cache_write = 1.25x input):

        cost = input*r + output*5r + cache_read*0.1r + cache_write*1.25r

    This auto-adapts as new models appear in OpenCode's history. For known
    OpenAI GPT/Codex models, a small published-rate table overrides derived
    rates because the generic derivation is only approximate for OpenAI output
    and cache ratios.
    """
    from collections import defaultdict
    sums = defaultdict(lambda: {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0, "cache_write": 0, "cost": 0.0})
    for m in messages:
        if m.get("source", "opencode") != "opencode":
            continue
        if m.get("billing_source") != "api":
            continue
        b = sums[m["model"]]
        b["input"] += m.get("input", 0) or 0
        b["output"] += m.get("output", 0) or 0
        b["reasoning"] += m.get("reasoning", 0) or 0
        b["cache_read"] += m.get("cache_read", 0) or 0
        b["cache_write"] += m.get("cache_write", 0) or 0
        b["cost"] += m.get("cash_cost", m.get("cost", 0)) or 0

    rates: dict[str, dict] = {}
    for model, b in sums.items():
        equiv = b["input"] + 5 * (b["output"] + b["reasoning"]) + 0.1 * b["cache_read"] + 1.25 * b["cache_write"]
        if equiv <= 0 or b["cost"] <= 0:
            continue
        r = b["cost"] / equiv
        rates[model] = {
            "input": r,
            "output": 5 * r,
            "cache_read": 0.1 * r,
            "cache_write": 1.25 * r,
        }
    if include_published_fallbacks:
        for model, published_rates in published_model_rates().items():
            rates[model] = published_rates
        for model, fallback_rates in static_fallback_model_rates().items():
            rates.setdefault(model, fallback_rates)
    return rates


def _find_rate(model: str, rates: dict[str, dict], long_context: bool = False) -> Optional[dict]:
    """Look up rate for a model, with prefix-match fallback.

    Cursor appends variant suffixes that OpenCode's rate table doesn't have:
        anthropic/claude-opus-4.6-high-thinking -> falls back to anthropic/claude-opus-4.6
        openai/gpt-5.1-codex-max-xhigh -> falls back to openai/gpt-5.1-codex (if exists)

    Strategy: longest-prefix match wins, so we pick the most specific known
    rate that's compatible with this model ID.
    """
    published_rate = _find_published_rate(model, long_context=long_context)
    if published_rate:
        return published_rate

    if model in rates:
        return rates[model]
    if any(model.startswith(prefix) for prefix in _UNPRICED_MODEL_PREFIXES):
        return None
    best: Optional[str] = None
    best_len = 0
    for key in rates:
        if _model_prefix_matches(model, key) and len(key) > best_len:
            best = key
            best_len = len(key)
    return rates[best] if best else None


def impute_cost(msg: dict, rates: dict[str, dict]) -> float:
    """Apply derived rates to a message's tokens to compute implied cost.

    Returns 0.0 if no matching observed or published rate is found
    (e.g. Cursor-only models like `cursor/composer-1`).
    """
    if msg["model"] in verified_pricing.MODELS:
        return verified_pricing.quote(msg)["cost"] or 0.0
    rate = _find_rate(msg["model"], rates, long_context=_uses_long_context(msg))
    if not rate:
        return 0.0
    return (
        rate["input"] * (msg.get("input", 0) or 0)
        + rate["output"] * ((msg.get("output", 0) or 0) + (msg.get("reasoning", 0) or 0))
        + rate["cache_read"] * (msg.get("cache_read", 0) or 0)
        + rate["cache_write"] * (msg.get("cache_write", 0) or 0)
    )


def load_all_messages(
    data_dir: str = DATA_DIR,
    exports_dir: str = EXPORTS_DIR,
    pool: bool = True,
    include_claude_code: bool = True,
    include_cursor: bool = True,
    impute_cursor_cost: bool = True,
    include_codex: bool = True,
) -> list[dict]:
    """Load messages from all available sources:

    - Local OpenCode SQLite DBs (always)
    - Local Claude Code project JSONL logs (if `include_claude_code` and available)
    - Synced exports from other machines (if `pool`) — may contain
      opencode, Claude Code, Cursor, and Codex records
    - Cursor dashboard usage CSV, if available, else local Cursor state.vscdb
      fallback (if `include_cursor`)
    - Local Codex Desktop session JSONL logs (if `include_codex` and available)

    Deduplication: records keyed by `(machine_id, source, msg_id)`. Local sources
    win over pooled exports (we read local first; later duplicates skipped).

    Records carry a `source` field: "opencode", "claude-code", "cursor", or
    "codex". Subscription/prepaid rows arrive with `cost=0.0` from disk because
    local state doesn't expose per-request cash spend. After all sources are
    loaded, we re-impute API-equivalent cost on non-metered records using the
    combined OpenCode/static rate table.
    """
    local_mid = machine_id()
    seen: set[tuple[str, str, str]] = set()
    merged: list[dict] = []

    def _dedup_key(msg: dict) -> tuple[str, str, str]:
        return (msg["machine_id"], msg.get("source", "opencode"), msg["msg_id"])

    def _hydrate_record(msg: dict) -> dict:
        source = msg.get("source", "opencode")
        provider = msg.get("provider") or msg.get("providerID") or "unknown"
        msg["provider"] = provider

        if source in {"claude-code", "cursor", "codex"}:
            msg.setdefault("billing_source", "subscription" if source == "claude-code" else source)
            msg.setdefault("cash_cost", 0.0)
            msg.setdefault("implied_cost", 0.0)
            msg.setdefault("cost_imputed", False)
        else:
            raw_cost = float(msg.get("cash_cost", msg.get("cost", 0.0)) or 0.0)
            billing_source = msg.get("billing_source") or infer_billing_source(source, provider, raw_cost)
            msg["billing_source"] = billing_source
            if billing_source == "api":
                msg.setdefault("cash_cost", raw_cost)
                msg.setdefault("implied_cost", raw_cost)
            else:
                msg.setdefault("cash_cost", 0.0)
                msg.setdefault("implied_cost", 0.0)
            msg.setdefault("cost_imputed", False)

        msg["cost"] = msg.get("implied_cost", 0.0)
        return msg

    # Local OpenCode DBs first — ground truth for this machine.
    for db_path in discover_databases(data_dir):
        for msg in read_messages_from_db(db_path, mid=local_mid):
            _hydrate_record(msg)
            key = _dedup_key(msg)
            if key in seen:
                continue
            seen.add(key)
            merged.append(msg)

    # Local Claude Code logs — subscription rail with usage stored in project JSONL.
    if include_claude_code:
        try:
            from claude_code_reader import read_claude_code_messages
            for msg in read_claude_code_messages(mid=local_mid):
                _hydrate_record(msg)
                key = _dedup_key(msg)
                if key in seen:
                    continue
                seen.add(key)
                merged.append(msg)
        except Exception:
            # Claude Code state unavailable or unreadable — just skip, don't fail.
            pass

    # Exports from other machines — skip our own export file (local DBs win).
    # As of v2 these may carry both opencode and cursor records.
    if pool:
        for exp_path in discover_exports(exports_dir):
            basename = os.path.splitext(os.path.basename(exp_path))[0]
            if basename == local_mid:
                continue
            for msg in _read_export_file(exp_path):
                if "msg_id" not in msg or "machine_id" not in msg:
                    continue
                # Older exports predate the `source` field — default them.
                msg.setdefault("source", "opencode")
                _hydrate_record(msg)
                key = _dedup_key(msg)
                if key in seen:
                    continue
                seen.add(key)
                merged.append(msg)

    # Cursor dashboard usage CSV — authoritative over local state.vscdb when
    # available because newer Cursor builds often store zero local bubble tokens.
    cursor_csv_loaded = False
    if include_cursor:
        try:
            from cursor_usage_csv_reader import read_cursor_usage_csv_messages
            cursor_csv_messages = read_cursor_usage_csv_messages(mid=local_mid)
            cursor_csv_loaded = bool(cursor_csv_messages)
            for msg in cursor_csv_messages:
                _hydrate_record(msg)
                key = _dedup_key(msg)
                if key in seen:
                    continue
                seen.add(key)
                merged.append(msg)
        except Exception:
            # Dashboard export unavailable or malformed — fall back to local state.
            cursor_csv_loaded = False

    # Local Cursor state — separate fallback source. Local read is authoritative
    # over any same-machine cursor records that snuck in via own export.
    if include_cursor and not cursor_csv_loaded:
        try:
            from cursor_reader import read_cursor_messages
            for msg in read_cursor_messages(mid=local_mid):
                _hydrate_record(msg)
                key = _dedup_key(msg)
                if key in seen:
                    continue
                seen.add(key)
                merged.append(msg)
        except Exception:
            # Cursor state unavailable or unreadable — just skip, don't fail.
            pass

    # Local Codex Desktop session logs — separate source. Local read is
    # authoritative over any same-machine codex records that snuck in via own export.
    if include_codex:
        try:
            from codex_reader import read_codex_messages
            for msg in read_codex_messages(mid=local_mid):
                _hydrate_record(msg)
                key = _dedup_key(msg)
                if key in seen:
                    continue
                seen.add(key)
                merged.append(msg)
        except Exception:
            # Codex state unavailable or unreadable — just skip, don't fail.
            pass

    # Re-impute cost on every non-metered record using the combined OpenCode
    # and static fallback rate table. This catches local/pooled Claude Code,
    # Cursor, Codex Desktop, and GHCP rows that carry no literal cash cost.
    if impute_cursor_cost:
        rates = derive_rates_from_opencode(merged)
        if rates:
            for msg in merged:
                if msg.get("billing_source") in {"subscription", "cursor", "codex", "ghcp"}:
                    existing_implied_cost = msg.get("implied_cost", 0.0) or 0.0
                    imputed_cost = impute_cost(msg, rates)
                    msg["implied_cost"] = max(existing_implied_cost, imputed_cost)
                    msg["cost_imputed"] = imputed_cost > existing_implied_cost
                else:
                    msg["implied_cost"] = msg.get("cash_cost", msg.get("cost", 0.0)) or 0.0
                msg["cost"] = msg["implied_cost"]
    else:
        for msg in merged:
            msg["implied_cost"] = msg.get("implied_cost", msg.get("cash_cost", msg.get("cost", 0.0)) or 0.0)
            msg["cost"] = msg["implied_cost"]

    merged.sort(key=lambda m: m["time"] if isinstance(m["time"], (int, float)) else 0)
    return merged


# ---------------------------------------------------------------------------
# Time window helpers
# ---------------------------------------------------------------------------

def _ts_to_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _msg_ts(msg: dict) -> int:
    t = msg["time"]
    if isinstance(t, (int, float)):
        return int(t)
    if isinstance(t, dict):
        return int(t.get("created", 0))
    return 0


def time_windows() -> dict[str, int]:
    """Return cutoff timestamps (ms) for standard windows."""
    now = datetime.now()
    return {
        "today": _ts_to_ms(now.replace(hour=0, minute=0, second=0, microsecond=0)),
        "this_week": _ts_to_ms((now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)),
        "this_month": _ts_to_ms(now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)),
        "all_time": 0,
    }


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def _add_msg_to_bucket(bucket: dict, msg: dict) -> None:
    bucket["tokens"]["input"] += msg["input"]
    bucket["tokens"]["output"] += msg["output"]
    bucket["tokens"]["reasoning"] += msg["reasoning"]
    bucket["tokens"]["cache_read"] += msg["cache_read"]
    bucket["tokens"]["cache_write"] += msg["cache_write"]
    bucket["cash_cost"] += msg.get("cash_cost", 0.0)
    bucket["implied_cost"] += msg.get("implied_cost", msg.get("cost", 0.0))
    bucket["cost"] = bucket["implied_cost"]
    bucket["messages"] += 1
    bucket["sessions"].add(msg["session_id"])


def aggregate_summary(messages: list[dict]) -> dict[str, dict]:
    """Aggregate messages into standard time windows."""
    windows = time_windows()
    result = {name: empty_bucket() for name in windows}
    for msg in messages:
        ts = _msg_ts(msg)
        for name, cutoff in windows.items():
            if ts >= cutoff:
                _add_msg_to_bucket(result[name], msg)
    # Convert session sets to counts for serialization
    for bucket in result.values():
        bucket["sessions"] = len(bucket["sessions"])
    return result


def aggregate_by_source(messages: list[dict], since_ms: int = 0) -> dict[str, dict]:
    """Aggregate messages by source (opencode vs claude-code vs cursor vs codex)."""
    result: dict[str, dict] = {}
    for msg in messages:
        if _msg_ts(msg) < since_ms:
            continue
        source = msg.get("source", "opencode")
        if source not in result:
            result[source] = empty_bucket()
            result[source]["estimated_messages"] = 0
        _add_msg_to_bucket(result[source], msg)
        if msg.get("token_estimated"):
            result[source]["estimated_messages"] += msg.get("message_count", 1) or 1
    for bucket in result.values():
        bucket["sessions"] = len(bucket["sessions"])
    return result


def aggregate_by_billing_source(messages: list[dict], since_ms: int = 0) -> dict[str, dict]:
    """Aggregate messages by billing rail (api / ghcp / cursor / codex / unknown)."""
    result: dict[str, dict] = {}
    for msg in messages:
        if _msg_ts(msg) < since_ms:
            continue
        billing_source = msg.get("billing_source", "unknown")
        if billing_source not in result:
            result[billing_source] = empty_bucket()
        _add_msg_to_bucket(result[billing_source], msg)
    for bucket in result.values():
        bucket["sessions"] = len(bucket["sessions"])
    return dict(sorted(result.items(), key=lambda kv: -kv[1]["implied_cost"]))


def aggregate_by_model(messages: list[dict], since_ms: int = 0) -> dict[str, dict]:
    """Aggregate messages by normalized model ID since a cutoff."""
    result: dict[str, dict] = {}
    for msg in messages:
        if _msg_ts(msg) < since_ms:
            continue
        model = msg["model"]
        if model not in result:
            result[model] = empty_bucket()
        _add_msg_to_bucket(result[model], msg)
    for bucket in result.values():
        bucket["sessions"] = len(bucket["sessions"])
    return dict(sorted(result.items(), key=lambda kv: -kv[1]["cost"]))


def aggregate_by_session(messages: list[dict], since_ms: int = 0, limit: int = 20) -> list[dict]:
    """Aggregate messages by session, sorted by cost descending."""
    sessions: dict[str, dict] = {}
    for msg in messages:
        if _msg_ts(msg) < since_ms:
            continue
        sid = msg["session_id"]
        if sid not in sessions:
            sessions[sid] = {
                "session_id": sid,
                "title": msg["session_title"],
                "directory": msg["session_dir"],
                "version": msg["opencode_version"],
                **empty_bucket(),
            }
        s = sessions[sid]
        _add_msg_to_bucket(s, msg)
    rows = sorted(sessions.values(), key=lambda s: -s["cost"])
    for row in rows:
        if "sessions" in row:
            del row["sessions"]
    return rows[:limit]


def query_usage(
    messages: list[dict],
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    model: Optional[str] = None,
    project: Optional[str] = None,
) -> dict:
    """Flexible query with optional filters. Returns a single bucket."""
    start_ms = _ts_to_ms(start) if start else 0
    end_ms = _ts_to_ms(end) if end else float("inf")
    bucket = empty_bucket()
    for msg in messages:
        ts = _msg_ts(msg)
        if ts < start_ms or ts > end_ms:
            continue
        if model and model.lower() not in msg["model"].lower():
            continue
        if project and project.lower() not in (msg["project_id"] or "").lower() and project.lower() not in (msg["session_dir"] or "").lower():
            continue
        _add_msg_to_bucket(bucket, msg)
    bucket["sessions"] = len(bucket["sessions"])
    return bucket


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def format_tokens(n: int) -> str:
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.2f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def format_cost(c: float | None) -> str:
    if c is None:
        return "unknown"
    if c >= 1.0:
        return f"${c:.2f}"
    return f"${c:.4f}"


COST_NOTE = ("*Recorded cost sums amounts present in source events; it is not an invoice or a subscription bill. "
             "Recorded coverage shows how many events contain those amounts. API-equivalent estimates are hypothetical token costs; "
             "unknown means no complete amount is available, not zero. Source coverage describes ingestion completeness.*")


def cost_fields(bucket: dict) -> dict[str, str]:
    """Render authoritative ledger fields without falling back to blended legacy costs."""
    recorded = bucket.get("recorded_cost_messages")
    messages = bucket.get("messages")
    coverage = "unknown" if recorded is None or messages is None else f"{recorded}/{messages} events"
    if recorded is not None and messages is not None and recorded < messages:
        coverage += " (partial)"
    complete = bucket.get("data_quality", {}).get("complete")
    return {
        "Recorded cost (USD)": format_cost(bucket.get("recorded_cost")),
        "Recorded coverage": coverage,
        "API-equivalent estimate (USD)": format_cost(bucket.get("api_equivalent_cost")),
        "Unpriced events": str(bucket.get("unpriced_messages", "unknown")),
        "Source coverage": "complete" if complete is True else "incomplete/stale" if complete is False else "unknown",
    }


def summary_to_markdown(summary: dict[str, dict]) -> str:
    headers = ["Period", *cost_fields({}), "Input", "Output", "Cache Read", "Cache Write", "Messages", "Sessions"]
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    labels = {"today": "Today", "this_week": "This Week", "this_month": "This Month", "all_time": "All Time"}
    for key in ["today", "this_week", "this_month", "all_time"]:
        b = summary[key]
        t = b["tokens"]
        lines.append(
            f"| {labels[key]} | {' | '.join(cost_fields(b).values())} | {format_tokens(t['input'])} | "
            f"{format_tokens(t['output'])} | {format_tokens(t['cache_read'])} | "
            f"{format_tokens(t['cache_write'])} | {b['messages']} | {b['sessions']} |"
        )
    return "\n".join(lines) + "\n\n" + COST_NOTE


def sources_to_markdown(by_source: dict[str, dict]) -> str:
    """Render recorded amounts and API-equivalent estimates with their coverage."""
    headers = ["Source", *cost_fields({}), "Input", "Output", "Cache Read", "Messages", "Sessions"]
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    for source, b in by_source.items():
        t = b["tokens"]
        estimated_messages = b.get("estimated_messages", 0)
        marker = "*" if source == "cursor" and estimated_messages else ("" if source != "cursor" else "")
        message_label = str(b["messages"])
        if estimated_messages:
            message_label = f"{message_label} (~{estimated_messages} est)"
        lines.append(
            f"| {source}{marker} | {' | '.join(cost_fields(b).values())} | {format_tokens(t['input'])} | "
            f"{format_tokens(t['output'])} | {format_tokens(t['cache_read'])} | "
            f"{message_label} | {b['sessions']} |"
        )
    lines.append("")
    lines.append(COST_NOTE)
    if any(bucket.get("estimated_messages", 0) for bucket in by_source.values()):
        lines.append("*Cursor rows marked with `*` are estimated from composer-level `contextTokensUsed` because this Cursor build stores zero per-bubble token counts locally.")
    return "\n".join(lines)


def models_to_markdown(by_model: dict[str, dict]) -> str:
    headers = ["Model", *cost_fields({}), "Input", "Output", "Cache Read", "Messages"]
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    for model, b in by_model.items():
        t = b["tokens"]
        lines.append(
            f"| {model} | {' | '.join(cost_fields(b).values())} | {format_tokens(t['input'])} | "
            f"{format_tokens(t['output'])} | {format_tokens(t['cache_read'])} | {b['messages']} |"
        )
    return "\n".join(lines) + "\n\n" + COST_NOTE


def sessions_to_markdown(sessions: list[dict]) -> str:
    headers = ["Title", *cost_fields({}), "Messages", "Input", "Output", "Cache Read", "Version"]
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    for s in sessions:
        t = s["tokens"]
        title = (s["title"] or "untitled")[:40]
        lines.append(
            f"| {title} | {' | '.join(cost_fields(s).values())} | {s['messages']} | "
            f"{format_tokens(t['input'])} | {format_tokens(t['output'])} | "
            f"{format_tokens(t['cache_read'])} | {s['version']} |"
        )
    return "\n".join(lines) + "\n\n" + COST_NOTE
