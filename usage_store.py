"""Local incremental usage ledger. Source stores are always opened read-only.

Changed files are parsed as snapshots: Codex replay filtering and Cursor's
estimate replacement are not append-only. Unchanged history is never parsed.
Only normalized usage observations (not conversations) are retained here.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import usage
import verified_pricing

TOKENS = tuple(usage.empty_tokens())
DEFAULT_DB = Path(os.environ.get("OPENCODE_USAGE_DB", Path(usage.REPO_DIR) / ".usage-cache" / "usage.sqlite3"))
PRIORITY = {"opencode": 10, "claude-code": 20, "export": 30, "cursor-csv": 40, "cursor-db": 40, "codex": 50}
PARSER_VERSION = "2"
SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS source_files(
 id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL, kind TEXT NOT NULL,
 priority INTEGER NOT NULL, signature TEXT, revision INTEGER NOT NULL DEFAULT 0,
 active INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT 'new',
 error TEXT, checked_at TEXT, success_at TEXT);
CREATE TABLE IF NOT EXISTS observations(
 id INTEGER PRIMARY KEY, file_id INTEGER NOT NULL, revision INTEGER NOT NULL,
 event_key TEXT NOT NULL, ordinal INTEGER NOT NULL, payload TEXT NOT NULL,
 UNIQUE(file_id,revision,event_key));
CREATE INDEX IF NOT EXISTS observations_event ON observations(event_key,file_id,revision);
CREATE INDEX IF NOT EXISTS observations_snapshot ON observations(file_id,revision);
CREATE TABLE IF NOT EXISTS sessions(
 session_key TEXT PRIMARY KEY, machine_id TEXT NOT NULL, source TEXT NOT NULL,
 session_id TEXT NOT NULL, title TEXT, directory TEXT, version TEXT,
 default_project TEXT, project_override TEXT, client_id TEXT, metadata_ts INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS assignment_history(
 id INTEGER PRIMARY KEY, session_key TEXT, project_id TEXT, client_id TEXT,
 reason TEXT NOT NULL, changed_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pricing_versions(
 id INTEGER PRIMARY KEY, model TEXT NOT NULL, provider TEXT NOT NULL,
 currency TEXT NOT NULL CHECK(currency='USD'), tier TEXT NOT NULL,
 effective_from INTEGER, effective_to INTEGER, basis TEXT NOT NULL,
 published INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, provenance TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS price_rates(
 pricing_version INTEGER NOT NULL REFERENCES pricing_versions(id),
 category TEXT NOT NULL, per_token REAL NOT NULL CHECK(per_token>=0),
 PRIMARY KEY(pricing_version,category));
CREATE TABLE IF NOT EXISTS events(
 event_key TEXT PRIMARY KEY, observation_id INTEGER NOT NULL,
 session_key TEXT NOT NULL, ts INTEGER NOT NULL, day_start INTEGER NOT NULL,
 day_end INTEGER NOT NULL, source TEXT NOT NULL, provider TEXT NOT NULL,
 model TEXT NOT NULL, billing_source TEXT NOT NULL, pricing_version INTEGER NOT NULL,
 payload TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS events_day ON events(day_start);
CREATE INDEX IF NOT EXISTS events_time ON events(ts);
CREATE INDEX IF NOT EXISTS events_session ON events(session_key);
CREATE TABLE IF NOT EXISTS valuations(
 id INTEGER PRIMARY KEY, event_key TEXT NOT NULL, observation_id INTEGER NOT NULL,
 pricing_version INTEGER NOT NULL, api_equivalent_cost REAL, implied_cost REAL,
 reason TEXT NOT NULL, evaluated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS daily(
 day_start INTEGER NOT NULL, day_end INTEGER NOT NULL, session_key TEXT NOT NULL,
 source TEXT NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL,
 billing_source TEXT NOT NULL, pricing_version INTEGER NOT NULL,
 input INTEGER, output INTEGER, reasoning INTEGER, cache_read INTEGER, cache_write INTEGER,
 cash_cost REAL, implied_cost REAL, api_equivalent_cost REAL, recorded_cost REAL, total_cost REAL,
 messages INTEGER, estimated_messages INTEGER, unpriced_messages INTEGER,
 recorded_cost_messages INTEGER,
 PRIMARY KEY(day_start,session_key,source,provider,model,billing_source,pricing_version));
"""


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _key(*parts):
    return hashlib.sha256(_json(parts).encode("utf-8")).hexdigest()


def _now():
    return datetime.now().astimezone().isoformat()


def _days(ts):
    date = datetime.fromtimestamp(ts / 1000).replace(hour=0, minute=0, second=0, microsecond=0)
    # time.mktime-backed naive timestamp() rejects pre-epoch local midnights
    # on Windows. Fixed-offset aware arithmetic works for that boundary too.
    def stamp(value):
        try:
            return int(value.timestamp() * 1000)
        except OSError:
            epoch_offset = datetime.fromtimestamp(0) - datetime(1970, 1, 1)
            return int(value.replace(tzinfo=timezone(epoch_offset)).timestamp()*1000)
    return stamp(date), stamp(date + timedelta(days=1))


def _signature(path, kind):
    paths = [path, path + "-wal", path + "-journal"] if kind in {"opencode", "cursor-db"} else [path]
    parts = []
    for index, item in enumerate(paths):
        try:
            st = os.stat(item)
        except FileNotFoundError:
            if index == 0:
                raise
            parts.append(None)
        else:
            parts.append([st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns])
    return _json(parts)


