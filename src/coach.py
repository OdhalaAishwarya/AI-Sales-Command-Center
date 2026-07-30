"""Dashboard Assistant: a template-driven, zero-AI-call help layer.

This is deliberately NOT a conversational AI - it's a scoped Q&A menu over
fixed dashboard-logic explanations (hardcoded strings describing the real,
unchanging scoring/category logic already in scoring.py/app.py) plus a set
of "Dashboard Insights" lookups that reformat already-computed data
(scores, rankings, memory trail, duplicates, orphans) into a sentence. No
Claude call anywhere in this module - "AI as an assistive layer," not a
chat model.
"""
from __future__ import annotations

OWNER_DISPLAY_TO_CRM = {
    "Karan": "Karandeep",
    "Jay": "Jay",
    "Dhaval": "Dhaval",
    "Bhavin": "Bhavin",
}

CONTACT_EMAIL = "aishwaryaodhale@gmail.com"
CONTACT_NAME = "Aishwarya Odhale"

FALLBACK_MESSAGE = f"I couldn't confidently answer that question. Please reach out to {CONTACT_NAME} for further assistance."
CLIENT_SPECIFIC_MESSAGE = "Please navigate to the Lead Details page and filter by the client name to view complete information."


def _bullets(intro: str, items: list[str], outro: str = "") -> str:
    """Every template/insight answer renders as a one-line direct answer,
    then a bulleted list of the distinct factors/steps, with any formula/
    number/threshold bolded - not one dense paragraph. Built as real HTML
    (not Markdown asterisks/dashes), since the chat bubble that displays
    this is itself rendered via unsafe_allow_html - markdown list syntax
    embedded inside a raw <div> isn't reliably parsed as a list."""
    if not items:
        return intro + (outro or "")
    html = f"{intro}<ul style='margin:6px 0 4px 0;padding-left:1.2em;'>"
    html += "".join(f"<li style='margin-bottom:4px;'>{item}</li>" for item in items)
    html += "</ul>"
    if outro:
        html += outro
    return html


