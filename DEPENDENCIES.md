# Dependency inventory

Prepared 3 October 2026. No third-party dependency source or binary is vendored in this candidate.

| Component | Use | License source |
|---|---|---|
| Python standard library | Core ledger, adapters, CLI, demo and unit tests | [Python license](https://docs.python.org/3/license.html) |
| MCP Python SDK 1.28.1 | Optional local stdio server | [Upstream MIT license](https://github.com/modelcontextprotocol/python-sdk/blob/main/LICENSE) |
| Pydantic 2.13.4 | Optional server input schemas | [Upstream MIT license](https://github.com/pydantic/pydantic/blob/main/LICENSE) |

These are direct dependencies only. The requirements file pins the two direct MCP dependencies to versions available on the verification workstation; it is not a transitive lockfile. The source-only core avoids those dependencies. An installer resolves the optional transitive dependencies separately; review the resolved distribution before publishing a bundled executable or container. Existing dependency notices continue to apply.

No dependency-license finding establishes the ownership of the application code. See LICENSE-DECISION.md for the separate project decision.
