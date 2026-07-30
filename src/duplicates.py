"""Rule-based near-duplicate detection across CRM rows.

Deliberately not LLM-based: "same company/contact entered twice" is a structural
question the data itself answers, and rules are cheaper and more reliable here.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from .linker import normalize_company, normalize_person, _similarity

COMPANY_SIM_THRESHOLD = 0.8
NAME_SIM_THRESHOLD = 0.85


@dataclass
class DuplicatePair:
    lead_id_a: str
    lead_id_b: str
    company_a: str
    company_b: str
    owner_a: str
    owner_b: str
    reasons: list[str] = field(default_factory=list)
    confidence: str = "medium"  # "high" | "medium"


def find_duplicates(crm_df: pd.DataFrame) -> list[DuplicatePair]:
    rows = crm_df.to_dict("records")
    pairs: list[DuplicatePair] = []

    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            a, b = rows[i], rows[j]
            reasons: list[str] = []

            company_a_norm = normalize_company(a["company"])
            company_b_norm = normalize_company(b["company"])
            company_exact = bool(company_a_norm) and company_a_norm == company_b_norm
            company_sim = _similarity(company_a_norm, company_b_norm) if company_a_norm and company_b_norm else 0.0
            company_close = company_exact or company_sim >= COMPANY_SIM_THRESHOLD
            if company_exact:
                reasons.append(f"same normalized company name ({company_a_norm!r})")
            elif company_close:
                reasons.append(f"very similar company names ({a['company']!r} vs {b['company']!r})")

            email_a = (a["contact_email"] or "").strip().lower()
            email_b = (b["contact_email"] or "").strip().lower()
            email_exact = bool(email_a) and email_a == email_b
            if email_exact:
                reasons.append(f"identical contact email ({email_a})")

            name_a_norm = normalize_person(a["contact_name"])
            name_b_norm = normalize_person(b["contact_name"])
            name_close = bool(name_a_norm) and bool(name_b_norm) and (
                name_a_norm == name_b_norm or _similarity(name_a_norm, name_b_norm) >= NAME_SIM_THRESHOLD
            )
            if name_close and name_a_norm != "":
                reasons.append(f"same/very similar contact name ({a['contact_name']!r} vs {b['contact_name']!r})")

            domain_a = email_a.split("@")[-1] if "@" in email_a else ""
            domain_b = email_b.split("@")[-1] if "@" in email_b else ""
            same_domain = bool(domain_a) and domain_a == domain_b

            if not company_close:
                continue  # company must match (exactly or near) for this to be a duplicate

            confidence = None
            if email_exact or (company_exact and name_close):
                confidence = "high"
            elif company_close and (name_close or same_domain):
                confidence = "medium"

            if confidence:
                pairs.append(
                    DuplicatePair(
                        lead_id_a=a["lead_id"],
                        lead_id_b=b["lead_id"],
                        company_a=a["company"],
                        company_b=b["company"],
                        owner_a=a["owner"],
                        owner_b=b["owner"],
                        reasons=reasons,
                        confidence=confidence,
                    )
                )

    return pairs