def discover_sources():
    """Return sources and diagnostics; absent optional roots are informational."""
    import claude_code_reader as claude
    import codex_reader as codex
    import cursor_reader as cursor

    found, issues = [], []

    def scan(root, suffix, kind, recursive=False, optional=True):
        root = Path(root)
        try:
            entries = list(os.scandir(root))
        except FileNotFoundError:
            issues.append({"source": kind, "path": str(root), "status": "unavailable" if optional else "error", "error": "Source directory absent"})
            return
        except OSError as exc:
            issues.append({"source": kind, "path": str(root), "status": "error", "error": str(exc)})
            return
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False) and recursive:
                    scan(entry.path, suffix, kind, True, optional=False)
                elif entry.is_file() and entry.name.endswith(suffix):
                    if kind != "export" or Path(entry.name).stem != usage.machine_id():
                        found.append((kind, str(Path(entry.path).resolve())))
            except OSError as exc:
                issues.append({"source": kind, "path": entry.path, "status": "error", "error": str(exc)})

    scan(usage.DATA_DIR, ".db", "opencode")
    scan(Path(claude.CLAUDE_HOME) / "projects", ".jsonl", "claude-code", True)
    scan(usage.EXPORTS_DIR, ".jsonl", "export")
    scan(Path(codex.CODEX_HOME) / "sessions", ".jsonl", "codex", True)
    scan(Path(codex.CODEX_HOME) / "archived_sessions", ".jsonl", "codex")
    explicit = os.environ.get("CURSOR_USAGE_EVENTS_CSV") or os.environ.get("CURSOR_USAGE_CSV")
    try:
        candidates = [explicit] if explicit else []
        if not explicit:
            # glob/Path.exists may hide permission errors as absent sources.
            downloads = Path(os.environ.get("USERPROFILE", os.path.expanduser("~"))) / "Downloads"
            try:
                with os.scandir(downloads) as entries:
                    candidates = [entry.path for entry in entries if entry.name.startswith("usage-events-") and entry.name.endswith(".csv") and entry.is_file()]
                candidates.sort(key=os.path.getmtime, reverse=True)
            except FileNotFoundError:
                issues.append({"source": "cursor-csv", "path": str(downloads), "status": "unavailable", "error": "Optional downloads directory absent"})
        if candidates:
            found.append(("cursor-csv", str(Path(candidates[0]).resolve())))
        else:
            try:
                os.stat(cursor.STATE_VSCDB)
            except FileNotFoundError:
                issues.append({"source": "cursor", "status": "unavailable", "error": "No dashboard CSV or local DB"})
            else:
                found.append(("cursor-db", str(Path(cursor.STATE_VSCDB).resolve())))
    except OSError as exc:
        issues.append({"source": "cursor", "status": "error", "error": str(exc)})
    return sorted(found, key=lambda item: (PRIORITY[item[0]], item[1])), issues


def read_source(kind, path, diagnostics):
    if kind == "opencode":
        return usage.read_messages_from_db(path, strict=True)
    if kind == "claude-code":
        from claude_code_reader import read_claude_code_messages
        return read_claude_code_messages(paths=[path], strict=True, diagnostics=diagnostics)
    if kind == "codex":
        from codex_reader import read_codex_messages
        # Session-index titles are refreshed separately, never once per file.
        return read_codex_messages(paths=[path], titles={}, strict=True, diagnostics=diagnostics)
    if kind == "cursor-csv":
        from cursor_usage_csv_reader import read_cursor_usage_csv_messages
        return read_cursor_usage_csv_messages(csv_path=path, strict=True)
    if kind == "cursor-db":
        from cursor_reader import read_cursor_messages
        return read_cursor_messages(db_path=path, strict=True)
    rows = []
    with open(path, encoding="utf-8") as stream:
        for line_no, line in enumerate(stream, 1):
            if not line.endswith("\n"):
                diagnostics.append("Incomplete trailing line; awaiting completion")
                break
            if not line.strip():
                continue
            row = json.loads(line)
            if "_meta" in row:
                continue
            if "machine_id" not in row or "msg_id" not in row:
                raise ValueError(f"Missing export identity at line {line_no}")
            row["source_line"] = line_no
            rows.append(row)
    return rows


def _hydrate(raw):
    msg = dict(raw)
    msg.setdefault("source", "opencode")
    msg["provider"] = msg.get("provider") or msg.get("providerID") or "unknown"
    msg["model"] = usage.normalize_model(msg.get("model") or "unknown")
    source = msg["source"]
    raw_cost = float(msg.get("cash_cost", msg.get("cost", 0)) or 0)
    msg.setdefault("billing_source", usage.infer_billing_source(source, msg["provider"], raw_cost))
    if source in {"claude-code", "cursor", "codex"} or msg["billing_source"] != "api":
        msg.setdefault("cash_cost", 0.0)
        msg.setdefault("implied_cost", 0.0)
    else:
        msg.setdefault("cash_cost", raw_cost)
        msg.setdefault("implied_cost", raw_cost)
    msg.setdefault("recorded_cost", raw_cost if msg["billing_source"] == "api" and ("cash_cost" in raw or "cost" in raw) else None)
    for field in TOKENS:
        value = msg.get(field, 0) or 0
        if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0 or int(value) != value:
            raise ValueError(f"Invalid token count: {field}")
        msg[field] = int(value)
    for field in ("cash_cost", "implied_cost", "recorded_cost"):
        if msg.get(field) is not None and (not math.isfinite(float(msg[field])) or float(msg[field]) < 0):
            raise ValueError(f"Invalid cost: {field}")
    msg["time"] = usage._msg_ts(msg)
    if msg["time"] <= 0:
        raise ValueError("Usage event has no valid timestamp")
    for field in ("machine_id", "msg_id", "session_id"):
        if not isinstance(msg.get(field), str) or not msg[field]:
            raise ValueError(f"Missing stable identity: {field}")
    for field in ("session_title", "session_dir", "project_id", "opencode_version"):
        msg.setdefault(field, "")
    return msg


