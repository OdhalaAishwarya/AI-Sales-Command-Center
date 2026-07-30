"""Build grounded per-lead prompts, call the LLM, and enforce the no-fabrication guardrail."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from anthropic import Anthropic

from .data_loader import Document
from .linker import CaseFile, OrphanLead
from .llm_client import DEFAULT_MODEL, analyze_case_file

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
    if isinstance(item, CaseFile):
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


def _filter_findings(raw: dict, docs: list[Document]) -> tuple[dict, list[dict]]:
    clean: dict = {
        "suggested_crm_update": raw.get("suggested_crm_update", ""),
        # Synthesized recommendations, not verbatim quotes - not subject to the
        # substring-grounding check, same as suggested_crm_update above.
        "suggested_next_action": raw.get("suggested_next_action", ""),
    }
    dropped: list[dict] = []
    for key in ("mismatches", "missing_info_flags", "timing_signals", "open_questions_or_dropped_commitments", "deprioritize_signals"):
        clean[key] = []
        for finding in raw.get(key, []) or []:
            quote = finding.get("evidence_quote", "")
            if _grounded(quote, docs):
                clean[key].append(finding)
            else:
                dropped.append({"category": key, **finding})
    return clean, dropped


def analyze_lead(client: Anthropic, item: CaseFile | OrphanLead, model: str | None = None) -> LeadAnalysis:
    if isinstance(item, CaseFile):
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
        dropped_findings=dropped,
    )
