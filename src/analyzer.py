"""Build grounded per-lead prompts, call the LLM, and enforce the no-fabrication guardrail."""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

from anthropic import Anthropic

from .data_loader import Document
from .linker import CaseFile, OrphanLead
from .llm_client import DEFAULT_MODEL, analyze_case_file

logger = logging.getLogger(__name__)

TODAY = "2026-07-10"

CRM_FIELD_LABELS = {
    "lead_id": "Lead ID",
    "company": "Company",
    "contact_name": "Contact name",
    "contact_email": "Contact email",
    "source": "Source",
    "service_interest": "Service interest",
    "status": "CRM status",
    "est_value_usd": "Estimated value (USD)",
    "owner": "Owner",
    "created_date": "Created date",
    "last_contact_date": "Last contact date (per CRM)",
    "next_followup_date": "Next follow-up date (per CRM)",
    "notes": "CRM notes field",
}


@dataclass
class LeadAnalysis:
    key: str  # lead_id, or a synthetic key for orphans
    lead_id: str | None
    company: str
    is_orphan: bool
    crm_row: dict | None
    docs: list[Document]
    mismatches: list[dict] = field(default_factory=list)
    missing_info_flags: list[dict] = field(default_factory=list)
    timing_signals: list[dict] = field(default_factory=list)
    open_questions_or_dropped_commitments: list[dict] = field(default_factory=list)
    deprioritize_signals: list[dict] = field(default_factory=list)
    suggested_crm_update: str = ""
    suggested_next_action: str = ""
    competitor_mentioned: dict | None = None
    tool_preference_mentioned: dict | None = None
    dropped_findings: list[dict] = field(default_factory=list)


def _doc_text_for_prompt(doc: Document) -> str:
    lines = [f"--- Document: {doc.filename} ({doc.doc_type}, dated {doc.date}) ---", f"Title: {doc.title}", "", doc.body]
    if doc.ground_truth:
        lines += ["", f"GROUND TRUTH (confirmed fact about what actually happened): {doc.ground_truth}"]
    return "\n".join(lines)


def _crm_block(crm_row: dict) -> str:
    lines = ["CRM RECORD:"]
    for field_key, label in CRM_FIELD_LABELS.items():
        val = crm_row.get(field_key, "")
        val = "" if val is None or (isinstance(val, float) and val != val) else val
        lines.append(f"  {label}: {val if str(val).strip() else '(blank)'}")
    return "\n".join(lines)


def build_prompt(item: CaseFile | OrphanLead) -> str:
    parts = [f"Today's date: {TODAY}", ""]
    # Duck-typed (hasattr), not isinstance(item, CaseFile) - see the matching
    # comment in cache.py's get_or_analyze for why isinstance is unreliable
    # here (st.cache_resource can hold instances from a stale module reload).
    # This is the fresh-analysis path (called from analyze_lead below on a
    # cache miss), so this exact line is what was still crashing after the
    # cache.py/app.py fix - it was the one isinstance check missed.
    if hasattr(item, "lead_id"):
        parts.append(_crm_block(item.crm_row))
    else:
        parts.append(
            f"CRM RECORD: none. This company ('{item.company_guess}') does not currently appear anywhere "
            "in the CRM export — evaluate whether the linked documents show real engagement that should "
            "have been logged."
        )
    parts.append("")
    parts.append(f"LINKED DOCUMENTS ({len(item.docs)}):")
    for doc in item.docs:
        parts.append("")
        parts.append(_doc_text_for_prompt(doc))
    return "\n".join(parts)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()


def _grounded(quote: str, docs: list[Document]) -> bool:
    q = _norm(quote)
    if not q:
        return False
    return any(q in _norm(doc.raw_text) for doc in docs)


ARRAY_FINDING_KEYS = ("mismatches", "missing_info_flags", "timing_signals", "open_questions_or_dropped_commitments", "deprioritize_signals")

# Matches a leaked internal tool-call XML wrapper the model occasionally
# emits as a plain string instead of a real JSON array for one field - e.g.
# '\n<parameter name="mismatches">[{...}, {...}]</parameter>'. This is a rare
# forced-tool-calling serialization artifact, not something our prompt
# controls; the JSON array embedded inside it is usually still genuine,
# grounded content, so it's worth trying to recover rather than discarding.
_PARAM_WRAPPER_RE = re.compile(r'^\s*<parameter\s+name="[^"]*">(.*?)(?:</parameter>\s*)?$', re.DOTALL)


def _recover_array_field(key: str, value) -> list:
    """Returns a usable list for a findings-array field. If `value` is
    already a list, returns it unchanged. If it's a string, attempts to
    extract and parse a JSON array from a <parameter name="..."> wrapper
    artifact (see _PARAM_WRAPPER_RE) before falling back to an empty list.
    Never raises - a recovery failure just means no findings from this
    field, not a crashed lead."""
    if isinstance(value, list):
        return value
    if not isinstance(value, str) or not value.strip():
        return []
    match = _PARAM_WRAPPER_RE.match(value)
    if not match:
        logger.warning("Discarding malformed %s field (expected list, got str with no recognizable wrapper): %r", key, value[:200])
        return []
    try:
        parsed = json.loads(match.group(1))
    except json.JSONDecodeError as e:
        logger.warning("Found a <parameter> wrapper for %s but couldn't parse the JSON inside it (%s): %r", key, e, value[:200])
        return []
    if not isinstance(parsed, list):
        logger.warning("Recovered %s field from a <parameter> wrapper, but it wasn't a JSON array (got %s)", key, type(parsed).__name__)
        return []
    logger.warning("Recovered %d %s finding(s) from a <parameter> wrapper artifact instead of discarding them.", len(parsed), key)
    return parsed


