"""Urgency scoring: turns raw findings into a ranked Weekly Attention list.

Ranking = deal size + staleness + explicit timing signals + severity of findings,
not alphabetical order or CRM status.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .analyzer import LeadAnalysis
from .duplicates import DuplicatePair

TODAY = date(2026, 7, 10)

_SEVERITY_SCORE = {"high": 3.0, "medium": 1.5, "low": 0.5}
_URGENCY_SCORE = {"due_now": 3.0, "due_this_month": 1.5, "later": 0.0, "unclear": 0.0}


@dataclass
class AttentionItem:
    key: str
    lead_id: str | None
    company: str
    score: float
    urgency_level: str  # "red" | "yellow" | "green"
    reasons: list[str] = field(default_factory=list)
    # Same reasons as above, but structured (category + point contribution) and
    # sorted highest-points-first, so a UI can show "the one biggest reason"
    # plus a "show more" list without re-deriving anything from raw findings.
    # category is one of "mismatch"/"timing"/"open_question"/"missing_info"/
    # "duplicate", or None for the deal-size/staleness context modifiers.
    reason_details: list[dict] = field(default_factory=list)
    is_orphan: bool = False
    is_duplicate: bool = False


def _parse_date(value: str) -> date | None:
    if not value:
        return None
    try:
        y, m, d = (int(x) for x in value.split("-"))
        return date(y, m, d)
    except (ValueError, TypeError):
        return None


def _deal_size_score(crm_row: dict | None) -> tuple[float, str | None]:
    if not crm_row:
        return 0.0, None
    val = crm_row.get("est_value_usd")
    if val is None or val != val:  # NaN check
        return 0.0, None
    val = float(val)
    if val >= 50000:
        return 2.0, f"large deal (${val:,.0f})"
    if val >= 20000:
        return 1.0, f"mid-size deal (${val:,.0f})"
    return 0.3, None


def _staleness_score(crm_row: dict | None) -> tuple[float, str | None]:
    if not crm_row:
        return 0.0, None
    last_contact = _parse_date(crm_row.get("last_contact_date", ""))
    if not last_contact:
        return 0.0, None
    days = (TODAY - last_contact).days
    if days > 90:
        return 1.5, f"{days} days since last CRM contact"
    if days > 45:
        return 0.75, f"{days} days since last CRM contact"
    return 0.0, None


def score_lead(analysis: LeadAnalysis, duplicate_lead_ids: set[str]) -> AttentionItem:
    reason_items: list[dict] = []  # {"category": str|None, "text": str, "points": float}
    signal_score = 0.0

    for m in analysis.mismatches:
        pts = _SEVERITY_SCORE.get(m.get("severity", "low"), 0.5)
        signal_score += pts
        reason_items.append({"category": "mismatch", "text": f"Mismatch: {m['summary']}", "points": pts})

    for t in analysis.timing_signals:
        pts = _URGENCY_SCORE.get(t.get("urgency", "unclear"), 0.0)
        signal_score += pts
        if pts > 0:
            reason_items.append({"category": "timing", "text": f"Timing: {t['summary']}", "points": pts})

    if analysis.open_questions_or_dropped_commitments:
        pts = 1.5 * min(len(analysis.open_questions_or_dropped_commitments), 2)
        signal_score += pts
        reason_items.append({
            "category": "open_question",
            "text": f"{len(analysis.open_questions_or_dropped_commitments)} open question(s)/dropped commitment(s)",
            "points": pts,
        })

    if analysis.missing_info_flags:
        pts = 2.0 if analysis.is_orphan else 1.0
        signal_score += pts
        reason_items.append({
            "category": "missing_info",
            "text": f"{len(analysis.missing_info_flags)} missing-info flag(s)",
            "points": pts,
        })

    is_duplicate = bool(analysis.lead_id) and analysis.lead_id in duplicate_lead_ids
    if is_duplicate:
        signal_score += 1.0
        reason_items.append({"category": "duplicate", "text": "Likely duplicate CRM record", "points": 1.0})

    deal_score, deal_reason = _deal_size_score(analysis.crm_row)
    stale_score, stale_reason = _staleness_score(analysis.crm_row)
    if deal_reason and signal_score > 0:
        reason_items.append({"category": None, "text": deal_reason, "points": deal_score})
    if stale_reason and signal_score > 0:
        reason_items.append({"category": None, "text": stale_reason, "points": stale_score})

    total = signal_score + (deal_score + stale_score if signal_score > 0 else 0.0)

    if total >= 6:
        level = "red"
    elif total >= 2.5:
        level = "yellow"
    else:
        level = "green"

    reason_items.sort(key=lambda r: r["points"], reverse=True)

    return AttentionItem(
        key=analysis.key,
        lead_id=analysis.lead_id,
        company=analysis.company,
        score=round(total, 2),
        urgency_level=level,
        reasons=[r["text"] for r in reason_items],
        reason_details=reason_items,
        is_orphan=analysis.is_orphan,
        is_duplicate=is_duplicate,
    )


_DECISION_MAKER_KEYWORDS = (
    "ceo", "cto", "cfo", "coo", "chief", "president", "founder", "co-founder",
    "vp", "vice president", "director", "head of", "managing director", "owner",
)


def evidence_completeness(analysis: LeadAnalysis) -> dict:
    """V2 section 3: an honest, checkable "how much do we actually know about
    this lead" score (0-100) - NOT a fabricated AI confidence value. Built
    entirely from signals already on hand: linked-document count, whether key
    CRM fields are populated, and how recent the last contact is. Orphan
    leads (no CRM row) skip the CRM-field points, so their ceiling is lower -
    that's an honest reflection of having less structured data, not a bug.
    """
    points = 0
    reasons: list[str] = []

    n_docs = len(analysis.docs)
    doc_points = min(n_docs * 15, 40)
    points += doc_points
    if n_docs == 0:
        reasons.append("No linked documents")
    elif n_docs == 1:
        reasons.append("Only 1 linked document")
    else:
        reasons.append(f"{n_docs} linked documents")

    crm_row = analysis.crm_row
    if crm_row is not None:
        for field_key, label, field_points in (
            ("contact_email", "contact email", 15),
            ("next_followup_date", "next_followup_date", 15),
            ("est_value_usd", "estimated deal value", 10),
        ):
            val = crm_row.get(field_key)
            has_val = val is not None and str(val).strip() != "" and val == val  # last check excludes NaN
            if has_val:
                points += field_points
            else:
                reasons.append(f"Missing: {label}")

        last_contact = _parse_date(crm_row.get("last_contact_date", ""))
        if last_contact:
            days = (TODAY - last_contact).days
            if days <= 30:
                points += 20
                reasons.append(f"Last contact {days} day(s) ago")
            elif days <= 90:
                points += 10
                reasons.append(f"No contact in {days} days")
            else:
                reasons.append(f"No contact in {days} days")
        else:
            reasons.append("No last-contact date on record")
    else:
        reasons.append("No CRM record for this lead")

    score = min(points, 100)
    if score >= 70:
        level = "success"
    elif score >= 40:
        level = "warning"
    else:
        level = "danger"

    return {"score": score, "level": level, "reasons": reasons}


def potential_impact(item: AttentionItem, crm_row: dict | None) -> str:
    """V2 section 6: High/Medium/Low, derived from deal value + the existing
    overall urgency score + staleness - all already computed, no new signal
    invented and no change to how the urgency score itself is calculated."""
    deal_score, _ = _deal_size_score(crm_row)
    stale_score, _ = _staleness_score(crm_row)
    impact_points = deal_score + stale_score + (item.score * 0.3)
    if impact_points >= 3.5:
        return "High"
    if impact_points >= 1.5:
        return "Medium"
    return "Low"


def is_hidden_opportunity(item: AttentionItem, analysis: LeadAnalysis) -> bool:
    """V2 section 8: medium (not high) urgency, no active mismatch/problem,
    but a real signal of long-term value - a confirmed budget (deal value on
    record) or a decision-maker directly present in the conversation. Purely
    a heuristic over existing findings/CRM data/document text - no AI call."""
    if item.urgency_level != "yellow":
        return False
    if analysis.mismatches:
        return False

    crm_row = analysis.crm_row or {}
    val = crm_row.get("est_value_usd")
    has_confirmed_budget = val is not None and val == val and float(val) >= 20000

    decision_maker_present = False
    for doc in analysis.docs:
        haystack = " ".join(doc.participants).lower() + " " + doc.raw_text.lower()
        if any(kw in haystack for kw in _DECISION_MAKER_KEYWORDS):
            decision_maker_present = True
            break

    return has_confirmed_budget or decision_maker_present


def is_high_risk(crm_row: dict | None) -> bool:
    """V2 section 9 metric: "very stale + high value" - reuses the exact same
    thresholds already used by _deal_size_score's "large deal" bucket (>=$50k)
    and _staleness_score's ">90 days" bucket, just combined into one flag."""
    if not crm_row:
        return False
    val = crm_row.get("est_value_usd")
    if val is None or val != val or float(val) < 50000:
        return False
    last_contact = _parse_date(crm_row.get("last_contact_date", ""))
    if not last_contact:
        return False
    return (TODAY - last_contact).days > 90


def rank_attention(analyses: list[LeadAnalysis], duplicate_pairs: list[DuplicatePair], top_n: int = 10) -> list[AttentionItem]:
    duplicate_lead_ids: set[str] = set()
    for p in duplicate_pairs:
        duplicate_lead_ids.add(p.lead_id_a)
        duplicate_lead_ids.add(p.lead_id_b)

    items = [score_lead(a, duplicate_lead_ids) for a in analyses]
    items = [i for i in items if i.score > 0]
    items.sort(key=lambda i: i.score, reverse=True)
    return items[:top_n]