# --------------------------------------------------------------------------
# Dashboard Help: 15 fixed questions, one canonical (question, fuzzy-match
# keywords, answer) triple each - the single source of truth for both the
# exact button-triggered lookup (HELP_TEMPLATES) and the free-text fallback
# matcher (match_template). Order matters for the fallback matcher: more
# specific keyword sets must come before broader ones they overlap with.
# --------------------------------------------------------------------------
HELP_QA: list[tuple[str, tuple[str, ...], str]] = [
    (
        "How are Quick Wins and Deprioritized Clients identified?",
        ("quick wins and deprioritiz", "quick wins and deprioritis"),
        _bullets(
            "Two different things:",
            [
                "<b>Quick Wins</b> (an Effort vs. Payoff quadrant): deal value <b>under $50,000</b>, contacted "
                "within the last <b>45 days</b> - lower stakes but not neglected, often fast to close.",
                "<b>Consider deprioritizing</b> (a separate category, not a quadrant): flags a specific pattern - "
                "repeated unresolved pricing pushback, an explicit competitor mentioned/chosen, or the client "
                "going silent specifically after an objection or a 'not right now.'",
            ],
            "It's a suggestion to reconsider, never automatic.",
        ),
    ),
    (
        "Explain the Effort vs Payoff Matrix",
        ("effort vs payoff", "effort vs. payoff", "effort/payoff", "payoff matrix"),
        _bullets(
            "The Effort vs. Payoff matrix plots every open lead (not Won/Lost) by deal value against days since "
            "last contact, using the CRM export alone - no AI calls involved. Four quadrants:",
            [
                "<b>Prioritize</b>: deal value <b>≥ $50,000</b> and contacted within the last <b>45 days</b>",
                "<b>Quick wins</b>: deal value <b>&lt; $50,000</b> and contacted within the last <b>45 days</b>",
                "<b>Worth the push</b>: deal value <b>≥ $50,000</b> but stale (<b>45+ days</b> since contact)",
                "<b>Consider letting go</b>: deal value <b>&lt; $50,000</b> and stale (<b>45+ days</b> since contact)",
            ],
            "You'll find it under the 'Effort vs. Payoff' section, with a table view available in its expander.",
        ),
    ),
    (
        "How is the Urgency Score calculated?",
        ("urgency score", "urgency is calculated", "how is urgency", "calculate urgency"),
        _bullets(
            "The Urgency Score (shown in This Week's Attention) adds up several signals for a lead:",
            [
                "Mismatch severity: <b>high = 3</b>, <b>medium = 1.5</b>, <b>low = 0.5</b> points",
                "Due-now / due-this-month timing signals: up to <b>3 points</b>",
                "Open questions or dropped commitments: up to <b>3 points</b>",
                "Missing critical CRM info: <b>1-2 points</b>",
                "Likely-duplicate CRM record: <b>+1 point</b>",
                "If any signal is present, deal size and staleness are added as extra context on top",
            ],
            "The total maps to a level: <b>Urgent</b> (red) at <b>6.0+</b>, <b>Check</b> (yellow) at <b>2.5+</b>, "
            "otherwise <b>Watch</b> (green). This is a fixed formula - it never changes between runs unless the "
            "underlying findings change.",
        ),
    ),
    (
        "What does the Completeness Score mean?",
        ("completeness score", "evidence completeness", "what does completeness"),
        _bullets(
            "The Evidence Completeness score (0-100, shown on each lead card) is an honest measure of how much "
            "we actually know about that lead - never a fabricated AI confidence percentage. It adds up:",
            [
                "Linked emails/notes that exist: up to <b>40 points</b>",
                "Contact email, next follow-up date, and deal value filled in on the CRM record: up to <b>40 points</b>",
                "How recently the lead was last contacted: up to <b>20 points</b>",
            ],
            "Click the 'Evidence completeness — why?' button on any card to see exactly which of these "
            "contributed and which are missing.",
        ),
    ),
    (
        "What does \"Consider Deprioritizing\" mean and how is it flagged?",
        ("deprioritiz", "deprioritise"),
        _bullets(
            "'Consider deprioritizing' is a separate category from the urgency score - it flags a specific "
            "pattern, not just a slow or quiet deal:",
            [
                "Repeated unresolved pricing pushback",
                "An explicit mention that the client chose a competitor",
                "The client going silent specifically after an objection or a 'not right now'",
            ],
            "It's a suggestion to reconsider, not an automatic action - you'll find these under the 'Consider "
            "deprioritizing' category card in All Findings.",
        ),
    ),
    (
        "How are companies categorized into Missing Info, Mismatches, Follow-ups, and Open Questions?",
        ("categoriz", "missing info", "mismatch", "follow-up", "follow up", "open question"),
        _bullets(
            "Leads are grouped into 6 categories under 'All Findings':",
            [
                "<b>Mismatches</b>: CRM status/activity contradicts what the emails or notes actually show",
                "<b>Missing info</b>: a critical CRM field (contact email, next follow-up date) is blank despite "
                "real engagement in the docs",
                "<b>Follow-up &amp; open questions</b>: soft timing signals plus unanswered client questions or "
                "dropped AtliQ commitments",
                "<b>Duplicates</b>: likely-duplicate CRM records (same contact/name), detected structurally, no "
                "AI needed",
                "<b>Not in CRM</b>: companies mentioned in emails/notes that were never logged as a CRM lead",
                "<b>Consider deprioritizing</b>: pricing pushback, competitor chosen, or silence after an objection",
            ],
            "Each category ranks its own leads by relevance to that specific category, not by overall urgency.",
        ),
    ),
    (
        "What do the different dashboard sections represent?",
        ("dashboard sections", "what do the different", "what does each section"),
        _bullets(
            "From top to bottom:",
            [
                "<b>Executive Summary</b>: a one-run AI synthesis of the whole pipeline",
                "<b>This Week's Attention</b>: the highest-urgency leads, ranked by the Urgency Score",
                "<b>Since your last check</b>: what's changed - new/resolved/persisting issues - since your "
                "previous run",
                "<b>Hidden Opportunity</b>: medium-urgency leads with strong underlying signals that aren't "
                "top-ranked yet",
                "<b>All Findings</b>: the 6 categories, each with its own ranked list",
                "<b>Effort vs. Payoff</b>: the CRM-only quadrant chart",
            ],
            "'Drill into a lead' at the bottom is a direct search for any single company's full record.",
        ),
    ),
    (
        "How does the \"Since your last check\" section work?",
        ("since your last check", "last check section", "how does since your last"),
        _bullets(
            "Each full analysis run saves a compact snapshot of that run's findings. 'Since your last check' "
            "compares the current snapshot to the immediately previous one:",
            [
                "Findings are grouped by <b>client (company)</b>, not counted raw - so it reports clients, not "
                "individual findings",
                "<b>New</b>: clients with a finding that wasn't there last time",
                "<b>Resolved</b>: clients whose previous finding is gone now",
                "<b>Persisting</b>: clients with the same finding across both runs, ranked by how many "
                "<b>consecutive</b> runs it's lingered",
            ],
            "Pure comparison of stored snapshots - no AI calls involved.",
        ),
    ),
    (
        "What counts as a \"Duplicate\" lead?",
        ("duplicate lead", "what counts as a duplicate", "counts as a duplicate"),
        _bullets(
            "A likely-duplicate CRM pair is detected structurally, not by AI judgment:",
            [
                "Same normalized company name (punctuation/suffixes like Inc/Ltd stripped)",
                "Or the same contact email",
                "Or a closely matching contact name",
            ],
            "Confidence is marked high or low depending on how many of these line up. You'll find these under "
            "the 'Duplicates' category card.",
        ),
    ),
    (
        "How does the tool detect a company missing from the CRM?",
        ("missing from the crm", "detect a company missing", "company not in crm"),
        "Every email thread and meeting note is scanned for the company it's about. If that company doesn't "
        "match any CRM lead (by name or contact), it becomes an <b>orphan lead</b> - surfaced under 'Not in "
        "CRM.' This is structural name/contact matching, not an AI judgment call.",
    ),
    (
        "What does Evidence Completeness tell me that urgency doesn't?",
        ("completeness tell me", "evidence completeness tell me", "urgency doesn't"),
        "They answer different questions. <b>Urgency</b> asks 'how much attention/risk does this lead reflect "
        "right now' (mismatches, timing, missing info combined into a score). <b>Evidence Completeness</b> asks "
        "'how much reliable data do we actually have about this lead' (linked docs, filled-in CRM fields, "
        "recent contact). A lead can be low-urgency but poorly documented, or high-urgency and well-documented "
        "- the two don't move together.",
    ),
    (
        "How does the tool decide a finding is trustworthy?",
        ("finding is trustworthy", "decide a finding", "is a finding trustworthy"),
        "Every finding must include a verbatim quote from a source document. Before it's ever shown, that quote "
        "is checked as an exact substring match (whitespace-normalized) against the real document text. Any "
        "finding that fails this check is <b>dropped</b>, never displayed - though it's still logged in a "
        "'dropped findings' expander on that lead's card, for transparency.",
    ),
    (
        "What's the difference between This Week's Attention and the category views?",
        ("attention and the category", "difference between this week", "week's attention and"),
        "<b>This Week's Attention</b> ranks every lead by one overall Urgency Score. The <b>category views</b> "
        "(under All Findings) each rank leads by that specific category's own relevance - e.g. Mismatches ranks "
        "by mismatch count/severity, not overall urgency. So the same lead can appear in a different order (or "
        "not at all) depending on which view you're looking at.",
    ),
    (
        "How is a lead's Potential Impact (High/Medium/Low) determined?",
        ("potential impact",),
        "Potential Impact combines three things already computed elsewhere - deal value, the existing Urgency "
        "Score, and staleness - into one <b>High / Medium / Low</b> label. It's a derived label only, not a new "
        "calculation or a new AI call.",
    ),
    (
        "Where does the data in this dashboard come from?",
        ("data in this dashboard", "where does the data", "data come from"),
        _bullets(
            "Three read-only sources only:",
            [
                "The CRM export (CSV)",
                "Every email thread",
                "Every meeting note",
            ],
            "The AI's role is limited to comparing them and citing verbatim quotes - nothing is invented, and "
            "nothing is ever written back to the CRM or sent anywhere.",
        ),
    ),
    (
        "Where can I find detailed lead information / lead history?",
        ("lead history", "lead information", "detailed lead", "find detailed"),
        "Use the 'Drill into a lead' section at the bottom of the dashboard - pick a company from the dropdown "
        "to see its full CRM record, every finding, and every linked email/meeting note in one place.",
    ),
    (
        "Who should I contact with questions about this dashboard?",
        ("who should i contact", "contact about this dashboard", "contact support"),
        f"For questions about how this dashboard works, reach out to <b>{CONTACT_NAME}</b> ({CONTACT_EMAIL}).",
    ),
    (
        "How do I filter by owner or company?",
        ("filter by owner", "filter by company", "how do i filter"),
        _bullets(
            "You can filter this dashboard two ways:",
            [
                "Owner buttons at the top scope everything to one person's pipeline (or 'Unassigned' for leads "
                "with no owner on file)",
                "'CRM status' / 'Lead source' filters in the sidebar narrow by status or source",
            ],
            "To find one specific company, use the 'Drill into a lead' search instead of filtering.",
        ),
    ),
]

