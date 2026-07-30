# AtliQ Sales & CRM Memory Assistant

A read-only "memory layer" under AtliQ's CRM. It loads `crm_export.csv` plus every
file in `emails/` and `meeting_notes/`, cross-references them, and surfaces what's
inconsistent, missing, or time-sensitive — in plain language, with evidence. It is
a single-purpose analysis tool, not a chatbot, and it never writes back to the CRM,
the emails, or the notes, and never sends anything.

(The original data dictionary lives in `README.md`; this file documents the app.)

## Setup

```
pip install -r requirements.txt
```

Provide your Anthropic API key one of two ways:
- Copy `.env.example` to `.env` and fill in `ANTHROPIC_API_KEY=...`, **or**
- Leave `.env` unset and paste the key into the sidebar's password field when the app is running (kept only in that session's memory, never written to disk).

## Run

```
streamlit run app.py
```

Open the local URL Streamlit prints (default `http://localhost:8501`). Click
**"Run / Refresh analysis"** in the sidebar to analyze every lead that has at
least one linked email or meeting note (leads with zero linked documents are
skipped — there's no evidence to compare against).

## How it works

1. `src/data_loader.py` parses the CRM CSV and every email/note markdown file,
   pulling out each file's trailing *italic* ground-truth line as a separate,
   fact-flagged field.
2. `src/linker.py` matches each CRM lead to its related documents (explicit
   `L-10xx` mentions, contact email, company name, contact name — strongest
   signal first) and separately flags documents whose title clearly names a
   company that has **no** CRM record at all ("Leads Not in CRM").
3. `src/duplicates.py` flags likely duplicate CRM rows via normalized company
   name / contact email / contact name similarity — no LLM needed, this is
   structural.
4. `src/analyzer.py` sends one Claude API call per lead with linked documents
   (via `src/llm_client.py`, using Anthropic's tool-use API with a forced
   tool-call), asking for mismatches, missing-info flags, timing
   signals, and open questions/dropped commitments — each with a verbatim quote
   and source filename. Every returned quote is checked against the actual
   source text before being shown (`_grounded` in `analyzer.py`); anything that
   fails this check is dropped, never displayed as fact.
5. `src/scoring.py` ranks leads for the "This Week's Attention" summary by deal
   size, staleness, explicit due-now timing, and finding severity — not
   alphabetically or by CRM status.
6. `src/cache.py` caches each lead's analysis JSON in `.cache/analysis_cache.json`
   (hashed by lead + linked-document text), so re-opening the app doesn't re-bill
   the API for unchanged data. Use the sidebar's "Ignore cache" checkbox to force
   a fresh analysis.

## Guardrails

- No code path ever opens `crm_export.csv`, `emails/*.md`, or `meeting_notes/*.md`
  in write mode.
- No outbound email/messaging functionality exists anywhere in this app.
- Every "Suggestion" shown in the UI is a label only — nothing is auto-applied.
- Findings must quote real source text; ungrounded model output is filtered out
  before rendering (visible per-lead in the drill-down's "dropped findings"
  expander, for transparency during development).

## Known limitations (prototype scope)

- The company/contact linker is rule-based (regex + fuzzy string matching) and
  tuned to this dataset's conventions; it can miscategorize a document if a
  company is mentioned only deep in a note's body without appearing in the title.
- LLM calls run sequentially per lead with a small thread pool (5 workers) — a
  full first-time analysis of ~30 leads takes roughly a minute.
