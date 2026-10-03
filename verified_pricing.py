"""Exact first-party USD API-equivalent tariffs checked 2026-09-06.

The check date is NOT an effective date. Historical valuations using this
catalogue are estimates at these rates, not reconstructed invoices.
"""
import math

CATALOG_VERSION = "verified-2026-09-06-v1"
FABLE = "anthropic/claude-fable-5.1"
ASTRA = "openai/gpt-6-astra"
MODELS = (FABLE, ASTRA)
SOURCES = {
    FABLE: ["https://platform.claude.com/docs/en/models/fable-5-1/overview",
            "https://platform.claude.com/docs/en/about-claude/pricing"],
    ASTRA: ["https://developers.openai.com/api/docs/models/gpt-6-astra",
            "https://developers.openai.com/api/docs/pricing"],
}


def protects(model):
    """Unknown descendants must not inherit this model's or an ancestor's price."""
    return any(model == key or model.startswith(key + "-") or model.startswith(key + ".") for key in MODELS)


def catalog():
    rows = []
    for model in MODELS:
        services = {"standard": 1, "batch": .5}
        if model == ASTRA:
            services.update(flex=.5, fast=2)
        for service, factor in services.items():
            for context in (["short", "long"] if model == ASTRA else ["short"]):
                if model == FABLE:
                    rates = dict(input=10, output=50, cache_read=.25, cache_write=12.5, cache_write_5m=12.5, cache_write_1h=20)
                elif context == "long":
                    rates = dict(input=20, output=75, cache_read=2, cache_write=25)
                else:
                    rates = dict(input=10, output=50, cache_read=1, cache_write=12.5)
                rows.append(dict(model=model, service_tier=service, context_tier=context,
                                 rates={key: value * factor / 1e6 for key, value in rates.items()}))
    return rows


def dimensions(msg):
    for key in ("original_usage", "pricing_metadata"):
        if msg.get(key) is not None and not isinstance(msg[key], dict):
            return dict(issue="Invalid " + key + " metadata", assumptions=[])
    original = msg.get("original_usage") or {}
    metadata = msg.get("pricing_metadata") or {}
    def value(key):
        return msg.get(key) or metadata.get(key) or original.get(key)

    model = msg["model"]
    assumptions = ["Current checked tariff; historical effective dates unknown"]
    for key in ("service_tier", "speed", "inference_geo", "processing_region"):
        for source in (msg, metadata, original):
            if source.get(key) is not None and not isinstance(source[key], str):
                return dict(issue="Invalid " + key + " metadata", assumptions=assumptions)
    service = value("service_tier")
    speed = value("speed")
    if speed == "fast":
        if service not in (None, "standard", "default", "fast", "priority"):
            return dict(issue="Conflicting service tier and speed", assumptions=assumptions)
        service = "fast"
    elif speed not in (None, "standard"):
        return dict(issue="Unsupported speed metadata", assumptions=assumptions)
    if service in (None, "default", "auto"):
        assumptions.append("Standard API-equivalent service assumed; actual tariff unrecorded")
        service = "standard"
    if model == ASTRA and service == "priority":
        service = "fast"
    supported = {"standard", "batch", "flex", "fast"} if model == ASTRA else {"standard", "batch"}
    issue = None if service in supported else "Unsupported service tier: " + str(service)
    multiplier = 1.0
    geo = value("inference_geo")
    region = value("processing_region")
    if model == FABLE:
        if geo == "us":
            multiplier = 1.1
        elif geo in (None, "not_available"):
            assumptions.append("Global first-party API-equivalent geography assumed")
        elif geo != "global":
            issue = "Unsupported inference geography: " + str(geo)
    else:
        # Storage residency alone does not establish regional processing.
        if region in ("us", "eu"):
            multiplier = 1.1
            if region == "eu" and service == "fast":
                issue = "Astra fast mode is unavailable with EU regional processing"
        elif region in (None, "global", "not_available"):
            if region != "global":
                assumptions.append("Global API-equivalent processing assumed")
        else:
            issue = "Unsupported processing region: " + str(region)
    prompt = sum(msg.get(k, 0) or 0 for k in ("input", "cache_read", "cache_write"))
    return dict(service_tier=service, context_tier="long" if model == ASTRA and prompt > 272000 else "short",
                multiplier=multiplier, assumptions=assumptions, issue=issue)


def cost_at_rates(msg, rates, multiplier=1.0):
    """Return cost and assumptions; reject malformed or inconsistent TTL splits."""
    assumptions = []
    total_write = msg.get("cache_write", 0) or 0
    write_cost = total_write * rates["cache_write"]
    if "cache_write_1h" in rates:
        original = msg.get("original_usage") or {}
        split = original.get("cache_creation", {})
        if split is None:
            split = {}
        if not isinstance(split, dict):
            raise ValueError("Invalid cache creation breakdown")
        five = split.get("ephemeral_5m_input_tokens", msg.get("cache_write_5m", 0)) or 0
        hour = split.get("ephemeral_1h_input_tokens", msg.get("cache_write_1h", 0)) or 0
        for value in (five, hour):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0 or int(value) != value:
                raise ValueError("Invalid cache TTL token count")
        if five + hour > total_write:
            raise ValueError("Cache TTL split exceeds total cache writes")
        unspecified = total_write - five - hour
        if unspecified:
            assumptions.append("Unspecified cache-write TTL valued at the 5-minute rate")
        write_cost = (five + unspecified) * rates["cache_write_5m"] + hour * rates["cache_write_1h"]
    cost = ((msg.get("input", 0) or 0) * rates["input"]
            + ((msg.get("output", 0) or 0) + (msg.get("reasoning", 0) or 0)) * rates["output"]
            + (msg.get("cache_read", 0) or 0) * rates["cache_read"] + write_cost) * multiplier
    return cost, assumptions


def quote(msg):
    result = dict(cost=None, rates=None, assumptions=[], issue="No exact verified model")
    if msg["model"] not in MODELS:
        return result
    result.update(dimensions(msg))
    if result["issue"]:
        return result
    row = next(row for row in catalog() if row["model"] == msg["model"] and row["service_tier"] == result["service_tier"] and row["context_tier"] == result["context_tier"])
    result["rates"] = row["rates"]
    try:
        result["cost"], assumptions = cost_at_rates(msg, row["rates"], result["multiplier"])
        result["assumptions"] += assumptions
    except ValueError as exc:
        result["issue"] = str(exc)
    return result