def _filter_findings(raw: dict, docs: list[Document]) -> tuple[dict, list[dict]]:
    # A second forced-tool-calling serialization artifact (alongside the
    # <parameter> XML-string one handled below): the model occasionally
    # wraps its entire tool response in an extra {"parameters": {...}}
    # layer instead of returning fields at the top level. Unwrapped here,
    # once, so every field lookup below just works - this is what was
    # silently making Northwind Logistics/ByteBridge look like genuinely
    # empty leads (raw.get("mismatches") found nothing at the top level,
    # even though the model had generated real, grounded content one level
    # deeper inside "parameters").
    if isinstance(raw, dict) and isinstance(raw.get("parameters"), dict) and not any(k in raw for k in ARRAY_FINDING_KEYS):
        logger.warning("Unwrapping a {'parameters': {...}} response wrapper artifact instead of treating this lead as empty.")
        raw = raw["parameters"]

    clean: dict = {
        "suggested_crm_update": raw.get("suggested_crm_update", ""),
        # Synthesized recommendations, not verbatim quotes - not subject to the
        # substring-grounding check, same as suggested_crm_update above.
        "suggested_next_action": raw.get("suggested_next_action", ""),
    }
    dropped: list[dict] = []
    for key in ARRAY_FINDING_KEYS:
        clean[key] = []
        items = raw.get(key) or []
        if not isinstance(items, list):
            items = _recover_array_field(key, items)
        for finding in items:
            # Defensive: the model is expected to return an object per the
            # tool schema, but forced tool-calling isn't a 100% type
            # guarantee - a malformed item (e.g. a bare string) must not
            # crash the whole lead's analysis. Log and skip it instead.
            if not isinstance(finding, dict):
                logger.warning("Skipping malformed %s item (expected dict, got %s): %r", key, type(finding).__name__, finding)
                continue
            quote = finding.get("evidence_quote", "")
            if _grounded(quote, docs):
                clean[key].append(finding)
            else:
                dropped.append({"category": key, **finding})

    # Single optional objects, not lists - same verbatim-quote grounding rule
    # as every other finding; the model is instructed to omit these keys
    # entirely when nothing qualifies, so a missing key is expected and not
    # an error. competitor_mentioned (a different company/person might do
    # the work) and tool_preference_mentioned (just a named tool/stack
    # preference, informational only) are mutually exclusive by construction
    # in the prompt - see llm_client.py's schema descriptions.
    for raw_key in ("competitor_mentioned", "tool_preference_mentioned"):
        finding = raw.get(raw_key)
        if finding is not None and not isinstance(finding, dict):
            logger.warning("Skipping malformed %s (expected dict, got %s): %r", raw_key, type(finding).__name__, finding)
            finding = None
        if finding and _grounded(finding.get("evidence_quote", ""), docs):
            clean[raw_key] = finding
        else:
            clean[raw_key] = None
            if finding:
                dropped.append({"category": raw_key, **finding})

    return clean, dropped


def analyze_lead(client: Anthropic, item: CaseFile | OrphanLead, model: str | None = None) -> LeadAnalysis:
    # Duck-typed (hasattr) - same reasoning as build_prompt above.
    if hasattr(item, "lead_id"):
        key = item.lead_id
        lead_id = item.lead_id
        company = item.crm_row.get("company", "")
        is_orphan = False
        crm_row = item.crm_row
    else:
        key = "ORPHAN::" + item.company_guess
        lead_id = None
        company = item.company_guess
        is_orphan = True
        crm_row = None

    if not item.docs:
        return LeadAnalysis(key=key, lead_id=lead_id, company=company, is_orphan=is_orphan, crm_row=crm_row, docs=[])

    prompt = build_prompt(item)
    raw = analyze_case_file(client, prompt, model=model or DEFAULT_MODEL)

    # Reliability backstop: a lead with linked documents coming back with
    # every single finding array null/empty is suspicious (observed as
    # genuine API/model non-determinism on a real run - same lead, same
    # prompt, retried moments later and came back fully populated). Not a
    # timeout, not an exception, not a truncated response - the model just
    # occasionally omits real content. One automatic retry before accepting
    # it as a genuine "healthy, nothing to report" lead.
    if not any(raw.get(k) for k in ARRAY_FINDING_KEYS):
        logger.warning(
            "%s: all finding arrays came back null/empty despite %d linked doc(s) - retrying once "
            "(suspected transient API non-determinism, not a code error).",
            company, len(item.docs),
        )
        retry_raw = analyze_case_file(client, prompt, model=model or DEFAULT_MODEL)
        if any(retry_raw.get(k) for k in ARRAY_FINDING_KEYS):
            logger.warning("%s: retry recovered non-empty findings - using the retry result.", company)
            raw = retry_raw
        else:
            logger.warning("%s: retry also came back all-empty - accepting as a genuine no-findings result.", company)

    clean, dropped = _filter_findings(raw, item.docs)

    return LeadAnalysis(
        key=key,
        lead_id=lead_id,
        company=company,
        is_orphan=is_orphan,
        crm_row=crm_row,
        docs=item.docs,
        mismatches=clean["mismatches"],
        missing_info_flags=clean["missing_info_flags"],
        timing_signals=clean["timing_signals"],
        open_questions_or_dropped_commitments=clean["open_questions_or_dropped_commitments"],
        deprioritize_signals=clean["deprioritize_signals"],
        suggested_crm_update=clean.get("suggested_crm_update", ""),
        suggested_next_action=clean.get("suggested_next_action", ""),
        competitor_mentioned=clean.get("competitor_mentioned"),
        tool_preference_mentioned=clean.get("tool_preference_mentioned"),
        dropped_findings=dropped,
    )
