"""Cache LLM analysis results to disk so Streamlit reruns don't re-hit the API.

The cache never touches the source data files - it only stores this app's own
derived JSON output, keyed by a hash of the lead + its linked document text.
"""
from __future__ import annotations

import hashlib
import json

from anthropic import Anthropic

from .analyzer import LeadAnalysis, analyze_lead
from .data_loader import BASE_DIR
from .linker import CaseFile, OrphanLead

PROMPT_VERSION = "v5"  # bumped: competitor_mentioned now excludes tool/stack preferences (split into tool_preference_mentioned)
CACHE_PATH = BASE_DIR / ".cache" / "analysis_cache.json"

# Part 3 (memory trail): a separate, compact, bounded-size history of findings
# across runs - kept apart from CACHE_PATH above so that file's shape/size for
# cost-avoidance caching is untouched. Each entry is a full run's worth of
# lightweight finding fingerprints (not full findings), and only the most
# recent MAX_HISTORY_RUNS are kept, so this file never grows unbounded no
# matter how many times the app gets run.
HISTORY_PATH = BASE_DIR / ".cache" / "findings_history.json"
MAX_HISTORY_RUNS = 10

# V2 (Executive Summary, section 2): a tiny separate cache for the one
# per-run synthesis call, keyed by a hash of that run's own result signature
# (see app.py) so an unchanged set of results reuses the same summary
# instead of paying for a new call - kept apart from CACHE_PATH above since
# it's a different kind of artifact (one string, not a per-lead analysis).
INSIGHTS_CACHE_PATH = BASE_DIR / ".cache" / "insights_cache.json"


def _hash_key(item: CaseFile | OrphanLead, key: str) -> str:
    parts = [PROMPT_VERSION, key] + sorted(f"{d.filename}:{d.raw_text}" for d in item.docs)
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def load_cache() -> dict:
    if not CACHE_PATH.exists():
        return {}
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_cache(cache: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache, indent=2), encoding="utf-8")


def _to_cacheable(analysis: LeadAnalysis) -> dict:
    return {
        "key": analysis.key,
        "lead_id": analysis.lead_id,
        "company": analysis.company,
        "is_orphan": analysis.is_orphan,
        "mismatches": analysis.mismatches,
        "missing_info_flags": analysis.missing_info_flags,
        "timing_signals": analysis.timing_signals,
        "open_questions_or_dropped_commitments": analysis.open_questions_or_dropped_commitments,
        "deprioritize_signals": analysis.deprioritize_signals,
        "suggested_crm_update": analysis.suggested_crm_update,
        "suggested_next_action": analysis.suggested_next_action,
        "competitor_mentioned": analysis.competitor_mentioned,
        "tool_preference_mentioned": analysis.tool_preference_mentioned,
        "dropped_findings": analysis.dropped_findings,
    }


def _from_cacheable(data: dict, item: CaseFile | OrphanLead, crm_row: dict | None) -> LeadAnalysis:
    return LeadAnalysis(
        key=data["key"],
        lead_id=data["lead_id"],
        company=data["company"],
        is_orphan=data["is_orphan"],
        crm_row=crm_row,
        docs=item.docs,
        mismatches=data["mismatches"],
        missing_info_flags=data["missing_info_flags"],
        timing_signals=data["timing_signals"],
        open_questions_or_dropped_commitments=data["open_questions_or_dropped_commitments"],
        deprioritize_signals=data.get("deprioritize_signals", []),
        suggested_crm_update=data["suggested_crm_update"],
        suggested_next_action=data.get("suggested_next_action", ""),
        competitor_mentioned=data.get("competitor_mentioned"),
        tool_preference_mentioned=data.get("tool_preference_mentioned"),
        dropped_findings=data["dropped_findings"],
    )


def get_or_analyze(
    client: Anthropic,
    item: CaseFile | OrphanLead,
    model: str | None = None,
    force: bool = False,
    cache: dict | None = None,
) -> tuple[LeadAnalysis, bool]:
    """Returns (analysis, from_cache) - from_cache tells the caller whether this
    actually hit the API or was served from disk, so a caller can report how many
    of a run's items were real (rate-limit-relevant) calls vs. free cache hits.
    """
    # Duck-typed (hasattr), not isinstance(item, CaseFile): st.cache_resource in
    # app.py's load_everything() can hold CaseFile/OrphanLead instances across a
    # redeploy's in-process module reload, leaving them as a different CaseFile
    # class object than the one just re-imported here - isinstance then wrongly
    # returns False for a real CaseFile, and item.company_guess (OrphanLead-only)
    # raises AttributeError. hasattr sidesteps class identity entirely. The
    # actual key/crm_row/is_orphan VALUES produced are unchanged - only the
    # branch-selection mechanism is more robust.
    key = item.lead_id if hasattr(item, "lead_id") else "ORPHAN::" + item.company_guess
    crm_row = item.crm_row if hasattr(item, "lead_id") else None

    if not item.docs:
        empty = LeadAnalysis(key=key, lead_id=item.lead_id if hasattr(item, "lead_id") else None,
                              company=(crm_row or {}).get("company", getattr(item, "company_guess", "")),
                              is_orphan=not hasattr(item, "lead_id"), crm_row=crm_row, docs=[])
        return empty, True

    owns_cache = cache is None
    cache = load_cache() if owns_cache else cache
    cache_key = _hash_key(item, key)

    if not force and cache_key in cache:
        result = _from_cacheable(cache[cache_key], item, crm_row)
        from_cache = True
    else:
        result = analyze_lead(client, item, model=model)
        cache[cache_key] = _to_cacheable(result)
        if owns_cache:
            save_cache(cache)
        from_cache = False

    return result, from_cache


def clear_cache() -> None:
    if CACHE_PATH.exists():
        CACHE_PATH.unlink()


def load_history() -> list[dict]:
    """Returns the list of past run-snapshots, oldest first."""
    if not HISTORY_PATH.exists():
        return []
    try:
        return json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def load_insights_cache() -> dict:
    if not INSIGHTS_CACHE_PATH.exists():
        return {}
    try:
        return json.loads(INSIGHTS_CACHE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_insights_cache(cache: dict) -> None:
    INSIGHTS_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    INSIGHTS_CACHE_PATH.write_text(json.dumps(cache, indent=2), encoding="utf-8")


def append_history_snapshot(timestamp: str, findings: list[dict]) -> list[dict]:
    """Appends one compact run-snapshot and trims to the last MAX_HISTORY_RUNS.

    `findings` should already be the compact form - {fp, company, category,
    summary} per finding - not full finding payloads (evidence/source aren't
    needed to say "this is new/resolved/persisting", which keeps this file's
    size bounded regardless of how many times the app is run).
    """
    history = load_history()
    history.append({"timestamp": timestamp, "findings": findings})
    history = history[-MAX_HISTORY_RUNS:]
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    HISTORY_PATH.write_text(json.dumps(history, indent=2), encoding="utf-8")
    return history
