# AI Usage Ledger

A local Python tool for answering **“How much AI usage belongs to this project?”** across coding assistants. It turns usage records into an incremental SQLite ledger, filters by project, flags incomplete inputs, and separates recorded costs from API-equivalent estimates.

Built from practical independent R&D use: an all-account usage total is not enough when you need to attribute usage to individual projects. This is experimental observability tooling, not an invoice, timesheet or orchestration framework.

## Try it without connecting an account

Requires Python 3.11 or later; currently verified locally on 3.12 and 3.14. The demo and core tests have no third-party dependencies. Obtain the source and run:

```powershell
git clone https://github.com/PavolKum/ai-usage-ledger.git
cd ai-usage-ledger
python -B -S demo.py
python -B -S -m unittest -v test_usage_store test_verified_pricing test_release
```

The demo generates entirely fictional usage records inside a temporary directory and removes them when it finishes. It does not discover your application logs, read credentials, or contact providers. It demonstrates duplicate handling, unchanged-file skipping, and recovery behavior when a source becomes malformed. MCP tests are skipped when its optional dependencies are unavailable; see the verification record for what was actually run.

The demo separates two fictional projects across several assistants:

| Project | Input / output tokens | Recorded amounts | API-equivalent estimate |
|---|---|---|---|
| Aurora: OpenCode + Codex | 1,500 / 300 | $0.02 on 1 of 2 events | $0.0021 |
| Beacon: OpenCode + Claude Code | 2,500 / 500 | $0.04 on 1 of 2 events | $0.0035 |

Five submitted rows become four events. The next refresh reads **0 files**. After a malformed snapshot, coverage becomes incomplete and previous totals remain available. These are invented fixture numbers. Recorded amounts cover only events with a recorded cost; neither column establishes a full subscription bill.

## What this demonstrates

- Incremental ingestion: parse changed source snapshots and reuse unchanged history.
- Auditable corrections: retain observations and valuations while rebuilding active totals.
- Data quality: expose failed or stale sources rather than silently presenting complete totals.
- Cost semantics: preserve recorded amounts separately from modeled API-equivalent estimates.
- Optional MCP access: let a trusted assistant query local usage data.

The tool does not measure productivity, model quality, money saved, or orchestration performance. An assistant querying the ledger may see usage from other sessions too; this is not a per-agent access-control system.

## Local data is an explicit choice

Run `python cli.py --help` for the current interface. Invoking the CLI without arguments displays help. Real-source ingestion requires an explicit database path and the local-discovery option. The ledger is written at that path; source databases are opened read-only.

```powershell
# Read your local logs only when you deliberately choose to do so:
python cli.py refresh --db .usage-cache/private.sqlite3 --local --json
# Query the existing ledger without refreshing the source logs:
python cli.py totals --db .usage-cache/private.sqlite3 --project aurora --json
python cli.py status --db .usage-cache/private.sqlite3 --json
```

An explicit normalized snapshot can be imported with `refresh --db PATH --export FILE`; this never enables automatic source discovery. Exit code 1 indicates a runtime problem or incomplete source coverage, and 2 indicates invalid arguments. Missing price information remains visible separately in the status. Even queries may maintain the ledger schema; they are not guaranteed to open the ledger read-only.

`--project` is a case-insensitive substring filter across session directories, assigned project identifiers, and upstream project IDs. It selects whole sessions, so a session used for several projects is not split automatically. A session without usable attribution may not match. `refresh --project NAME` still ingests all selected sources and filters the displayed totals; source-health status remains global. Omitting the filter returns the whole ledger.

Missing optional applications appear under `unavailable_sources`. No discovered inputs, unreadable inputs, malformed snapshots and previously ingested files that disappear remain explicit coverage failures.

Local records can contain session titles, working directories, machine identifiers, and project assignments. Keep the ledger and any output private. Do not commit usage exports, screenshots of real sessions, or generated reports. An MCP client can transmit returned data to its model provider, even though this server does not itself call a provider API. Connect it only to clients you trust with that data.

Source discovery supports OpenCode SQLite, Claude Code JSONL, Codex JSONL, and Cursor CSV/local SQLite through inherited readers. These integrations depend on vendor log formats. This preparation pass verifies synthetic fixtures; it does **not** certify compatibility with current versions of every vendor application.

## Optional MCP server

```powershell
python -m pip install -r requirements-mcp.txt
```

The server uses local stdio. Set `OPENCODE_USAGE_ALLOW_DISCOVERY=1` and `OPENCODE_USAGE_DB` to an explicit private database path in your MCP client's environment, then have it run `python server.py` from this directory. Without that opt-in, data-access tools refuse to ingest logs. Listing tools does not ingest data.

Every enabled MCP data-access tool refreshes the local sources, including `usage_status`. The `usage_query` tool accepts the same project filter. Use the CLI's `totals` command when you want to query an existing ledger without refreshing.

The release excludes OAuth quota probing, account credentials, provider HTTP calls, personal dashboards, export transport, and cross-machine syncing. Optional MCP dependencies have their own licenses and networking capabilities; this statement describes this application's behavior, not a sandbox imposed on those libraries.

## Pricing and limits

`verified_pricing.py` identifies a tariff catalog checked on **6 September 2026** and records its source links. That historical label has not been independently revalidated during release preparation. The check date is not a price effective date. Legacy fallback tariffs have weaker dating/provenance. Neither source guarantees today's price or reconstructs a historical invoice.

The synthetic demo uses an invented model and invented tariff, so it makes no provider-price claim. Unknown or unsupported valuations are reported as incomplete, not a proven zero bill. Subscription fees, credits, negotiated discounts, taxes, and commercial accounting reconciliation are outside scope.

The legacy parser library remains available for compatibility, but the published entry points use the ledger and its quality indicators. Direct library callers are responsible for selecting sources and inspecting diagnostics.

## Files worth reading

| File | Purpose |
|---|---|
| `demo.py` | Isolated example with fictional records |
| `usage_store.py` | Incremental ledger, snapshot and pricing history |
| `cli.py` | Explicit local ingestion and queries |
| `server.py` | Optional MCP query interface |
| `*_reader.py` | Source-format adapters |
| `test_usage_store.py` | Synthetic ingestion, correction and failure regressions |
| `test_verified_pricing.py` | Tariff calculation and valuation-history regressions |
| `test_release.py` | Release entry-point and demo safeguards |
| `DEPENDENCIES.md` | Runtime dependency and license notes |
| `VERIFICATION.md` | Checks actually run and remaining limits |

## License

[MIT](LICENSE). See [LICENSE-DECISION.md](LICENSE-DECISION.md) for origin and dependency notes. Developed with AI assistance; the verification record states what was actually tested.
