r"""
Cursor dashboard usage CSV reader.

Cursor's local `state.vscdb` can under-report usage on newer builds because
message-level token counts may be zero. The Cursor dashboard export is a better
source when available. This reader maps `usage-events-*.csv` rows into the same
message shape consumed by usage.py.

Default discovery order:
1. Explicit path argument
2. CURSOR_USAGE_EVENTS_CSV / CURSOR_USAGE_CSV environment variable
3. Latest `%USERPROFILE%\Downloads\usage-events-*.csv`
"""

from __future__ import annotations

import csv
import glob
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from cursor_reader import _normalize_cursor_model


REQUIRED_COLUMNS = {
    "Date",
    "Kind",
    "Model",
    "Max Mode",
    "Input (w/ Cache Write)",
    "Input (w/o Cache Write)",
    "Cache Read",
    "Output Tokens",
    "Total Tokens",
    "Cost",
}


def discover_cursor_usage_csvs() -> list[str]:
    """Return candidate Cursor dashboard CSV exports, newest first."""
    explicit = os.environ.get("CURSOR_USAGE_EVENTS_CSV") or os.environ.get("CURSOR_USAGE_CSV")
    if explicit:
        return [explicit]

    downloads = Path(os.environ.get("USERPROFILE", os.path.expanduser("~"))) / "Downloads"
    paths = glob.glob(str(downloads / "usage-events-*.csv"))
    return sorted(paths, key=lambda path: os.path.getmtime(path), reverse=True)


def _parse_int(value, strict: bool = False) -> int:
    text = str(value or "").strip().replace(",", "")
    if not text or text == "-":
        return 0
    number = float(text)
    if strict and (number < 0 or int(number) != number):
        raise ValueError("Invalid Cursor token count")
    return int(number)


def _parse_cost(value) -> float:
    text = str(value or "").strip().replace("$", "").replace(",", "")
    if text in {"", "-", "Included", "Free"}:
        return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


def _parse_timestamp_ms(value: str) -> int:
    text = (value or "").strip()
    if not text:
        return 0
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        return int(datetime.fromisoformat(text).timestamp() * 1000)
    except ValueError:
        return 0


def _read_rows(path: str) -> list[dict]:
    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = REQUIRED_COLUMNS - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"Cursor usage CSV is missing columns: {', '.join(sorted(missing))}")
        return list(reader)


def read_cursor_usage_csv_messages(
    csv_path: Optional[str] = None,
    mid: Optional[str] = None,
    *, strict: bool = False,
) -> list[dict]:
    """Read Cursor dashboard usage events as normalized usage records."""
    candidates = [csv_path] if csv_path else discover_cursor_usage_csvs()
    candidates = [path for path in candidates if path and os.path.exists(path)]
    if not candidates:
        return []

    # Import lazily to avoid a top-level usage.py dependency.
    from usage import machine_id

    mid = mid or machine_id()
    path = candidates[0]
    if strict:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell():
                handle.seek(-1, os.SEEK_END)
                if handle.read(1) != b"\n":
                    raise ValueError("Incomplete trailing Cursor CSV line; awaiting completion")
    basename = os.path.basename(path)
    rows = []

    for index, row in enumerate(_read_rows(path), start=1):
        if strict and (None in row or any(row.get(k) is None for k in REQUIRED_COLUMNS)):
            raise ValueError(f"Incomplete or malformed Cursor CSV row {index}")
        event_ms = _parse_timestamp_ms(row.get("Date", ""))
        if event_ms <= 0:
            if strict:
                raise ValueError(f"Invalid Cursor date at row {index}")
            continue

        raw_model = (row.get("Model") or "unknown").strip() or "unknown"
        model = _normalize_cursor_model(raw_model)
        provider = model.split("/", 1)[0] if "/" in model else "unknown"
        cash_cost = _parse_cost(row.get("Cost"))
        label = str(row.get("Cost") or "").strip()
        recorded = None
        if label not in {"", "-", "Included", "Free"}:
            try:
                recorded = float(label.replace("$", "").replace(",", ""))
            except ValueError:
                if strict:
                    raise ValueError(f"Unknown Cursor cost at row {index}") from None
        day = datetime.fromtimestamp(event_ms / 1000).strftime("%Y-%m-%d")
        input_tokens = _parse_int(row.get("Input (w/o Cache Write)"), strict)
        cache_write = _parse_int(row.get("Input (w/ Cache Write)"), strict)
        cache_read = _parse_int(row.get("Cache Read"), strict)
        output_tokens = _parse_int(row.get("Output Tokens"), strict)
        total_tokens = _parse_int(row.get("Total Tokens"), strict)

        if input_tokens == 0 and cache_write == 0 and cache_read == 0 and output_tokens == 0:
            continue

        rows.append({
            "source": "cursor",
            "machine_id": mid,
            "msg_id": f"cursor-usage-csv:{basename}:{index}",
            "model": model,
            "provider": provider,
            "billing_source": "cursor",
            "cash_cost": cash_cost,
            "implied_cost": cash_cost,
            "cost": cash_cost,
            "input": input_tokens,
            "output": output_tokens,
            "reasoning": 0,
            "cache_read": cache_read,
            "cache_write": cache_write,
            "time": event_ms,
            "session_id": f"cursor-usage-csv:{day}",
            "session_title": f"Cursor usage export {day}",
            "session_dir": "",
            "project_id": "cursor",
            "opencode_version": "cursor usage csv",
            "db_file": basename,
            "message_count": 1,
            "token_estimated": False,
            "cursor_usage_mode": "dashboard_usage_events_csv",
            "cursor_kind": row.get("Kind") or "",
            "cursor_max_mode": row.get("Max Mode") or "",
            "cursor_cost_label": row.get("Cost") or "",
            "cursor_total_tokens": total_tokens,
            "recorded_cost": recorded,
            "original_usage": row,
            "source_line": index + 1,
        })

    return rows
