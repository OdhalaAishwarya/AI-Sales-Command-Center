"""Read-only loaders for the CRM export and the email/meeting-note markdown files.

Nothing in this module ever opens a file in write mode.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

BASE_DIR = Path(__file__).resolve().parent.parent
CRM_PATH = BASE_DIR / "crm_export.csv"
EMAILS_DIR = BASE_DIR / "emails"
NOTES_DIR = BASE_DIR / "meeting_notes"

# Trailing italic parenthetical ground-truth line, e.g. "*(No reply as of 2026-07-10.)*"
_GROUND_TRUTH_RE = re.compile(r"^\*\((.+)\)\*$", re.DOTALL)
_HEADER_RE = re.compile(r"^\*\*(From|To|Date|Subject):\*\*\s*(.*)$")


@dataclass
class EmailMessage:
    from_: str
    to: str
    date: str
    body: str


@dataclass
class Document:
    """A single email thread or meeting note, normalized for downstream use."""

    doc_type: str  # "email" | "note"
    filename: str
    title: str
    date: str  # from the filename, YYYY-MM-DD
    participants: list[str]  # raw From/To strings (emails) — empty for notes
    messages: list[EmailMessage]  # populated for emails, empty for notes
    body: str  # full raw body text (notes) or reconstructed thread text (emails)
    ground_truth: str | None  # trailing italic fact line, if present
    raw_text: str  # the entire file content, used for the LLM prompt + quote verification


def load_crm() -> pd.DataFrame:
    df = pd.read_csv(CRM_PATH, dtype=str).fillna("")
    for col in ("est_value_usd",):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def _extract_ground_truth(text: str) -> tuple[str, str | None]:
    """Split off a trailing italic parenthetical ground-truth line, if present.

    Returns (remaining_text, ground_truth_or_None).
    """
    lines = text.rstrip().splitlines()
    # Walk backwards past blank lines to find the last non-blank line.
    idx = len(lines) - 1
    while idx >= 0 and not lines[idx].strip():
        idx -= 1
    if idx < 0:
        return text, None
    candidate = lines[idx].strip()
    m = _GROUND_TRUTH_RE.match(candidate)
    if not m:
        return text, None
    ground_truth = m.group(1).strip()
    remaining = "\n".join(lines[:idx]).rstrip()
    return remaining, ground_truth


def _parse_email(path: Path) -> Document:
    raw_text = path.read_text(encoding="utf-8")
    body_wo_gt, ground_truth = _extract_ground_truth(raw_text)

    title_match = re.match(r"^#\s*(.+)$", body_wo_gt.splitlines()[0])
    title = title_match.group(1).strip() if title_match else path.stem

    # Messages are separated by a line containing only "---".
    chunks = re.split(r"\n---\n", body_wo_gt)
    messages: list[EmailMessage] = []
    participants: list[str] = []
    for chunk in chunks:
        fields = {"From": "", "To": "", "Date": "", "Subject": ""}
        body_lines = []
        in_body = False
        for line in chunk.splitlines():
            hm = _HEADER_RE.match(line.strip())
            if hm and not in_body:
                fields[hm.group(1)] = hm.group(2).strip()
                continue
            if line.startswith("# "):
                continue
            if not line.strip() and not body_lines:
                in_body = True
                continue
            in_body = True
            body_lines.append(line)
        msg_body = "\n".join(body_lines).strip()
        if fields["From"] or msg_body:
            messages.append(
                EmailMessage(
                    from_=fields["From"],
                    to=fields["To"],
                    date=fields["Date"],
                    body=msg_body,
                )
            )
            if fields["From"]:
                participants.append(fields["From"])
            if fields["To"]:
                participants.append(fields["To"])

    date = messages[0].date if messages else path.stem[:10]

    return Document(
        doc_type="email",
        filename=path.name,
        title=title,
        date=date,
        participants=participants,
        messages=messages,
        body=body_wo_gt,
        ground_truth=ground_truth,
        raw_text=raw_text,
    )


def _parse_note(path: Path) -> Document:
    raw_text = path.read_text(encoding="utf-8")
    body_wo_gt, ground_truth = _extract_ground_truth(raw_text)

    lines = body_wo_gt.splitlines()
    title_match = re.match(r"^#\s*(.+)$", lines[0]) if lines else None
    title = title_match.group(1).strip() if title_match else path.stem

    date_match = re.match(r"^(\d{4}-\d{2}-\d{2})", path.stem)
    date = date_match.group(1) if date_match else ""

    return Document(
        doc_type="note",
        filename=path.name,
        title=title,
        date=date,
        participants=[],
        messages=[],
        body=body_wo_gt,
        ground_truth=ground_truth,
        raw_text=raw_text,
    )


def load_emails() -> list[Document]:
    return [_parse_email(p) for p in sorted(EMAILS_DIR.glob("*.md"))]


def load_notes() -> list[Document]:
    return [_parse_note(p) for p in sorted(NOTES_DIR.glob("*.md"))]


def load_all_docs() -> list[Document]:
    return load_emails() + load_notes()
