"""
OpenCode Usage MCP Server
-------------------------
Provides tools for querying token usage and cost data across OpenCode, Claude
Code, Cursor, and Codex Desktop. Reads existing local data stores — never writes
to them.

Transport: stdio (local server)
Tool calls require OPENCODE_USAGE_ALLOW_DISCOVERY=1 and OPENCODE_USAGE_DB.
Importing the server and listing tools never opens a ledger or reads logs.
"""

import asyncio
import os
from datetime import datetime, timedelta
from typing import Literal, Optional

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field

from usage import (
    models_to_markdown,
    sessions_to_markdown,
    sources_to_markdown,
    summary_to_markdown,
    time_windows,
    format_cost,
    format_tokens,
)
from usage_store import UsageStore

mcp = FastMCP("opencode_usage")

ResponseFormat = Literal["markdown", "json"]


def _authorized_db():
    """Check permission before constructing the store or discovering sources."""
    if os.environ.get("OPENCODE_USAGE_ALLOW_DISCOVERY") != "1":
        raise RuntimeError("Local discovery disabled. Set OPENCODE_USAGE_ALLOW_DISCOVERY=1 to permit reading local usage logs.")
    path = os.environ.get("OPENCODE_USAGE_DB")
    if not path or not path.strip():
        raise RuntimeError("Set OPENCODE_USAGE_DB to an explicit private ledger destination.")
    return path


def _query(group=None, *, summary=False, **kwargs):
    """Refresh changed sources, then query precomputed daily facts."""
    with UsageStore(_authorized_db()) as store:
        status = store.refresh()
        if summary:
            result = {name: store.aggregate(start=cutoff) for name, cutoff in time_windows().items()}
        else:
            result = store.aggregate(group=group, **kwargs)
        quality = {
            "complete": status["complete"],
            "pricing_complete": status["pricing_complete"],
            "source_issues": len(status["source_failures"]) + len(status.get("issues", [])),
            "pricing_note": status["pricing_note"],
        }
        buckets = result if isinstance(result, list) else result.values() if group or summary else [result]
        if not result and not status["complete"]:
            raise RuntimeError("Usage coverage is incomplete; use usage_status for source diagnostics")
        for bucket in buckets:
            bucket["data_quality"] = quality
        return result, status


def _health_note(status):
    notes = []
    if not status["complete"]:
        notes.append("Coverage incomplete or stale; use usage_status for source failures.")
    if status["unpriced"]:
        notes.append("Some usage has no API-equivalent price; cost totals are partial estimates.")
    notes.append("API-equivalent costs are estimates; imported legacy prices have unknown effective dates.")
    return "\n\n*" + " ".join(notes) + "*"


# ---------------------------------------------------------------------------
# Tool 1: usage_summary
# ---------------------------------------------------------------------------

class UsageSummaryInput(BaseModel):
    """Input for usage summary."""
    response_format: ResponseFormat = Field(
        default="markdown",
        description="Output format: 'markdown' or 'json'.",
    )


@mcp.tool(
    name="usage_summary",
    description=(
        "Get a summary of OpenCode, Claude Code, Cursor, and Codex token usage and cost across standard time "
        "windows: today, this week, this month, and all time. Returns input, "
        "output, cache read, cache write tokens, cost, message count, and "
        "session count for each window."
    ),
)
async def usage_summary(params: UsageSummaryInput) -> str:
    summary, status = await asyncio.to_thread(_query, summary=True)
    if params.response_format == "json":
        import json
        return json.dumps(summary, indent=2, default=str)
    return summary_to_markdown(summary) + _health_note(status)


# ---------------------------------------------------------------------------
# Tool 2: usage_by_model
# ---------------------------------------------------------------------------

class UsageByModelInput(BaseModel):
    """Input for per-model usage breakdown."""
    period: Optional[str] = Field(
        default="all_time",
        description="Time period: 'today', 'this_week', 'this_month', or 'all_time'.",
    )
    response_format: ResponseFormat = Field(
        default="markdown",
        description="Output format: 'markdown' or 'json'.",
    )


@mcp.tool(
    name="usage_by_model",
    description=(
        "Break down OpenCode, Claude Code, Cursor, and Codex token usage and cost by model (e.g. Claude Opus 4.7, "
        "GPT-5.4). Sorted by cost descending. Model IDs are normalized so variants "
        "like 'claude-opus-4-7' and 'anthropic/claude-opus-4.7' are merged."
    ),
)
async def usage_by_model(params: UsageByModelInput) -> str:
    windows = time_windows()
    since = windows.get(params.period, 0)
    by_model, status = await asyncio.to_thread(_query, "model", start=since)
    if params.response_format == "json":
        import json
        return json.dumps(by_model, indent=2, default=str)
    return models_to_markdown(by_model) + _health_note(status)


# ---------------------------------------------------------------------------
# Tool 3: usage_sessions
# ---------------------------------------------------------------------------

class UsageSessionsInput(BaseModel):
    """Input for per-session usage listing."""
    period: Optional[str] = Field(
        default="all_time",
        description="Time period: 'today', 'this_week', 'this_month', or 'all_time'.",
    )
    limit: int = Field(
        default=15,
        description="Maximum number of sessions to return.",
        ge=1,
        le=100,
    )
    response_format: ResponseFormat = Field(
        default="markdown",
        description="Output format: 'markdown' or 'json'.",
    )