# The 15 canonical Dashboard Help prompts (for both exact-match routing and
# rotation sampling) - filtered to just the pool named in the enhancement
# spec (the last 2 entries above are older Fix-round prompts kept only for
# free-text fallback matching, not part of the 15-question rotation pool).
HELP_QUESTIONS: list[str] = [q for q, _, _ in HELP_QA][:15]
HELP_TEMPLATES: dict[str, str] = {q: a for q, _, a in HELP_QA}


def match_template(question: str) -> str | None:
    """Keyword-containment match against the fixed template list - no AI
    call, so this is intentionally approximate rather than semantic. Used
    for free-typed questions; button clicks use the exact HELP_TEMPLATES
    lookup instead, which can't be ambiguous."""
    q = question.lower()
    for _, keywords, answer in HELP_QA:
        if any(kw in q for kw in keywords):
            return answer
    return None


def mentions_known_company(question: str, known_companies: list[str]) -> bool:
    q = question.lower()
    return any(c and c.lower() in q for c in known_companies)


# --------------------------------------------------------------------------
# Dashboard Insights: the 15 canonical prompts, each backed by a formatter
# over data app.py already has cached (analyses/scores/duplicates/orphans/
# memory trail/exec summary) - no new AI call, no new scoring logic.
# --------------------------------------------------------------------------
INSIGHT_QUESTIONS: list[str] = [
    "What are my Top 3 priorities today?",
    "Which leads need immediate attention?",
    "How many leads are currently flagged Urgent?",
    "Which leads have been persisting the longest?",
    "What's changed since my last check?",
    "Which leads are missing key information?",
    "Which leads look like duplicates right now?",
    "Which companies aren't in the CRM yet?",
    "What are the Hidden Opportunities right now?",
    "Which leads should I consider deprioritizing?",
    "What's my highest-value deal that's currently stale?",
    "How many leads are in the Quick Wins quadrant?",
    "Which leads have unanswered open questions?",
    "What does today's Executive Summary say?",
    "Which leads have a suggested action ready to send?",
]


