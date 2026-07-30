"""Link CRM leads to the email/meeting-note documents that talk about them.

Matching signals, strongest first:
  1. Explicit lead_id mention in the doc text (e.g. "L-1032").
  2. Contact email match (doc participants vs crm contact_email).
  3. Normalized company-name similarity.
  4. Contact-name similarity.

Docs that match zero CRM leads are "orphans" — companies mentioned in emails/notes
that never made it into the CRM at all.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

import pandas as pd

from .data_loader import Document

_LEAD_ID_RE = re.compile(r"\bL-10\d\d\b")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

_SUFFIXES = {
    "inc", "llc", "ltd", "limited", "pvt", "private", "group", "solutions",
    "technologies", "tech", "labs", "laboratories", "industries", "capital",
    "hospitality", "foods", "healthcare", "health", "systems", "corp",
    "corporation", "company", "co", "sa", "plc", "holdings", "bank",
    "insurance", "clinics", "advisory", "agency", "nbfc",
}

_ATLIQ_DOMAIN = "atliq.com"


def normalize_company(name: str) -> str:
    name = name.lower()
    name = re.sub(r"[^a-z0-9\s]", " ", name)
    tokens = [t for t in name.split() if t not in _SUFFIXES]
    return " ".join(tokens).strip()


def normalize_person(name: str) -> str:
    name = name.lower()
    name = re.sub(r"^(dr|mr|mrs|ms)\.?\s+", "", name)
    name = re.sub(r"[^a-z\s]", " ", name)
    return " ".join(name.split())


def _similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _doc_emails(doc: Document) -> set[str]:
    emails = set()
    for p in doc.participants:
        emails.update(m.group(0).lower() for m in _EMAIL_RE.finditer(p))
    emails.update(m.group(0).lower() for m in _EMAIL_RE.finditer(doc.raw_text))
    return {e for e in emails if not e.endswith("@" + _ATLIQ_DOMAIN)}


def _doc_lead_ids(doc: Document) -> set[str]:
    return set(_LEAD_ID_RE.findall(doc.raw_text))


# Title prefixes that mean "look at the other side of the dash for the company".
_LEFT_TRY_RIGHT = {"meeting notes", "notes", "call", "email", "email thread"}
# Title prefixes that mean "this doc has no single company subject" (internal-only
# meetings, or a conference debrief spanning many companies).
_LEFT_NO_COMPANY = {"internal", "techsummit mumbai"}


def _leading_capitalized_phrase(text: str) -> str:
    """Grab the leading run of Title-Case-ish tokens (a likely proper-noun phrase)."""
    tokens = text.strip().split()
    kept = []
    for tok in tokens:
        stripped = tok.strip(".,?!()’'\"")
        if stripped == "&":
            kept.append("&")
            continue
        if not stripped or not stripped[0].isupper():
            break
        kept.append(stripped)
    return " ".join(kept).strip()


def guess_subject_company(doc: Document) -> str | None:
    """Best-effort guess at the single company a doc's title is about, or None
    if the title doesn't clearly name one (internal meetings, multi-company debriefs).
    """
    title = re.sub(r"^Email(\s+Thread)?:\s*", "", doc.title).strip()
    parts = re.split(r"\s+[—–-]\s+", title, maxsplit=1)
    left = parts[0].strip()
    right = parts[1].strip() if len(parts) > 1 else ""

    if left.lower() in _LEFT_NO_COMPANY:
        return None
    if left.lower() not in _LEFT_TRY_RIGHT:
        candidate = _leading_capitalized_phrase(left)
        if candidate:
            return candidate
    if right:
        first_segment = right.split(",")[0]
        candidate = _leading_capitalized_phrase(first_segment)
        if candidate:
            return candidate
    return None


def _company_similar(a_norm: str, b_norm: str) -> bool:
    if not a_norm or not b_norm:
        return False
    if a_norm in b_norm or b_norm in a_norm:
        return True
    a_tokens, b_tokens = set(a_norm.split()), set(b_norm.split())
    if a_tokens & b_tokens:
        return True
    return _similarity(a_norm, b_norm) > 0.75


@dataclass
class CaseFile:
    lead_id: str
    crm_row: dict
    docs: list[Document] = field(default_factory=list)
    match_reasons: dict[str, str] = field(default_factory=dict)  # filename -> reason


@dataclass
class OrphanLead:
    """A company that appears in emails/notes but has no CRM row."""

    company_guess: str
    docs: list[Document] = field(default_factory=list)


def build_case_files(crm_df: pd.DataFrame, docs: list[Document]) -> tuple[list[CaseFile], list[OrphanLead]]:
    case_files = {row["lead_id"]: CaseFile(lead_id=row["lead_id"], crm_row=row.to_dict()) for _, row in crm_df.iterrows()}

    # Precompute normalized CRM fields for matching.
    crm_norm = []
    for _, row in crm_df.iterrows():
        crm_norm.append(
            {
                "lead_id": row["lead_id"],
                "company_norm": normalize_company(row["company"]),
                "contact_name_norm": normalize_person(row["contact_name"]),
                "contact_email": (row["contact_email"] or "").lower(),
            }
        )

    for doc in docs:
        subject_guess = guess_subject_company(doc)
        subject_guess_norm = normalize_company(subject_guess) if subject_guess else ""

        doc_matches: dict[str, str] = {}  # lead_id -> reason

        # 1. Explicit lead_id mention — strongest signal, decisive on its own.
        for lid in _doc_lead_ids(doc) & case_files.keys():
            doc_matches[lid] = "explicit lead_id mention"

        doc_emails = _doc_emails(doc)
        doc_text_norm = normalize_company(doc.title + " " + doc.body[:2000])

        for c in crm_norm:
            if c["lead_id"] in doc_matches:
                continue
            # 2. Contact email match.
            if c["contact_email"] and c["contact_email"] in doc_emails:
                doc_matches[c["lead_id"]] = "contact email match"
                continue
            # 3. Normalized company-name similarity (weak signal — a company can be
            # mentioned in passing without the doc being about them). If the title
            # clearly names a *different* company, don't attach this doc to a lead
            # it only shares an incidental body mention with.
            company_hit = False
            if c["company_norm"] and len(c["company_norm"]) >= 3:
                if c["company_norm"] in doc_text_norm or _similarity(c["company_norm"], doc_text_norm[: len(c["company_norm"]) + 20]) > 0.8:
                    company_hit = True
                else:
                    tokens = c["company_norm"].split()
                    if tokens and all(t in doc_text_norm for t in tokens if len(t) > 2):
                        company_hit = True
            if company_hit:
                if subject_guess_norm and not _company_similar(subject_guess_norm, c["company_norm"]):
                    continue  # title points elsewhere; treat as incidental mention
                doc_matches[c["lead_id"]] = "company name match"
                continue
            # 4. Contact-name similarity.
            if c["contact_name_norm"] and _similarity(c["contact_name_norm"], normalize_person(" ".join(doc.participants))) > 0.85:
                doc_matches[c["lead_id"]] = "contact name match"

        for lid, reason in doc_matches.items():
            case_files[lid].docs.append(doc)
            case_files[lid].match_reasons[doc.filename] = reason

        # Orphan detection is independent of the case-file matching above: if the
        # doc's title clearly names a company that doesn't correspond to any CRM
        # record, it's a lead AtliQ never entered — regardless of whether the doc
        # *also* got attached to an unrelated CRM lead via a contact/body mention.
        if subject_guess_norm and not any(_company_similar(subject_guess_norm, c["company_norm"]) for c in crm_norm):
            doc._orphan_key = subject_guess_norm  # type: ignore[attr-defined]
            doc._orphan_guess = subject_guess  # type: ignore[attr-defined]

    orphans: dict[str, OrphanLead] = {}
    for doc in docs:
        key = getattr(doc, "_orphan_key", None)
        if not key:
            continue
        if key not in orphans:
            orphans[key] = OrphanLead(company_guess=getattr(doc, "_orphan_guess"))
        orphans[key].docs.append(doc)

    return list(case_files.values()), list(orphans.values())
