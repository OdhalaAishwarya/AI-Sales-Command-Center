# AtliQ Sales & CRM Dataset — Data Dictionary

_The full project overview, screenshots, and live demo link are in the main [README.md](README.md). This file documents the dataset itself._

**Everything here is fully synthetic.** It is inspired by AtliQ's real situation but contains no real client names or deals. Treat **"today" as 2026-07-10**.

**Scope note:** this dataset is about *selling and client relationships* — leads, follow-ups, cross-sells, revisits, renewals. Delivery/project-execution work is out of scope; where a pre-sales demo or prototype appears (GlobalMart, NovaPharma), it exists because AtliQ sometimes builds small demos alongside proposals — a normal part of selling.

## The AtliQ people in this data
All AtliQ team members appear by first name with an `@atliq.com` email: **Bhavin** and **Dhaval** (founders), **Karandeep** (CEO), **Jay** (sales executive), **Pranav** (CTO), and **Ayush** (AI Product Manager). The sellers — Bhavin, Dhaval, Karandeep, and Jay — own leads in the CRM; Pranav and Ayush appear on pre-sales work (demos, scoping).

## How the files connect
The join keys across all three sources are **company name**, **contact name/email**, and (in notes) **lead_id**:
- CRM `contact_email` matches the client's email address in the email threads.
- Meeting notes reference companies and, in several places, CRM lead_ids directly (e.g., L-1003).
- Some emails/notes involve companies **deliberately missing** from the CRM (Harbor & Finch, Lumino Fitness, PixelWorks) — that gap is part of the problem.
- Watch for the Meridian duplicate: L-1003 and L-1027 are the same hospital group, entered twice by two different owners.

## What you have

### 1. `crm_export.csv` — the CRM as it exists today (40 records)
One row per lead/account, exactly as exported. Expect it to be imperfect — that is the point.

| Column | Meaning |
|---|---|
| lead_id | Unique ID assigned by the CRM |
| company | Account name (as typed by whoever entered it) |
| contact_name / contact_email | Primary contact (sometimes missing) |
| source | Referral, LinkedIn, Conference, Website, Cold Outreach |
| service_interest | Which AtliQ service line the lead relates to (often blank) |
| status | New / Contacted / Proposal Sent / Won / Lost (often stale or blank) |
| est_value_usd | Estimated deal value (often blank) |
| owner | Bhavin, Dhaval, Karandeep, or Jay (sometimes blank) |
| created_date / last_contact_date / next_followup_date | Note how often next_followup_date is empty |
| notes | Free text, wildly inconsistent |

### 2. `emails/` — 34 email threads (markdown)
Client and prospect conversations, with full From/To addresses. Some correspond to CRM records; some contradict what the CRM says; some involve leads that never made it into the CRM at all. Italicised lines in parentheses are editorial notes telling you what happened (or didn't) after the thread — treat them as ground truth.

### 3. `meeting_notes/` — 16 meeting notes (markdown)
Call and meeting summaries of very uneven quality — from a structured QBR write-up to a two-line scribble. Read the internal pipeline review (2026-07-02) first: it frames the problem in AtliQ's own words.

## Suggestions
- Pick any account and cross-reference: what the CRM says vs what the emails and notes say. The gaps are the story.
- You may extend this dataset (more emails, more leads, other artifacts you believe should exist) if your solution needs it. Say so in your README.
