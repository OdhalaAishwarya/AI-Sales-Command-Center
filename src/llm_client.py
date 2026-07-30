"""Thin wrapper around the Anthropic API for structured, grounded analysis calls.

Uses a forced tool-call so the model's output is always valid JSON matching our
schema, instead of parsing JSON out of free-form text.
"""
from __future__ import annotations

import os
import random
import time

from anthropic import Anthropic, RateLimitError
from dotenv import load_dotenv

from .data_loader import BASE_DIR

# Load explicitly from the app's own folder - don't depend on the process's cwd
# matching wherever `streamlit run` happened to be launched from.
load_dotenv(dotenv_path=BASE_DIR / ".env")

DEFAULT_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")

# Retry policy for 429s - a burst of leads with large linked-document prompts
# can trip a tokens-per-minute limit even on a paid plan.
MAX_RATE_LIMIT_RETRIES = 5
BASE_BACKOFF_SECONDS = 3.0
MAX_BACKOFF_SECONDS = 45.0

FINDINGS_TOOL = {
    "name": "record_findings",
    "description": "Record findings from comparing a CRM lead record against its linked emails and meeting notes.",
    "input_schema": {
        "type": "object",
        "properties": {
            "mismatches": {
                "type": "array",
                "description": "Places where the CRM's status/activity contradicts what the emails or notes show.",
                "items": {
                    "type": "object",
                    "properties": {
                        "summary": {"type": "string", "description": "One-sentence plain-language description of the contradiction."},
                        "crm_says": {"type": "string", "description": "The relevant CRM field/value being contradicted."},
                        "evidence_quote": {"type": "string", "description": "A verbatim quote (exact substring) from the source document."},
                        "source_file": {"type": "string", "description": "Filename the quote came from."},
                        "severity": {"type": "string", "enum": ["high", "medium", "low"]},
                    },
                    "required": ["summary", "crm_says", "evidence_quote", "source_file", "severity"],
                },
            },
            "missing_info_flags": {
                "type": "array",
                "description": "CRM records missing a critical field (contact_email, next_followup_date) where the docs show real engagement, so follow-up is blocked by a data gap.",
                "items": {
                    "type": "object",
                    "properties": {
                        "summary": {"type": "string"},
                        "missing_field": {"type": "string"},
                        "evidence_quote": {"type": "string"},
                        "source_file": {"type": "string"},
                    },
                    "required": ["summary", "missing_field", "evidence_quote", "source_file"],
                },
            },
            "timing_signals": {
                "type": "array",
                "description": "Soft timing language from the docs, converted into an urgency bucket relative to 2026-07-10.",
                "items": {
                    "type": "object",
                    "properties": {
                        "summary": {"type": "string"},
                        "evidence_quote": {"type": "string"},
                        "source_file": {"type": "string"},
                        "urgency": {"type": "string", "enum": ["due_now", "due_this_month", "later", "unclear"]},
                        "estimated_date_or_window": {"type": "string", "description": "Best-effort concrete date or window implied, e.g. '2026-07 (first week)'."},
                    },
                    "required": ["summary", "evidence_quote", "source_file", "urgency", "estimated_date_or_window"],
                },
            },
            "open_questions_or_dropped_commitments": {
                "type": "array",
                "description": "Direct client questions or AtliQ promises that don't appear to have been followed up on.",
                "items": {
                    "type": "object",
                    "properties": {
                        "summary": {"type": "string"},
                        "kind": {"type": "string", "enum": ["unanswered_question", "dropped_commitment"]},
                        "evidence_quote": {"type": "string"},
                        "source_file": {"type": "string"},
                    },
                    "required": ["summary", "kind", "evidence_quote", "source_file"],
                },
            },
            "deprioritize_signals": {
                "type": "array",
                "description": "Signs this lead may be a poor fit or going cold - NOT an urgency signal, a different kind of flag entirely. Only report a pattern like: repeated pricing pushback with no resolution, an explicit mention that the client chose a competitor, or the client going silent specifically after an objection or a 'not right now'. This is a suggestion for a human to consider, never an automatic action.",
                "items": {
                    "type": "object",
                    "properties": {
                        "summary": {"type": "string"},
                        "signal_type": {"type": "string", "enum": ["pricing_pushback", "competitor_chosen", "went_silent_after_objection"]},
                        "evidence_quote": {"type": "string", "description": "A verbatim quote (exact substring) from the source document."},
                        "source_file": {"type": "string"},
                    },
                    "required": ["summary", "signal_type", "evidence_quote", "source_file"],
                },
            },
            "suggested_crm_update": {
                "type": "string",
                "description": "Optional one-line suggested manual CRM update (e.g. 'Set next_followup_date to 2026-07-14'). Empty string if none.",
            },
            "suggested_next_action": {
                "type": "string",
                "description": "Only if this lead's overall situation looks urgent/time-sensitive: one concrete, specific next step in plain "
                "language (e.g. 'Send a two-line check-in referencing their board decision on July 14'). It must reference a real "
                "fact, date, name, or commitment already present in the documents above - not generic advice. Empty string if the "
                "lead isn't urgent or there's nothing concrete to suggest. This is a draft suggestion for a human only - it must "
                "never imply anything gets sent automatically.",
            },
        },
        "required": [
            "mismatches", "missing_info_flags", "timing_signals", "open_questions_or_dropped_commitments",
            "deprioritize_signals", "suggested_crm_update", "suggested_next_action",
        ],
    },
}

