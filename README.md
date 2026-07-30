![AtliQ AI Sales Command Center](assets/hero-solution.png)

# AtliQ AI Sales Command Center

A read-only AI memory layer that keeps AtliQ's CRM aligned with what's actually happening in every email thread and meeting note — surfacing mismatches, missing info, and time-sensitive follow-ups in plain language, with evidence.

🔗 **Live Demo:** _[link coming soon]_

## The Problem

Sales conversations move fast, but CRM updates don't — founders and reps spend their time talking to customers, not manually logging every follow-up, so important details get scattered across emails and meeting notes instead of the CRM.

![Sales conversations move fast, CRM updates don't](assets/problem-statement.png)

## The Solution

The AI Sales Command Center loads the CRM export alongside every linked email and meeting note, cross-references them, and flags what's inconsistent, missing, or overdue — each finding backed by a verbatim quote from its source document. It never edits the CRM, emails, or notes, and never sends anything; every suggestion is a label for a human to review, not an automatic action.

## Tech Stack / How It Works

Built with **Streamlit** (UI) and the **Anthropic API** (Claude, for grounded per-lead analysis), with a rule-based linker/duplicate-detector layer (no LLM) handling CRM-to-document matching. Every AI-returned quote is checked against the real source text before it's ever displayed — ungrounded findings are dropped, not shown as fact.

See [APP_README.md](APP_README.md) for full setup, run instructions, and architecture details.

## Project Documentation
- [Product Canvas](Project%20Docs/AI%20Product%20Canvas.pdf)
- [PRD](Project%20Docs/AI_PRD.pdf)
- [Problem Discovery & User Research](Project%20Docs/Problem_Discovery_User_Research.pdf)

## Dataset

Full field-by-field documentation of `crm_export.csv`, `emails/`, and `meeting_notes/` lives in [DATA_DICTIONARY.md](DATA_DICTIONARY.md).
