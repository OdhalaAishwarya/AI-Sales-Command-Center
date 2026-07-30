"""V2 additions: two small, explicitly-approved synthesis AI calls, kept apart
from analyzer.py's grounded per-lead extraction pipeline so that pipeline's
prompt/schema/PROMPT_VERSION stay completely untouched.

Both calls here are cheap and narrow in scope:
- generate_executive_summary: ONE call per full analysis run, fed only the
  already-computed lead summaries/scores/categories (not raw documents).
- generate_followup_email: ONE call per user click on a single lead's
  detail view, fed only that lead's own findings/suggested action.

Note for a future round: this module is a natural place to hang a
conversational "AI Sales Coach" if one is ever built - out of scope here.
"""
from __future__ import annotations

from anthropic import Anthropic

from .llm_client import DEFAULT_MODEL, MAX_RATE_LIMIT_RETRIES, _rate_limit_wait_seconds
from anthropic import RateLimitError
import time

EXEC_SUMMARY_TOOL = {
    "name": "record_executive_summary",
    "description": "Record a short executive summary synthesizing the current state of a sales pipeline, as bullet points.",
    "input_schema": {
        "type": "object",
        "properties": {
            "bullets": {
                "type": "array",
                "description": "Exactly 4 short, scannable bullet points synthesizing the pipeline snapshot "
                "provided, in this order: (1) the overall counts across categories (mismatches, missing info, "
                "etc.), (2) the most urgent leads, named by specific company, (3) which owner's pipeline needs "
                "the most attention, (4) one overall plain-language takeaway. Each bullet is one short sentence. "
                "Must only reference numbers/names/owners actually present in the provided digest - never invent "
                "a company, owner, or figure.",
                "items": {"type": "string"},
                "minItems": 4,
                "maxItems": 4,
            },
        },
        "required": ["bullets"],
    },
}

EXEC_SUMMARY_SYSTEM_PROMPT = """You are a sales-ops analyst writing a short, scannable executive summary of a \
pipeline snapshot for a sales manager, as bullet points (not a paragraph). You are given pre-computed counts and \
a list of the highest-attention leads (company, owner, urgency score, top reason) - not raw documents. Only \
reference figures, company names, and owner names that literally appear in the digest provided. Do not invent \
numbers or speculate about anything not present in the digest. Produce exactly 4 bullets: overall counts, the \
most urgent specific companies, which owner's pipeline needs the most attention, and one overall takeaway. Each \
bullet should be one short, plain-language sentence."""


FOLLOWUP_EMAIL_TOOL = {
    "name": "record_followup_email",
    "description": "Record a short draft follow-up email for a single sales lead.",
    "input_schema": {
        "type": "object",
        "properties": {
            "subject": {"type": "string", "description": "Short email subject line."},
            "body": {
                "type": "string",
                "description": "A short (3-6 sentence) plain-language draft follow-up email body, referencing "
                "only the concrete facts, names, and commitments given in the lead context below. Professional, "
                "friendly tone. This is a draft for a human to review and send manually - never phrase it as "
                "already sent.",
            },
        },
        "required": ["subject", "body"],
    },
}

FOLLOWUP_EMAIL_SYSTEM_PROMPT = """You are a sales rep's assistant drafting a short follow-up email for ONE lead. \
You are given that lead's known findings (mismatches, missing info, timing signals, open questions) and a \
suggested next action - not the raw source documents. Only reference facts, names, and commitments that appear \
in the context given. Do not invent details. This is a draft only - the human will review and send it manually, \
so never write as if it has already been sent."""


def _call_tool(client: Anthropic, system_prompt: str, tool: dict, user_content: str, model: str) -> dict:
    """Same forced-tool-call + 429-backoff pattern as llm_client.analyze_case_file,
    reused here rather than duplicated, for these two lightweight calls."""
    for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
        try:
            response = client.messages.create(
                model=model,
                max_tokens=1024,
                system=system_prompt,
                tools=[tool],
                tool_choice={"type": "tool", "name": tool["name"]},
                messages=[{"role": "user", "content": user_content}],
            )
            break
        except RateLimitError as e:
            if attempt == MAX_RATE_LIMIT_RETRIES:
                raise
            time.sleep(_rate_limit_wait_seconds(attempt, e))

    for block in response.content:
        if block.type == "tool_use" and block.name == tool["name"]:
            return block.input
    raise RuntimeError(f"Model did not return a {tool['name']} tool call.")


def build_executive_summary_digest(pipeline_stats: dict, top_items: list[dict]) -> str:
    """pipeline_stats: e.g. {'total_leads': 43, 'mismatches': 24, ...}.
    top_items: e.g. [{'company': 'X', 'owner': 'Bhavin', 'score': 8.5, 'top_reason': '...'}]."""
    lines = ["PIPELINE SNAPSHOT (pre-computed - not raw documents):", ""]
    for k, v in pipeline_stats.items():
        lines.append(f"- {k}: {v}")
    lines.append("")
    lines.append(f"TOP {len(top_items)} HIGHEST-ATTENTION LEADS (company, owner, urgency score, top reason):")
    for it in top_items:
        lines.append(f"- {it['company']} (owner: {it['owner'] or 'unassigned'}, score: {it['score']}) - {it['top_reason']}")
    return "\n".join(lines)


def generate_executive_summary(client: Anthropic, pipeline_stats: dict, top_items: list[dict], model: str = DEFAULT_MODEL) -> list[str]:
    digest = build_executive_summary_digest(pipeline_stats, top_items)
    result = _call_tool(client, EXEC_SUMMARY_SYSTEM_PROMPT, EXEC_SUMMARY_TOOL, digest, model)
    return result.get("bullets", [])


def build_followup_email_context(company: str, findings: list[dict], suggested_action: str) -> str:
    lines = [f"LEAD: {company}", ""]
    if suggested_action:
        lines.append(f"Suggested next action: {suggested_action}")
    lines.append("")
    lines.append("Known findings:")
    for f in findings:
        lines.append(f"- ({f['category']}) {f['text']}")
    if not findings:
        lines.append("(none on record)")
    return "\n".join(lines)


def generate_followup_email(client: Anthropic, company: str, findings: list[dict], suggested_action: str, model: str = DEFAULT_MODEL) -> dict:
    context = build_followup_email_context(company, findings, suggested_action)
    return _call_tool(client, FOLLOWUP_EMAIL_SYSTEM_PROMPT, FOLLOWUP_EMAIL_TOOL, context, model)