def format_top_priorities(rows: list[dict], owner_label: str, top_n: int = 3) -> str:
    """'What are my Top 3 priorities today?' - pure reformatting of the
    already-computed AttentionItem ranking (see app.py's _coach_lead_rows),
    no new scoring logic and no AI call."""
    if not rows:
        return f"No analyzed leads found for {owner_label} right now - try Run/Refresh analysis first."
    top = sorted(rows, key=lambda r: r["score"], reverse=True)[:top_n]
    items = [
        f"<b>{r['company']} ({r['tag']})</b> — {r['urgency_level']}, score <b>{r['score']}</b>: {r['top_finding']}"
        for r in top
    ]
    return _bullets(f"Top {len(top)} priorities for {owner_label} today:", items)


def format_urgent_leads(rows: list[dict], owner_label: str) -> str:
    """'Which leads need immediate attention?' - same idea, filtered to the
    'Urgent' (red) urgency level already computed by scoring.py. Rows come
    in with the display label already applied (see app.py's
    _coach_lead_rows), hence the "Urgent" check, not the raw "red" value."""
    urgent = [r for r in rows if r["urgency_level"] == "Urgent"]
    if not urgent:
        return f"No leads are currently flagged Urgent for {owner_label}."
    urgent.sort(key=lambda r: r["score"], reverse=True)
    items = [f"<b>{r['company']} ({r['tag']})</b> — score <b>{r['score']}</b>: {r['top_finding']}" for r in urgent]
    return _bullets(f"{len(urgent)} lead(s) need immediate attention for {owner_label}:", items)