class UsageStore:
    def __init__(self, path=DEFAULT_DB):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)
        self.db.execute("BEGIN IMMEDIATE")
        if "metadata_ts" not in {r[1] for r in self.db.execute("PRAGMA table_info(sessions)")}:
            self.db.execute("ALTER TABLE sessions ADD COLUMN metadata_ts INTEGER NOT NULL DEFAULT 0")
        if "total_cost" not in {r[1] for r in self.db.execute("PRAGMA table_info(daily)")}:
            self.db.execute("ALTER TABLE daily ADD COLUMN total_cost REAL")
            self._rebuild_days({r[0] for r in self.db.execute("SELECT DISTINCT day_start FROM events")})
        self.db.execute("INSERT OR IGNORE INTO metadata VALUES('schema_version','1')")
        if self.db.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[0] not in {"1", "2"}:
            raise RuntimeError("Unsupported usage-store schema version")
        if "tariff_details" not in {r[1] for r in self.db.execute("PRAGMA table_info(pricing_versions)")}:
            self.db.execute("ALTER TABLE pricing_versions ADD COLUMN tariff_details TEXT NOT NULL DEFAULT '{}'")
        # Older servers must reconnect rather than misinterpret service-tier rows.
        self.db.execute("UPDATE metadata SET value='2' WHERE key='schema_version'")
        zone = _json([time.tzname, os.environ.get("TZ")])
        previous = self.db.execute("SELECT value FROM metadata WHERE key='timezone'").fetchone()
        if previous and previous[0] != zone:
            raise RuntimeError("Timezone changed; rebuild the local cache to regenerate daily boundaries")
        self.db.execute("INSERT OR IGNORE INTO metadata VALUES('timezone',?)", (zone,))
        self.db.commit()
        self.prices = None
        self.install_verified_prices()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.db.close()

    def _price(self, model, rates, *, provider="*", tier="short", effective_from=None,
               effective_to=None, basis="explicit", published=False, provenance, tariff_details=None):
        if tier not in {"short", "long"}:
            raise ValueError("Tier must be short or long")
        if effective_to is not None and (effective_from is None or effective_to <= effective_from):
            raise ValueError("Effective interval must be increasing and half-open")
        required = {"input", "output", "cache_read", "cache_write"}
        if not required <= set(rates) or set(rates) - required - {"cache_write_5m", "cache_write_1h"}:
            raise ValueError("Provide four pricing categories, optionally both cache TTL rates; output includes reasoning")
        if ("cache_write_5m" in rates) != ("cache_write_1h" in rates):
            raise ValueError("Provide both cache TTL rates")
        if any(not math.isfinite(v) or v < 0 for v in rates.values()):
            raise ValueError("Rates must be finite and nonnegative")
        cursor = self.db.execute(
            "INSERT INTO pricing_versions(model,provider,currency,tier,effective_from,effective_to,basis,published,created_at,provenance) VALUES(?,?,'USD',?,?,?,?,?,?,?)",
            (model, provider, tier, effective_from, effective_to, basis, int(published), _now(), provenance))
        self.db.executemany("INSERT INTO price_rates VALUES(?,?,?)", [(cursor.lastrowid, k, v) for k, v in rates.items()])
        self.db.execute("UPDATE pricing_versions SET tariff_details=? WHERE id=?", (_json(tariff_details or {}), cursor.lastrowid))
        self.prices = None
        return cursor.lastrowid

    def install_verified_prices(self):
        """Append the exact checked catalogue once; retain all previous valuations."""
        key = "pricing_catalog:" + verified_pricing.CATALOG_VERSION
        with self.db:
            # Serialize the existence check with other refreshing processes.
            self.db.execute("BEGIN IMMEDIATE")
            if self.db.execute("SELECT 1 FROM metadata WHERE key=?", (key,)).fetchone():
                return []
            ids = []
            for row in verified_pricing.catalog():
                details = dict(catalog=verified_pricing.CATALOG_VERSION, match_mode="exact",
                               service_tier=row["service_tier"], context_threshold=272000 if row["model"] == verified_pricing.ASTRA else None,
                               checked_at="2026-09-06", sources=verified_pricing.SOURCES[row["model"]])
                ids.append(self._price(row["model"], row["rates"], tier=row["context_tier"], basis="verified-published", published=True,
                                       provenance="Official first-party USD API-equivalent rates checked 2026-09-06. Effective dates unknown; historical use is an estimate. " + " ".join(details["sources"]),
                                       tariff_details=details))
            self.db.execute("INSERT INTO metadata VALUES(?,?)", (key, _json(ids)))
            return ids

    def add_price(self, model, rates, *, effective_from, provenance, provider="*", tier="short", effective_to=None):
        """Append a dated USD rate version. Existing valuations stay unchanged."""
        if effective_from is None or not provenance.strip():
            raise ValueError("New prices require an effective date and provenance")
        with self.db:
            return self._price(model, rates, provider=provider, tier=tier, effective_from=effective_from,
                               effective_to=effective_to, provenance=provenance)

    def _seed_prices(self):
        if self.db.execute("SELECT 1 FROM metadata WHERE key='pricing_seed'").fetchone():
            return
        msgs = [json.loads(r[0]) for r in self.db.execute("SELECT payload FROM events WHERE source='opencode'")]
        derived = usage.derive_rates_from_opencode(msgs, include_published_fallbacks=False)
        fallback = usage.static_fallback_model_rates()
        provenance = "Imported legacy usage.py snapshot. Effective dates unknown; historical use is an estimate, not a historical tariff. Derived rates use the initial local/pooled OpenCode observations."
        for model, rates in {**fallback, **derived}.items():
            self._price(model, rates, basis="legacy-derived" if model in derived else "legacy-static", provenance=provenance)
        for tier in ("short", "long"):
            for model, rates in usage.published_model_rates(tier).items():
                self._price(model, rates, tier=tier, basis="legacy-published", published=True, provenance=provenance)
        self.db.execute("INSERT INTO metadata VALUES('pricing_seed',?)", (_now(),))

    def _resolve_price(self, msg):
        if self.prices is None:
            self.prices = []
            for row in self.db.execute("SELECT * FROM pricing_versions"):
                item = dict(row)
                item["rates"] = dict(self.db.execute("SELECT category,per_token FROM price_rates WHERE pricing_version=?", (row["id"],)))
                item["details"] = json.loads(row["tariff_details"])
                self.prices.append(item)
        candidates = []
        dimensions = verified_pricing.dimensions(msg) if msg["model"] in verified_pricing.MODELS else None
        for price in self.prices:
            if price["provider"] not in {"*", msg["provider"]}:
                continue
            if msg["model"] != price["model"] and not usage._model_prefix_matches(msg["model"], price["model"]):
                continue
            if price["details"].get("match_mode") == "exact" and price["model"] != msg["model"]:
                continue
            if dimensions:
                if dimensions["issue"]:
                    continue
                if price["basis"] not in {"verified-published", "explicit"}:
                    continue
                if price["details"].get("service_tier", "standard") != dimensions["service_tier"]:
                    continue
                if price["basis"] == "verified-published" and price["tier"] != dimensions["context_tier"]:
                    continue
            if price["tier"] == "long" and not usage._uses_long_context(msg):
                continue
            if price["effective_from"] is not None and msg["time"] < price["effective_from"]:
                continue
            if price["effective_to"] is not None and msg["time"] >= price["effective_to"]:
                continue
            rank = (price["basis"] == "explicit", price["basis"] == "verified-published", price["provider"] != "*", price["published"],
                    price["tier"] == "long", len(price["model"]), price["effective_from"] or 0, price["id"])
            candidates.append((rank, price))
        return max(candidates, key=lambda item: item[0])[1] if candidates else None

    def _fill_new_price_coverage(self, stats, days):
        """New metered models can fill NULL estimates with an audited version.

        Existing non-NULL valuations and existing rates are never rewritten by
        new empirical samples. This is observed pricing, not a vendor tariff.
        """
        signature = _json([tuple(r) for r in self.db.execute("SELECT id,revision FROM source_files WHERE kind IN ('opencode','export') AND active=1 ORDER BY id")])
        old = self.db.execute("SELECT value FROM metadata WHERE key='observed_pricing_revision'").fetchone()
        if old and old[0] == signature:
            return
        msgs = [json.loads(r[0]) for r in self.db.execute("SELECT payload FROM events WHERE source='opencode'")]
        rates = usage.derive_rates_from_opencode(msgs, include_published_fallbacks=False)
        added = []
        for model, rate in rates.items():
            if model in verified_pricing.MODELS:
                continue
            probe = {"model": model, "provider": "unknown", "time": int(time.time()*1000)}
            if self._resolve_price(probe) is None:
                price_id = self._price(model, rate, basis="observed-derived", provenance="New metered OpenCode model observed at " + _now() + ". Effective tariff dates unknown. Empirical legacy token-ratio estimate; source observations retained. Evidence revisions: " + signature)
                added.append(price_id)
        count = 0
        if added:
            for row in self.db.execute("SELECT e.event_key,e.observation_id,o.payload FROM events e JOIN observations o ON o.id=e.observation_id WHERE e.pricing_version=0").fetchall():
                raw = json.loads(row["payload"])
                if self._resolve_price(raw):
                    msg = self._value(row["event_key"], row["observation_id"], raw, "filled missing estimate from newly observed metered model; effective dates unknown")
                    days.add(self._put_event(row["event_key"], row["observation_id"], msg))
                    count += 1
        stats["new_price_versions"] = added
        stats["newly_priced_events"] = count
        self.db.execute("INSERT OR REPLACE INTO metadata VALUES('observed_pricing_revision',?)", (signature,))

    def _value(self, key, observation_id, raw, reason):
        msg = _hydrate(raw)
        price = self._resolve_price(msg)
        estimate = None
        msg["pricing_assumptions"] = []
        msg["pricing_issue"] = None
        if price:
            rates = price["rates"]
            dimensions = verified_pricing.dimensions(msg) if price["basis"] == "verified-published" else {}
            try:
                estimate, assumptions = verified_pricing.cost_at_rates(msg, rates, dimensions.get("multiplier", 1))
                msg["pricing_assumptions"] = dimensions.get("assumptions", []) + assumptions
            except ValueError as exc:
                msg["pricing_issue"] = str(exc)
                price = None
        elif msg["model"] in verified_pricing.MODELS:
            msg["pricing_issue"] = verified_pricing.dimensions(msg)["issue"]
        msg["api_equivalent_cost"] = estimate
        msg["pricing_version"] = price["id"] if price else 0
        msg["pricing_status"] = price["basis"] if price else "missing"
        floor = msg.get("implied_cost", 0) or 0
        if msg["billing_source"] in {"subscription", "cursor", "codex", "ghcp"}:
            msg["implied_cost"] = max(floor, estimate or 0)
            msg["cost_imputed"] = (estimate or 0) > floor
        else:
            msg["implied_cost"] = msg.get("cash_cost", 0) or 0
            msg["cost_imputed"] = False
        msg["cost"] = msg["implied_cost"]
        msg["total_cost"] = msg["cash_cost"] if msg["cash_cost"] > 0 else msg["implied_cost"]
        msg["charging_basis"] = msg["billing_source"]
        self.db.execute("INSERT INTO valuations(event_key,observation_id,pricing_version,api_equivalent_cost,implied_cost,reason,evaluated_at) VALUES(?,?,?,?,?,?,?)",
                        (key, observation_id, msg["pricing_version"], estimate, msg["implied_cost"], reason, _now()))
        return msg

    def _put_event(self, key, observation_id, msg):
        session_key = _key(msg["machine_id"], msg["source"], msg["session_id"])
        day, end = _days(msg["time"])
        directory = msg.get("session_dir") or ""
        project = _key(msg["machine_id"], "cwd", os.path.normcase(os.path.normpath(directory))) if directory else None
        upstream = msg.get("project_id")
        if not project and upstream and upstream not in {"codex", "cursor", "claude-code"}:
            project = _key(msg["machine_id"], "project", upstream)
        self.db.execute("""INSERT INTO sessions(session_key,machine_id,source,session_id,title,directory,version,default_project,metadata_ts)
            VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(session_key) DO UPDATE SET
            title=excluded.title,directory=excluded.directory,version=excluded.version,default_project=excluded.default_project,metadata_ts=excluded.metadata_ts
            WHERE excluded.metadata_ts>=sessions.metadata_ts""",
            (session_key, msg["machine_id"], msg["source"], msg["session_id"], msg.get("session_title"), directory, msg.get("opencode_version"), project, msg["time"]))
        self.db.execute("INSERT OR REPLACE INTO events VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (key, observation_id, session_key, msg["time"], day, end, msg["source"], msg["provider"], msg["model"], msg["billing_source"], msg.get("pricing_version", 0), _json(msg)))
        return day

    def refresh(self, sources=None, *, force=False):
        """Serialize refreshes. Failed snapshots keep their last valid contribution."""
        started = time.perf_counter()
        live_sources = sources is None
        if live_sources:
            sources, diagnostics = discover_sources()
            unavailable = [item for item in diagnostics if item.get("status") == "unavailable"]
            issues = [item for item in diagnostics if item.get("status") != "unavailable"]
            if not sources:
                issues.append({"source": "discovery", "status": "no_sources", "error": "No local usage source files found; totals do not establish coverage"})
        else:
            sources, issues, unavailable = list(sources), [], []
        sources = [(kind, str(Path(path).resolve())) for kind, path in sources]
        stats = {"files_seen": len(sources), "files_read": 0, "files_unchanged": 0, "bytes_read": 0, "issues": issues, "unavailable_sources": unavailable}
        affected, days = set(), set()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            version = self.db.execute("SELECT value FROM metadata WHERE key='parser_version'").fetchone()
            if not version or version[0] != PARSER_VERSION:
                force = True
            old_files = {r["path"]: dict(r) for r in self.db.execute("SELECT * FROM source_files")}
            successful_kinds = set()
            successful_paths = set()
            for kind, path in sources:
                self.db.execute("INSERT OR IGNORE INTO source_files(path,kind,priority) VALUES(?,?,?)", (path, kind, PRIORITY[kind]))
                file = self.db.execute("SELECT * FROM source_files WHERE path=?", (path,)).fetchone()
                try:
                    signature = _signature(path, kind)
                    if not force and signature == file["signature"] and file["active"]:
                        if file["status"] in {"missing", "error"}:
                            # Reparse on recovery: a matching stat tuple alone is
                            # not evidence that a prior read failure has cleared.
                            signature = None
                        else:
                            stats["files_unchanged"] += 1
                            successful_kinds.add(kind)
                            successful_paths.add(path)
                            continue
                    signature = _signature(path, kind)
                    diagnostics = []
                    rows = read_source(kind, path, diagnostics)
                    hydrated = [_hydrate(row) for row in rows]
                    if _signature(path, kind) != signature:
                        raise RuntimeError("Source changed while reading; retry on next refresh")
                    stats["files_read"] += 1
                    stats["bytes_read"] += os.stat(path).st_size
                    revision = file["revision"] + 1
                    affected.update(r[0] for r in self.db.execute("SELECT event_key FROM observations WHERE file_id=? AND revision=?", (file["id"], file["revision"])))
                    observations = []
                    for ordinal, msg in enumerate(hydrated):
                        key = _key(msg["machine_id"], msg["source"], msg["msg_id"])
                        observations.append((file["id"], revision, key, ordinal, _json(msg)))
                        affected.add(key)
                    self.db.executemany("INSERT OR IGNORE INTO observations(file_id,revision,event_key,ordinal,payload) VALUES(?,?,?,?,?)", observations)
                    self.db.execute("UPDATE source_files SET signature=?,revision=?,active=1,status=?,error=?,checked_at=?,success_at=? WHERE id=?",
                                    (signature, revision, "partial" if diagnostics else "ok", "; ".join(diagnostics) or None, _now(), _now(), file["id"]))
                    successful_kinds.add(kind)
                    successful_paths.add(path)
                except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError, sqlite3.Error, RuntimeError) as exc:
                    self.db.execute("UPDATE source_files SET status='error',error=?,checked_at=? WHERE id=?", (str(exc), _now(), file["id"]))

            present = {p for _, p in sources}
            for path, old in old_files.items():
                if path in present or not old["active"]:
                    continue
                # A successful Cursor snapshot replaces its predecessor. Other
                # vanished files retain evidence (rotation/archival is not deletion).
                cursor_replaced = old["kind"].startswith("cursor-") and any(k.startswith("cursor-") for k in successful_kinds)
                rotated = any(kind == old["kind"] and Path(new_path).name == Path(path).name and new_path in successful_paths for kind, new_path in sources)
                if cursor_replaced or rotated:
                    affected.update(r[0] for r in self.db.execute("SELECT event_key FROM observations WHERE file_id=? AND revision=?", (old["id"], old["revision"])))
                    self.db.execute("UPDATE source_files SET active=0,status='superseded',error=NULL WHERE id=?", (old["id"],))
                else:
                    self.db.execute("UPDATE source_files SET status='missing',error='Source file absent; retaining last good observations' WHERE id=?", (old["id"],))

            seeded = bool(self.db.execute("SELECT 1 FROM metadata WHERE key='pricing_seed'").fetchone())
            for key in affected:
                previous = self.db.execute("SELECT * FROM events WHERE event_key=?", (key,)).fetchone()
                candidate = self.db.execute("""SELECT o.id,o.payload FROM observations o JOIN source_files f ON f.id=o.file_id
                    WHERE o.event_key=? AND o.revision=f.revision AND f.active=1
                    ORDER BY f.priority,f.path,o.ordinal LIMIT 1""", (key,)).fetchone()
                if previous:
                    days.add(previous["day_start"])
                if not candidate:
                    self.db.execute("DELETE FROM events WHERE event_key=?", (key,))
                    continue
                # A changed file often contains unchanged events. Preserve their
                # valuation instead of silently applying newly added prices.
                if previous:
                    old_payload = self.db.execute("SELECT payload FROM observations WHERE id=?", (previous["observation_id"],)).fetchone()[0]
                    if old_payload == candidate["payload"]:
                        self.db.execute("UPDATE events SET observation_id=? WHERE event_key=?", (candidate["id"], key))
                        continue
                    old_raw, new_raw = json.loads(old_payload), json.loads(candidate["payload"])
                    price_inputs = [*TOKENS, "time", "model", "provider", "billing_source", "cash_cost", "implied_cost", "recorded_cost"]
                    same_dimensions = True
                    if new_raw.get("model") in verified_pricing.MODELS:
                        old_quote, new_quote = verified_pricing.quote(old_raw), verified_pricing.quote(new_raw)
                        same_dimensions = all(old_quote.get(field) == new_quote.get(field) for field in ("cost", "service_tier", "context_tier", "multiplier", "issue"))
                    if same_dimensions and all(old_raw.get(field) == new_raw.get(field) for field in price_inputs):
                        valued = json.loads(previous["payload"])
                        for field in ("cost", "total_cost", "implied_cost", "cost_imputed", "api_equivalent_cost", "pricing_version", "pricing_status", "charging_basis"):
                            new_raw[field] = valued[field]
                        for field in ("pricing_assumptions", "pricing_issue"):
                            if field in valued:
                                new_raw[field] = valued[field]
                        days.add(self._put_event(key, candidate["id"], new_raw))
                        continue
                raw = json.loads(candidate["payload"])
                msg = self._value(key, candidate["id"], raw, "source correction" if previous else "ingestion") if seeded else raw
                days.add(self._put_event(key, candidate["id"], msg))
            if not seeded:
                self._seed_prices()
                for row in self.db.execute("SELECT event_key,observation_id,payload FROM events").fetchall():
                    msg = self._value(row["event_key"], row["observation_id"], json.loads(row["payload"]), "initial legacy pricing snapshot")
                    days.add(self._put_event(row["event_key"], row["observation_id"], msg))
            self._fill_new_price_coverage(stats, days)
            self._rebuild_days(days)
            if live_sources:
                self._refresh_titles(stats)
            stats["affected_days"] = len(days)
            stats["seconds"] = round(time.perf_counter() - started, 4)
            self.db.execute("INSERT OR REPLACE INTO metadata VALUES('last_refresh',?)", (_json(stats),))
            self.db.execute("INSERT OR REPLACE INTO metadata VALUES('parser_version',?)", (PARSER_VERSION,))
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        return self.status()

    def _rebuild_days(self, days):
        expressions = [f"sum(json_extract(payload,'$.{k}'))" for k in TOKENS]
        expressions += ["sum(json_extract(payload,'$.cash_cost'))", "sum(json_extract(payload,'$.implied_cost'))",
                        "sum(json_extract(payload,'$.api_equivalent_cost'))", "sum(json_extract(payload,'$.recorded_cost'))", "sum(json_extract(payload,'$.total_cost'))", "count(*)",
                        "sum(CASE WHEN json_extract(payload,'$.token_estimated') THEN coalesce(json_extract(payload,'$.message_count'),1) ELSE 0 END)",
                        "sum(pricing_version=0)", "count(json_extract(payload,'$.recorded_cost'))"]
        for day in days:
            self.db.execute("DELETE FROM daily WHERE day_start=?", (day,))
            columns = "day_start,day_end,session_key,source,provider,model,billing_source,pricing_version,input,output,reasoning,cache_read,cache_write,cash_cost,implied_cost,api_equivalent_cost,recorded_cost,total_cost,messages,estimated_messages,unpriced_messages,recorded_cost_messages"
            self.db.execute("INSERT INTO daily(" + columns + ") SELECT day_start,day_end,session_key,source,provider,model,billing_source,pricing_version," + ",".join(expressions) +
                            " FROM events WHERE day_start=? GROUP BY day_start,day_end,session_key,source,provider,model,billing_source,pricing_version", (day,))

    def _refresh_titles(self, stats):
        from codex_reader import CODEX_HOME
        path = str(Path(CODEX_HOME) / "session_index.jsonl")
        try:
            signature = _signature(path, "index")
            old = self.db.execute("SELECT value FROM metadata WHERE key='title_signature'").fetchone()
            if old and old[0] == signature:
                titles = self.db.execute("SELECT value FROM metadata WHERE key='title_snapshot'").fetchone()
                if titles:
                    self.db.executemany("UPDATE sessions SET title=? WHERE machine_id=? AND source='codex' AND session_id=?",
                                        [(title, usage.machine_id(), "codex:" + sid) for sid, title in json.loads(titles[0]).items()])
                return
            titles = {}
            with open(path, encoding="utf-8") as stream:
                for line in stream:
                    if not line.endswith("\n"):
                        stats["issues"].append({"source": "codex-index", "status": "partial", "error": "Incomplete title index line"})
                        return
                    if line.strip():
                        row = json.loads(line)
                        if row.get("id") and row.get("thread_name"):
                            titles[row["id"]] = str(row["thread_name"])[:80]
                            self.db.execute("UPDATE sessions SET title=? WHERE machine_id=? AND source='codex' AND session_id=?",
                                            (str(row["thread_name"])[:80], usage.machine_id(), "codex:" + row["id"]))
            if _signature(path, "index") == signature:
                self.db.execute("INSERT OR REPLACE INTO metadata VALUES('title_signature',?)", (signature,))
                self.db.execute("INSERT OR REPLACE INTO metadata VALUES('title_snapshot',?)", (_json(titles),))
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError) as exc:
            stats["issues"].append({"source": "codex-index", "status": "error", "error": str(exc)})

    def reprice(self, *, start=0, end=2**62, reason, models=None):
        """Explicit repricing is auditable and never alters recorded costs."""
        if not reason.strip():
            raise ValueError("Repricing requires a reason")
        days = set()
        if models is not None and not models:
            raise ValueError("Model filter must not be empty")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.prices = None
            sql = "SELECT e.event_key,e.observation_id,o.payload FROM events e JOIN observations o ON o.id=e.observation_id WHERE ts>=? AND ts<?"
            parameters = [start, end]
            if models is not None:
                sql += " AND e.model IN (" + ",".join("?" for _ in models) + ")"
                parameters += list(models)
            rows = self.db.execute(sql, parameters).fetchall()
            for row in rows:
                msg = self._value(row["event_key"], row["observation_id"], json.loads(row["payload"]), reason)
                days.add(self._put_event(row["event_key"], row["observation_id"], msg))
            self._rebuild_days(days)
        return len(rows)

    def assign_session(self, session_key, *, project_id=None, client_id=None, reason):
        if not reason.strip():
            raise ValueError("Assignment requires a reason")
        with self.db:
            cursor = self.db.execute("UPDATE sessions SET project_override=?,client_id=? WHERE session_key=?", (project_id, client_id, session_key))
            if not cursor.rowcount:
                raise KeyError(session_key)
            self.db.execute("INSERT INTO assignment_history(session_key,project_id,client_id,reason,changed_at) VALUES(?,?,?,?,?)", (session_key, project_id, client_id, reason, _now()))

    def status(self):
        row = self.db.execute("SELECT value FROM metadata WHERE key='last_refresh'").fetchone()
        result = json.loads(row[0]) if row else {}
        result["sources"] = [dict(r) for r in self.db.execute("SELECT kind,status,count(*) AS files FROM source_files WHERE active=1 GROUP BY kind,status")]
        result["source_failures"] = [dict(r) for r in self.db.execute("SELECT kind,path,status,error,success_at FROM source_files WHERE active=1 AND status!='ok'")]
        result["unpriced"] = [dict(r) for r in self.db.execute("SELECT model,sum(messages) AS messages FROM daily WHERE pricing_version=0 GROUP BY model")]
        result["complete"] = not result.get("issues") and not result["source_failures"]
        result["pricing_complete"] = not result["unpriced"]
        result["pricing_note"] = "Effective dates are unknown for legacy and checked catalogues. Exact Fable 5.1/Astra use verified rates; service/region defaults are API-equivalent assumptions. Estimates are not bills; missing estimates remain NULL."
        return result

    def messages(self, *, start=0, end=2**62):
        return [json.loads(row[0]) for row in self.db.execute("SELECT payload FROM events WHERE ts>=? AND ts<? ORDER BY ts,event_key", (start, end))]

    def aggregate(self, *, group=None, start=0, end=2**62, model=None, project=None, client=None, limit=20):
        """Full days use precomputed facts; only partial boundary days use events."""
        if group not in {None, "source", "model", "session", "provider", "billing_source"}:
            raise ValueError("Unsupported grouping")
        fields = list(TOKENS) + ["cash_cost", "implied_cost", "api_equivalent_cost", "recorded_cost", "total_cost", "messages", "estimated_messages", "unpriced_messages", "recorded_cost_messages"]
        dims = "session_key,source,provider,model,billing_source"
        event_fields = [f"json_extract(payload,'$.{k}') AS {k}" for k in fields[:10]] + ["1 AS messages", "CASE WHEN json_extract(payload,'$.token_estimated') THEN coalesce(json_extract(payload,'$.message_count'),1) ELSE 0 END AS estimated_messages", "(pricing_version=0) AS unpriced_messages", "(json_extract(payload,'$.recorded_cost') IS NOT NULL) AS recorded_cost_messages"]
        boundary_days = [_days(start)[0], _days(end - 1)[0] if 0 < end < 253402214400000 else -1]
        sql = "WITH facts AS (SELECT " + dims + "," + ",".join(fields) + " FROM daily WHERE day_start>=? AND day_end<=? UNION ALL SELECT " + dims + "," + ",".join(event_fields) + " FROM events WHERE day_start IN (?,?) AND ts>=? AND ts<? AND (day_start<? OR day_end>?)) SELECT f.*,s.session_id,s.title,s.directory,s.version,coalesce(s.project_override,s.default_project) AS project_key,s.client_id FROM facts f JOIN sessions s USING(session_key) WHERE 1=1"
        args = [start, end, *boundary_days, start, end, start, end]
        if model:
            sql += " AND instr(lower(f.model),lower(?))>0"
            args.append(model)
        if project:
            sql += " AND (instr(lower(coalesce(s.project_override,s.default_project,'')),lower(?))>0 OR instr(lower(coalesce(s.directory,'')),lower(?))>0 OR EXISTS(SELECT 1 FROM events e WHERE e.session_key=s.session_key AND instr(lower(coalesce(json_extract(e.payload,'$.project_id'),'')),lower(?))>0))"
            args.extend([project, project, project])
        if client:
            sql += " AND s.client_id=?"
            args.append(client)
        result = {}
        for row in self.db.execute(sql, args):
            key = row["session_key"] if group == "session" else row[group] if group else "total"
            if key not in result:
                bucket = {**usage.empty_bucket(), "api_equivalent_cost": 0.0, "recorded_cost": 0.0,
                          "unpriced_messages": 0, "recorded_cost_messages": 0, "total_cost": 0.0}
                if group == "source":
                    bucket["estimated_messages"] = 0
                if group == "session":
                    bucket.update({"session_key": key, "session_id": row["session_id"], "title": row["title"], "directory": row["directory"], "version": row["version"], "project_id": row["project_key"], "client_id": row["client_id"]})
                result[key] = bucket
            bucket = result[key]
            for field in TOKENS:
                bucket["tokens"][field] += row[field] or 0
            for field in fields[5:]:
                if field == "estimated_messages" and group != "source":
                    continue
                bucket[field] += row[field] or 0
            bucket["sessions"].add(row["session_key"])
        if not result and group is None:
            result["total"] = {**usage.empty_bucket(), "api_equivalent_cost": None, "recorded_cost": None, "unpriced_messages": 0, "recorded_cost_messages": 0, "total_cost": 0.0}
        for bucket in result.values():
            bucket["sessions"] = len(bucket["sessions"])
            bucket["cost"] = bucket["implied_cost"]
            if bucket["unpriced_messages"]:
                bucket["api_equivalent_cost"] = None
            if not bucket["recorded_cost_messages"]:
                bucket["recorded_cost"] = None
            if group == "session":
                del bucket["sessions"]
        if group == "session":
            return sorted(result.values(), key=lambda b: -b["cost"])[:limit]
        if group is None:
            return result["total"]
        return dict(sorted(result.items(), key=lambda item: -item[1]["cost"]))


def main():
    # Keep the legacy entry point behind the same explicit-source safeguards.
    from cli import main as cli_main
    return cli_main()


if __name__ == "__main__":
    raise SystemExit(main())