@mcp.tool(
    name="usage_sessions",
    description=(
        "List sessions ranked by cost. Shows title, cost, message count, "
        "token breakdown, and source/version per session."
    ),
)
async def usage_sessions(params: UsageSessionsInput) -> str:
    windows = time_windows()
    since = windows.get(params.period, 0)
    sessions, status = await asyncio.to_thread(_query, "session", start=since, limit=params.limit)
    if params.response_format == "json":
        import json
        return json.dumps(sessions, indent=2, default=str)
    return sessions_to_markdown(sessions) + _health_note(status)


# ---------------------------------------------------------------------------
# Tool 4: usage_by_source
# ---------------------------------------------------------------------------

class UsageBySourceInput(BaseModel):
    """Input for source breakdown (opencode vs claude-code vs cursor vs codex)."""
    period: Optional[str] = Field(
        default="all_time",
        description="Time period: 'today', 'this_week', 'this_month', or 'all_time'.",
    )
    response_format: ResponseFormat = Field(
        default="markdown",
        description="Output format: 'markdown' or 'json'.",
    )


@mcp.tool(
    name="usage_by_source",
    description=(
        "Break down token usage by data source: 'opencode' (detailed, with "
        "cost and cache data), 'claude-code' (Claude Code project logs), "
        "'cursor' (local subscription/prepaid state), and 'codex' (Codex "
        "Desktop session logs). Useful for seeing total LLM activity across tools."
    ),
)
async def usage_by_source_tool(params: UsageBySourceInput) -> str:
    windows = time_windows()
    since = windows.get(params.period, 0)
    by_source, status = await asyncio.to_thread(_query, "source", start=since)
    if params.response_format == "json":
        import json
        return json.dumps(by_source, indent=2, default=str)
    return sources_to_markdown(by_source) + _health_note(status)


# ---------------------------------------------------------------------------
# Tool 5: usage_query
# ---------------------------------------------------------------------------

class UsageQueryInput(BaseModel):
    """Flexible usage query with optional filters."""
    start_date: Optional[str] = Field(
        default=None,
        description="Start date in YYYY-MM-DD format. Omit for no lower bound.",
    )
    end_date: Optional[str] = Field(
        default=None,
        description="End date in YYYY-MM-DD format. Omit for no upper bound.",
    )
    model: Optional[str] = Field(
        default=None,
        description="Filter by model name (partial match, case-insensitive). E.g. 'opus', 'gpt-5'.",
    )
    project: Optional[str] = Field(
        default=None,
        description="Filter by project ID or directory path (partial match).",
    )
    client_id: Optional[str] = Field(default=None, description="Filter by an explicitly assigned stable client ID.")
    response_format: ResponseFormat = Field(
        default="markdown",
        description="Output format: 'markdown' or 'json'.",
    )


@mcp.tool(
    name="usage_query",
    description=(
        "Flexible query for OpenCode, Claude Code, Cursor, and Codex usage data. Filter by date range, model, "
        "and/or project. Returns aggregated tokens, cost, message count, and "
        "session count for the matching messages."
    ),
)
async def usage_query_tool(params: UsageQueryInput) -> str:
    start = datetime.strptime(params.start_date, "%Y-%m-%d") if params.start_date else None
    end = datetime.strptime(params.end_date, "%Y-%m-%d") + timedelta(days=1) if params.end_date else None
    result, status = await asyncio.to_thread(_query, start=int(start.timestamp()*1000) if start else 0,
                                           end=int(end.timestamp()*1000) if end else 2**62,
                                           model=params.model, project=params.project, client=params.client_id)
    if params.response_format == "json":
        import json
        return json.dumps(result, indent=2, default=str)
    t = result["tokens"]
    lines = [
        "| Metric | Value |",
        "|---|---|",
        f"| Cost | {format_cost(result['cost'])} |",
        f"| Messages | {result['messages']} |",
        f"| Sessions | {result['sessions']} |",
        f"| Input Tokens | {format_tokens(t['input'])} |",
        f"| Output Tokens | {format_tokens(t['output'])} |",
        f"| Reasoning Tokens | {format_tokens(t['reasoning'])} |",
        f"| Cache Read | {format_tokens(t['cache_read'])} |",
        f"| Cache Write | {format_tokens(t['cache_write'])} |",
    ]
    filters = []
    if params.start_date:
        filters.append(f"from {params.start_date}")
    if params.end_date:
        filters.append(f"to {params.end_date}")
    if params.model:
        filters.append(f"model ~ '{params.model}'")
    if params.project:
        filters.append(f"project ~ '{params.project}'")
    if params.client_id:
        filters.append(f"client = '{params.client_id}'")
    if filters:
        lines.insert(0, f"*Filters: {', '.join(filters)}*\n")
    return "\n".join(lines) + _health_note(status)


@mcp.tool(name="usage_status", description="Inspect incremental usage ingestion, source failures, stale snapshots, and missing API-equivalent pricing.")
async def usage_status_tool() -> str:
    import json
    def inspect():
        with UsageStore(_authorized_db()) as store:
            return store.refresh()
    return json.dumps(await asyncio.to_thread(inspect), indent=2)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run(transport="stdio")