def format_urgent_count(rows: list[dict], owner_label: str) -> str:
    """'How many leads are currently flagged Urgent?' - a straight count
    plus the Check/Watch breakdown, from the same already-computed levels."""
    counts = {"Urgent": 0, "Check": 0, "Watch": 0}
    for r in rows:
        counts[r["urgency_level"]] = counts.get(r["urgency_level"], 0) + 1
    return _bullets(
        f"For {owner_label}: <b>{counts['Urgent']}</b> lead(s) are currently flagged Urgent.",
        [
            f"<b>Urgent</b>: {counts['Urgent']}",
            f"<b>Check</b>: {counts['Check']}",
            f"<b>Watch</b>: {counts['Watch']}",
        ],
    )


def format_persisting_longest(persisting_clients: list[dict], top_n: int = 5) -> str:
    """'Which leads have been persisting the longest?' - reuses the memory
    trail's own persisting_clients list (already sorted by consecutive-run
    streak in app.py's compute_memory_trail) - no recomputation."""
    if not persisting_clients:
        return "No persisting issues on record yet - this builds up once there are at least two completed runs."
    top = persisting_clients[:top_n]
    items = [
        f"<b>{c['company']}</b> — {c['count']} persisting issue(s), flagged for <b>{c['streak']}</b> consecutive checks"
        for c in top
    ]
    return _bullets(f"Top {len(top)} longest-persisting clients:", items)


def format_since_last_check(n_new: int, n_resolved: int, n_persisting: int) -> str:
    """'What's changed since my last check?' - the same three counts
    already shown as badges in the 'Since your last check' section."""
    return _bullets(
        "Since your last check:",
        [
            f"<b>{n_new}</b> client(s) have new issues",
            f"<b>{n_resolved}</b> client(s)' issues resolved",
            f"<b>{n_persisting}</b> client(s) have persisting issues",
        ],
    )


def format_missing_info_leads(rows: list[dict]) -> str:
    """'Which leads are missing key information?'"""
    missing = [r for r in rows if r.get("missing_info_count", 0) > 0]
    if not missing:
        return "No leads currently have a missing-info flag."
    missing.sort(key=lambda r: r["missing_info_count"], reverse=True)
    items = [f"<b>{r['company']} ({r['tag']})</b> — {r['missing_info_count']} missing-info flag(s)" for r in missing]
    return _bullets(f"{len(missing)} lead(s) are missing key information:", items)


def format_duplicate_pairs(pair_labels: list[str]) -> str:
    """'Which leads look like duplicates right now?' - reuses the
    already-computed duplicate_pairs_visible list, just formatted."""
    if not pair_labels:
        return "No likely-duplicate CRM records among the currently visible leads."
    return _bullets(f"{len(pair_labels)} likely-duplicate pair(s):", pair_labels)


def format_orphans(company_names: list[str]) -> str:
    """'Which companies aren't in the CRM yet?' - reuses the already-
    computed orphans_visible list, just formatted."""
    if not company_names:
        return "No companies currently flagged as missing from the CRM."
    items = [f"<b>{name}</b>" for name in company_names]
    return _bullets(f"{len(company_names)} compan(y/ies) mentioned in docs but not in the CRM:", items)


