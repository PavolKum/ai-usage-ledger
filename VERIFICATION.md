# Verification record

3 October 2026. Source-only distribution with fictional test inputs.

| Check | Result |
|---|---|
| Python 3.12.14, `-B -S demo.py` | Passed; synthetic expected totals, deduplication, zero rereads and stale-source handling |
| Python 3.12.14, `-B -S -m unittest -q test_usage_store test_verified_pricing test_release` | 67 passed, 1 optional MCP test skipped; site packages disabled |
| Python 3.14, same suite with installed MCP 1.28.1 and Pydantic 2.13.4 | 68 passed, no skips |
| MCP registration and disabled-discovery paths | All six usage tools registered; access refused without both explicit settings |
| Actual MCP stdio client/server round trip | Initialization and tool listing passed; data-access request returned the expected disabled-discovery error |
| Project attribution | Two fictional projects remain separate across assistants; assigned labels match; missing project returns no events and unknown costs |
| Optional source discovery | Missing optional apps do not cause false failure; no inputs, explicit missing inputs, vanished historical inputs and permission errors remain incomplete |
| Clean GitHub-hosted Windows and Linux, Python 3.12 and 3.14 | All four jobs passed core/demo and full MCP test stages at commit `c148386`; [run evidence](https://github.com/PavolKum/ai-usage-ledger/actions/runs/37135547955) |
| Fresh public clone | Offline project demo and core suite passed; 67 passed, 1 optional MCP skip with site packages disabled |
| Source selection | Explicit file allowlist; no original Git history or data/report directories copied |
| Text scan | No credential-like values, personal local paths, email addresses or private machine names found; license decision intentionally names the prospective owner |

The test suite uses temporary synthetic data. Release-entry tests fail on automatic source discovery or application network calls. On Windows, the event loop's internal socket pair is created before those guards; the guard remains active while application tools execute.

The September report of 139 passing tests describes an earlier, broader project snapshot. It is not the count for this curated release.

## Limits

- Clean Windows/Linux runners were verified as recorded above. macOS and Python 3.11 were not tested. The initial Windows CI run exposed short-path alias handling in a test guard; the guard was corrected and the complete matrix passed.
- Live vendor installations/log versions were not exercised, and current vendor pricing was not revalidated.
- The scan is a targeted check, not a guarantee of secret absence or a complete authorship audit.
- The original source is larger than this candidate. Export transport, personal reports and quota/credential functionality are intentionally excluded.
- The owner selected MIT and authorized public publication after the bounded provenance review. No production-readiness, performance or adoption claim is made.