SYSTEM_PROMPT = """You are a sales-ops analyst comparing a CRM record for AtliQ Technologies against \
the emails and meeting notes linked to that lead. Today's date is 2026-07-10.

Rules:
- Only report something if it is directly supported by the provided CRM fields and document text. Never invent, \
assume, or infer beyond what's written.
- Every finding's evidence_quote MUST be an exact, verbatim substring copied from the provided document text \
(including a note's own trailing ground-truth line, which is fact). Do not paraphrase inside the quote field.
- Lines in the documents introduced as ground truth (marked "GROUND TRUTH:") are confirmed fact - weigh them heavily.
- If there is nothing noteworthy for a category, return an empty array for it. Don't force findings.
- Be conservative: a healthy, unremarkable lead should produce few or no findings.
- deprioritize_signals is a distinct, non-urgent flag - do not report it just because a deal is slow or quiet; it \
needs a real pattern (repeated unresolved pricing pushback, an explicit competitor choice, or silence specifically \
following an objection/"not right now").
- suggested_next_action does not need a verbatim evidence_quote (it's a synthesized recommendation, not a quote), \
but it must still reference something concrete and real from the documents, not generic advice.
- Never suggest or imply that any file should be edited or sent automatically - suggested_crm_update and \
suggested_next_action are both just notes for a human to review."""


def get_client(api_key: str | None = None) -> Anthropic:
    key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError(
            "No Anthropic API key found. Set ANTHROPIC_API_KEY in a .env file, or enter it in the sidebar."
        )
    return Anthropic(api_key=key)


def _rate_limit_wait_seconds(attempt: int, error: RateLimitError) -> float:
    """Prefer the server's own Retry-After header; fall back to exponential backoff."""
    retry_after = None
    response = getattr(error, "response", None)
    if response is not None:
        header = response.headers.get("retry-after")
        if header is not None:
            try:
                retry_after = float(header)
            except ValueError:
                retry_after = None
    if retry_after is not None:
        return retry_after + random.uniform(0, 0.5)
    return min(BASE_BACKOFF_SECONDS * (2**attempt), MAX_BACKOFF_SECONDS) + random.uniform(0, 1)


def analyze_case_file(client: Anthropic, prompt: str, model: str = DEFAULT_MODEL) -> dict:
    """Call Claude with the findings tool forced, and return the parsed JSON input.

    Retries on 429 (rate limit) with backoff that respects the server's Retry-After
    header when present; any other error is raised immediately.
    """
    for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
        try:
            response = client.messages.create(
                model=model,
                max_tokens=4096,
                system=SYSTEM_PROMPT,
                tools=[FINDINGS_TOOL],
                tool_choice={"type": "tool", "name": "record_findings"},
                messages=[{"role": "user", "content": prompt}],
            )
            break
        except RateLimitError as e:
            if attempt == MAX_RATE_LIMIT_RETRIES:
                raise
            time.sleep(_rate_limit_wait_seconds(attempt, e))

    for block in response.content:
        if block.type == "tool_use" and block.name == "record_findings":
            return block.input
    raise RuntimeError("Model did not return a record_findings tool call.")
