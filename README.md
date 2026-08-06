![AtliQ AI Sales Command Center](assets/hero-solution.png)

# AtliQ AI Sales Command Center

A read-only AI memory layer that keeps AtliQ's CRM aligned with what's actually happening in every email thread and meeting note — surfacing mismatches, missing info, and time-sensitive follow-ups in plain language, with evidence.

🔗 **Live Demo:** [AI Sales Command Center](https://ai-sales-command-center-5srun5kqnywjo2wb4dfy3u.streamlit.app/)

## The Problem

Sales conversations move fast, but CRM updates don't — founders and reps spend their time talking to customers, not manually logging every follow-up, so important details get scattered across emails and meeting notes instead of the CRM.

![Sales conversations move fast, CRM updates don't](assets/problem-statement.png)

## The Solution

The AI Sales Command Center loads the CRM export alongside every linked email and meeting note, cross-references them, and flags what's inconsistent, missing, or overdue — each finding backed by a verbatim quote from its source document. It never edits the CRM, emails, or notes, and never sends anything; every suggestion is a label for a human to review, not an automatic action.

## Approach

Built with **Streamlit** (UI) and the **Anthropic API** (Claude, for grounded per-lead analysis), with a rule-based linker/duplicate-detector layer (no LLM) handling CRM-to-document matching. **Prompt-based, not RAG or fine-tuned**: each lead's linked emails/notes are small enough to pass in full to Claude directly — simpler and more reliable than a retrieval pipeline at this data volume, and there's no labelled dataset to fine-tune on responsibly. Two design principles are enforced in code, not just policy: **grounded** (every AI-returned quote is checked against the real source text before it's ever displayed — ungrounded findings are dropped, not shown as fact) and **read-only, human-approved** (no code path ever writes to the CRM, emails, or notes, and no suggestion is ever auto-applied — every one is a label for a human to review and act on themselves).

See [APP_README.md](APP_README.md) for full setup, run instructions, and architecture details.

## Features

- **This Week's Attention** — a ranked list of the leads most needing action this week, scored by deal size, staleness, urgency, and finding severity.
- **Mismatch & Suggested Action** — flags where the CRM contradicts what an email or note actually says, with a suggested fix that's never applied automatically.
- **Full Reasoning Trail** — a "Why did AI recommend this?" expander showing every individual finding behind a lead's score, not just the headline reason.
- **Draft Follow-Up Emails** — generates a draft reply grounded in that lead's real findings, only on request, and never sent automatically.
- **Categories** — browse every finding by type: Mismatches, Missing Info, Follow-ups & Open Questions, Duplicates, Not in CRM, Consider Deprioritizing.
- **Effort vs. Payoff** — every open lead plotted by deal value against staleness, to help decide where to spend time next.
- **Hidden Opportunities** — surfaces medium-urgency leads with a real signal of long-term value (a confirmed budget or a decision-maker present) that could otherwise get skipped.
- **Drill Into a Lead** — the full CRM record, every linked document, and a timeline for one lead in one place.
- **Portfolio Insights** — six charts giving a cross-lead view of the pipeline: pipeline health, client service interest, revenue at risk, lead source effectiveness, findings mix by owner, and competitor mentions.
- **Since Your Last Check** — compares this analysis run to the previous one: what's new, what's resolved, what's still persisting.
- **Floating Assistant** — a template-driven help widget (not a chatbot, no AI calls) that answers questions about how to use the dashboard.

## Project Documentation

1. **Completed user research worksheet**<br>
   [AtliQ_Problem_Discovery_User_Research.xlsx](Project%20Docs/AtliQ_Problem_Discovery_User_Research.xlsx) (+ [PDF companion](Project%20Docs/AtliQ_Problem_Discovery_User_Research.pdf))<br>
   Persona, empathy map, 5 Whys, root-cause problem statement, and proposed solution.

2. **AI opportunity map**<br>
   [AI Product Canvas.pdf](Project%20Docs/AI%20Product%20Canvas.pdf)<br>
   The founder's sales workflow broken into stages, AI capability mapping, and ethical risk mitigation cards (Miro board export).

3. **AI PRD**<br>
   [AI_PRD.pdf](Project%20Docs/AI_PRD.pdf)<br>
   Product requirements for the chosen solution, including a dedicated North Star and Metrics section.

4. **Cost estimation**<br>
   [AtliQ_Cost_Estimation.xlsx](Project%20Docs/AtliQ_Cost_Estimation.xlsx) (+ [PDF companion](Project%20Docs/AtliQ_Cost_Estimation.pdf))<br>
   Model(s) used, expected token usage per interaction, expected volumes, and infrastructure costs.

5. **Working AI prototype**<br>
   See [APP_README.md](APP_README.md) for setup/run instructions, or the live demo linked above.<br>
   This repo: working code plus this README explaining how to run it.

6. **Presentation**<br>
   [AtliQ_Product_Story_Presentation.pptx](Project%20Docs/AtliQ_Product_Story_Presentation.pptx)<br>
   The problem and discovery insights, the opportunity chosen, the solution and a walkthrough, success metrics, and risks and guardrails.

7. **Stakeholder demo video**<br>
   [Watch the demo video](https://drive.google.com/file/d/1wM3xGKPuzX-FjcqsurtuSG1HfQ0qmOsS/view?usp=sharing)<br>
   A walkthrough of the live prototype handling real dataset scenarios, presented to AtliQ's stakeholders. Best watched at 2x speed.

Assignment brief, for reference:<br>
[Capstone 1 Brief - AtliQ Sales and CRM Assistant.pdf](Project%20Docs/Capstone%201%20Brief%20-%20AtliQ%20Sales%20and%20CRM%20Assistant.pdf)

Additional docs (not part of the official checklist, kept for reference):<br>
[Feature_Guide.pdf](Project%20Docs/Feature_Guide.pdf) · [AtliQ_Product_Presentation.pdf](Project%20Docs/AtliQ_Product_Presentation.pdf) (PDF export of item 6's presentation, for viewing without PowerPoint)

## Dataset

Full field-by-field documentation of `crm_export.csv`, `emails/`, and `meeting_notes/` lives in [DATA_DICTIONARY.md](DATA_DICTIONARY.md).