def format_hidden_opportunities(rows: list[dict]) -> str:
    """'What are the Hidden Opportunities right now?'"""
    hidden = [r for r in rows if r.get("hidden_opportunity") == "yes"]
    if not hidden:
        return "No hidden opportunities among the currently visible leads."
    items = [f"<b>{r['company']} ({r['tag']})</b> — {r['top_finding']}" for r in hidden]
    return _bullets(f"{len(hidden)} hidden opportunit(y/ies):", items)


def format_deprioritize_candidates(rows: list[dict]) -> str:
    """'Which leads should I consider deprioritizing?'"""
    candidates = [r for r in rows if r.get("deprioritize_count", 0) > 0]
    if not candidates:
        return "No leads currently show a deprioritize signal."
    items = [f"<b>{r['company']} ({r['tag']})</b> — {r['deprioritize_count']} signal(s)" for r in candidates]
    return _bullets(f"{len(candidates)} lead(s) to consider deprioritizing:", items)


def format_highest_value_stale(rows: list[dict], stale_days_threshold: int = 45) -> str:
    """'What's my highest-value deal that's currently stale?'"""
    stale = [r for r in rows if r.get("deal_value") and r.get("days_stale") is not None and r["days_stale"] >= stale_days_threshold]
    if not stale:
        return f"No deals with a recorded value are currently stale ({stale_days_threshold}+ days since contact)."
    top = max(stale, key=lambda r: r["deal_value"])
    return (
        f"<b>{top['company']} ({top['tag']})</b> — <b>${top['deal_value']:,.0f}</b>, "
        f"<b>{top['days_stale']} days</b> since last contact."
    )


def format_quick_wins_count(rows: list[dict], value_threshold: float, staleness_threshold: int) -> str:
    """'How many leads are in the Quick Wins quadrant?' - reuses the exact
    same VALUE_THRESHOLD/STALENESS_THRESHOLD the Effort vs. Payoff chart
    itself uses (passed in from app.py), so this can't drift out of sync."""
    quick_wins = [
        r for r in rows
        if r.get("deal_value") is not None and r.get("days_stale") is not None
        and r["status"] not in ("won", "lost")
        and r["deal_value"] < value_threshold and r["days_stale"] < staleness_threshold
    ]
    if not quick_wins:
        return "No leads currently fall in the Quick Wins quadrant."
    items = [f"<b>{r['company']} ({r['tag']})</b> — ${r['deal_value']:,.0f}, {r['days_stale']} days stale" for r in quick_wins]
    return _bullets(f"<b>{len(quick_wins)}</b> lead(s) are in the Quick Wins quadrant:", items[:10])


def format_unanswered_questions(rows: list[dict]) -> str:
    """'Which leads have unanswered open questions?'"""
    leads = [r for r in rows if r.get("unanswered_questions_count", 0) > 0]
    if not leads:
        return "No leads currently have an unanswered open question."
    items = [f"<b>{r['company']} ({r['tag']})</b> — {r['unanswered_questions_count']} unanswered question(s)" for r in leads]
    return _bullets(f"{len(leads)} lead(s) have unanswered open questions:", items)


def format_exec_summary_echo(exec_summary: list[str] | str | None) -> str:
    """'What does today's Executive Summary say?' - echoes the ALREADY-
    GENERATED Executive Summary bullets verbatim (see app.py's run_clicked
    block) - no recomputation, no new call."""
    if not exec_summary:
        return "No Executive Summary yet - click Run/Refresh analysis in the sidebar to generate one."
    bullets = exec_summary if isinstance(exec_summary, list) else [exec_summary]
    return _bullets("Today's Executive Summary:", bullets)


def format_ready_to_send(rows: list[dict]) -> str:
    """'Which leads have a suggested action ready to send?' - "ready" means
    both a suggested_next_action exists AND a contact_email is on file, since
    that's what the app's own Gmail flow requires to actually send."""
    ready = [r for r in rows if r.get("has_action") and r.get("has_contact_email")]
    if not ready:
        return "No leads currently have both a suggested action and a contact email on file."
    items = [f"<b>{r['company']} ({r['tag']})</b> — {r['top_finding']}" for r in ready]
    return _bullets(f"{len(ready)} lead(s) have a suggested action ready to send:", items)
