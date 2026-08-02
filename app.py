"""AI Sales Command Center.

A read-only "memory layer" under the CRM: loads the CRM export plus every email
thread and meeting note, cross-references them, and surfaces mismatches, missing
info, duplicates, timing signals, and dropped commitments — with evidence.

This tool never edits crm_export.csv, never writes to emails/ or meeting_notes/,
and never sends any message. Every "suggested" action is a label only.
"""
from __future__ import annotations

import base64
import hashlib
import os
import random
import re
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))

import altair as alt
import pandas as pd
import streamlit as st
from anthropic import APIStatusError

from src.analyzer import LeadAnalysis
from src.cache import (
    CACHE_PATH, append_history_snapshot, get_or_analyze, load_cache,
    load_history, load_insights_cache, save_cache, save_insights_cache,
)
from src.data_loader import Document, load_all_docs, load_crm
from src.duplicates import DuplicatePair, find_duplicates
from src.coach import (
    CLIENT_SPECIFIC_MESSAGE, CONTACT_EMAIL, FALLBACK_MESSAGE, HELP_QUESTIONS, HELP_TEMPLATES,
    INSIGHT_QUESTIONS, OWNER_DISPLAY_TO_CRM, format_deprioritize_candidates, format_duplicate_pairs,
    format_exec_summary_echo, format_hidden_opportunities, format_highest_value_stale,
    format_missing_info_leads, format_orphans, format_persisting_longest, format_quick_wins_count,
    format_ready_to_send, format_since_last_check, format_top_priorities, format_unanswered_questions,
    format_urgent_count, format_urgent_leads, match_template, mentions_known_company,
)
from src.insights import generate_executive_summary, generate_followup_email
from src.linker import CaseFile, OrphanLead, build_case_files
from src.llm_client import DEFAULT_MODEL, get_client
from src.scoring import (
    TODAY, AttentionItem, evidence_completeness, is_hidden_opportunity,
    is_high_risk, potential_impact, rank_attention, score_lead,
)

st.set_page_config(page_title="AI Sales Command Center", layout="wide")

# Urgency vocabulary shared by "This Week's Attention" (scoring.py's red/yellow/green
# AttentionItem.urgency_level, unchanged) and the new findings list below - both
# render through badge_html() so both automatically get the same icon-based look.
BADGE_LEVELS = {
    "urgent": {"label": "Urgent", "icon": "priority_high", "color": "#b91c1c", "bg": "rgba(220,38,38,0.12)"},
    "check": {"label": "Check", "icon": "warning", "color": "#92400e", "bg": "rgba(180,83,9,0.12)"},
    "watch": {"label": "Watch", "icon": "visibility", "color": "#4b5563", "bg": "rgba(107,114,128,0.12)"},
    # Deliberately NOT on the urgent/check/watch severity scale - "consider
    # deprioritizing" is a different kind of signal (fit/interest), not urgency.
    "consider": {"label": "Consider", "icon": "trending_down", "color": "#57534e", "bg": "rgba(120,113,108,0.14)"},
}
URGENCY_TO_BADGE = {"red": "urgent", "yellow": "check", "green": "watch"}

# V2 section 3/10: evidence-completeness colors reuse the exact same
# danger/warning/success hex values already used elsewhere (BADGE_LEVELS'
# "urgent"/"check" colors, and the green already used for "resolved" in the
# memory trail) rather than inventing a new palette.
EVIDENCE_LEVEL_COLORS = {
    "success": ("#15803d", "rgba(21,128,61,0.14)"),
    "warning": ("#92400e", "rgba(180,83,9,0.12)"),
    "danger": ("#b91c1c", "rgba(220,38,38,0.12)"),
}

# Recolored to the app's purple-to-magenta brand theme (was a mixed
# red/orange/blue/gray scheme) - warm, needs-attention categories lean
# magenta/pink, neutral categories lean cooler/muted purple. Semantic
# distinction (attention vs. neutral) is kept via warm-vs-cool tone, not via
# the old red/orange/blue meaning - numbers/icons/filter behavior unchanged.
# NOTE: BADGE_LEVELS (urgent/check/watch/consider urgency badges) is a
# separate dict and deliberately NOT touched here - that's an urgency
# signal, not a category color.
CATEGORIES = [
    {"key": "mismatches", "label": "Mismatches", "icon": "warning", "color": "#BE185D", "bg": "rgba(190,24,93,0.10)"},
    {"key": "missing_info", "label": "Missing info", "icon": "assignment_late", "color": "#DB2777", "bg": "rgba(219,39,119,0.10)"},
    # Fix A: timing_signals + open_questions_or_dropped_commitments merged into
    # one DISPLAY category - both fields still come straight from the same
    # LeadAnalysis, unchanged; this only affects how they're grouped/labeled.
    {"key": "followup_open_questions", "label": "Follow-up & open questions", "icon": "pending_actions", "color": "#C026D3", "bg": "rgba(192,38,211,0.10)"},
    {"key": "duplicates", "label": "Duplicates", "icon": "content_copy", "color": "#7C3AED", "bg": "rgba(124,58,237,0.10)"},
    {"key": "not_in_crm", "label": "Not in CRM", "icon": "person_search", "color": "#8B5CF6", "bg": "rgba(139,92,246,0.10)"},
    {"key": "deprioritize", "label": "Consider deprioritizing", "icon": "trending_down", "color": "#7C6A9C", "bg": "rgba(124,106,156,0.10)"},
]
CATEGORIES_BY_KEY = {c["key"]: c for c in CATEGORIES}

# scoring.py's reason_details use short category keys (mismatch/timing/...);
# this maps them to the matching card in CATEGORIES for icon/color/label reuse.
# Both "timing" and "open_question" map to the same merged card (Fix A).
REASON_CATEGORY_TO_CARD = {
    "mismatch": "mismatches",
    "timing": "followup_open_questions",
    "open_question": "followup_open_questions",
    "missing_info": "missing_info",
    "duplicate": "duplicates",
}

# all_individual_findings() keeps the 5 underlying finding types distinct
# (needed to pick the right one when substituting real content for a count-
# only primary line); this is the display-only merge for their icon/tag.
FINDING_TYPE_TO_CARD = {
    "mismatches": "mismatches",
    "missing_info": "missing_info",
    "timing": "followup_open_questions",
    "open_questions": "followup_open_questions",
    "deprioritize": "deprioritize",
}

DEPRIORITIZE_SIGNAL_LABELS = {
    "pricing_pushback": "Repeated pricing pushback",
    "competitor_chosen": "Competitor mentioned/chosen",
    "went_silent_after_objection": "Went silent after objection",
}

# AI Sales Coach icons: the user supplied these as actual image files (not
# icon-font glyphs) - embedded as base64 data URIs so they render inline
# with no extra HTTP request/static file server needed.
ASSETS_DIR = Path(__file__).resolve().parent / "assets"
# Two distinct bot images share the assets/ folder - the original single-
# gradient/one-speech-bubble icon (for the small 58x58 floating launcher
# button) and the newer full-body two-speech-bubble artwork the user
# provided to replace robot-man.png (for the larger in-panel avatar). Both
# briefly collided under the same "chatbot.png" filename; copied each to
# its own clearly-named file so neither reference is ambiguous going
# forward. Original files (chatbot - Copy.png / chatbot.png) left in place,
# now unused by the app but not deleted.
# Launcher was flat magenta/pink while the (native, unmodified) hourglass
# spans magenta->purple->blue/cyan - recolored offline (PIL, same
# alpha/white-detail-preserving technique, sampled directly from
# watch-glass.png's own top/bottom pixel colors) so both launcher icons
# read as one matched pair. Saved as chatbot-launcher-matched.png; the
# original chatbot-launcher.png is left on disk, just no longer referenced.
COACH_LAUNCHER_ICON_B64 = base64.b64encode((ASSETS_DIR / "chatbot-launcher-matched.png").read_bytes()).decode("ascii")
COACH_AVATAR_ICON_B64 = base64.b64encode((ASSETS_DIR / "chatbot-avatar.png").read_bytes()).decode("ascii")
# Reverted: back to the original, unmodified source asset (native
# magenta->purple->blue/cyan gradient, no PIL recolor, no CSS filter) per
# explicit instruction to restore both icons' native colors. The recolored
# watch-glass-recolored.png file is left on disk, just no longer referenced.
WORKPLAN_HOURGLASS_ICON_B64 = base64.b64encode((ASSETS_DIR / "watch-glass.png").read_bytes()).decode("ascii")

CSS = """
<style>
/* Renders Google Material Symbols glyphs - Streamlit already loads this font
   file for its own native icon support, so this needs no external CDN. */
.msi {
    font-family: 'Material Symbols Rounded';
    font-weight: normal;
    font-style: normal;
    line-height: 1;
    letter-spacing: normal;
    text-transform: none;
    white-space: nowrap;
    word-wrap: normal;
    direction: ltr;
    -webkit-font-feature-settings: 'liga';
    font-feature-settings: 'liga';
    -webkit-font-smoothing: antialiased;
    vertical-align: middle;
}
.atliq-card {
    border: 1px solid #2D2438;
    border-radius: 10px;
    padding: 14px 16px;
    margin-bottom: 12px;
    background: #15121F;
}
/* V2 section 10: cards can now show multiple badges (urgency + impact +
   evidence completeness) on the same row - let them wrap with consistent
   spacing on narrow widths instead of overflowing or touching. */
.atliq-card .icon-badge {
    margin-top: 4px;
}
.atliq-evidence {
    border-left: 3px solid rgba(128,128,128,0.4);
    padding: 6px 12px;
    margin-top: 8px;
    font-style: italic;
    font-size: 0.92rem;
    opacity: 0.9;
}
.atliq-source {
    font-size: 0.78rem;
    opacity: 0.65;
    margin-top: 2px;
}
.atliq-suggestion {
    font-size: 0.85rem;
    margin-top: 8px;
    padding: 6px 10px;
    border-radius: 6px;
    background: rgba(59,130,246,0.12);
    border: 1px dashed rgba(59,130,246,0.4);
    text-decoration: none;
}
.icon-badge {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    padding: 3px 11px;
    border-radius: 999px;
    font-size: 0.76rem;
    font-weight: 600;
    white-space: nowrap;
}
.icon-badge .msi { font-size: 15px; }

.category-card {
    border-radius: 14px;
    padding: 18px 16px 14px;
    border: 2px solid transparent;
    transition: border-color 0.15s ease;
}
.category-card.selected { border-color: var(--card-color); }
.category-card .cat-icon { font-size: 26px; color: var(--card-color); }
.category-card .cat-count { font-size: 2rem; font-weight: 700; line-height: 1.15; margin-top: 8px; }
.category-card .cat-unit { font-size: 0.68rem; opacity: 0.75; text-transform: uppercase; letter-spacing: 0.03em; margin-top: -2px; }
.category-card .cat-label { font-size: 0.86rem; opacity: 0.72; margin-top: 2px; }

/* The buttons directly under each category card are the actual click target
   (Streamlit can't fire a Python callback from a raw HTML div click) - round
   them to match the cards above so the pair reads as one unit. */
div[data-testid="stVerticalBlock"] button[kind="secondary"],
div[data-testid="stVerticalBlock"] button[kind="primary"] {
    border-radius: 10px;
}

/* ------------------------------------------------------------------
   Part 0 — professional visual overhaul. The color/font/radius theme
   itself lives in .streamlit/config.toml (Streamlit's native theme
   system); this block only hides default chrome, widens whitespace,
   and polishes the sidebar/divider treatment on top of that theme.
   ------------------------------------------------------------------ */

/* Chrome hiding. client.toolbarMode="minimal" in config.toml already
   removes the menu/deploy button natively; these are a defensive
   fallback (including the older-version selectors) in case that ever
   leaves something behind. */
#MainMenu, footer, [data-testid="stStatusWidget"] { visibility: hidden; height: 0; }
[data-testid="stHeader"], [data-testid="stToolbar"], [data-testid="stAppDeployButton"] {
    display: none;
    height: 0;
}

/* Custom HTML elements (cards/badges/rows) are raw divs via st.markdown,
   so they don't automatically inherit the configured theme font. */
html, body, [class*="css"], .atliq-card, .category-card, .icon-badge {
    font-family: "Inter", system-ui, -apple-system, "Segoe UI", sans-serif;
}

/* More generous whitespace, now that the header chrome is gone. */
[data-testid="stMainBlockContainer"] {
    padding-top: 2.5rem;
    padding-bottom: 3rem;
}
[data-testid="stVerticalBlock"] { gap: 0.9rem; }

/* Subtle hairline dividers instead of the default heavy rule. */
[data-testid="stMainBlockContainer"] hr {
    border: none;
    border-top: 1px solid rgba(255,255,255,0.08);
    margin: 2rem 0;
}

/* Sidebar: quieter, more deliberate spacing. */
section[data-testid="stSidebar"] {
    border-right: 1px solid rgba(255,255,255,0.06);
}
section[data-testid="stSidebar"] [data-testid="stVerticalBlock"] { gap: 0.7rem; }
/* The sidebar is icon-rail-only now (everything else moved to the main
   header) - vertically center that one icon group within the full sidebar
   height instead of leaving it top-anchored with all the empty space
   stranded below. Horizontal centering/spacing/tooltips/active-highlight
   are untouched (still handled by [class*="st-key-nav_rail"] itself further
   down) - this only changes where that block sits vertically. */
section[data-testid="stSidebar"] [data-testid="stSidebarUserContent"] {
    display: flex;
    flex-direction: column;
    justify-content: center;
    min-height: 100vh;
}
section[data-testid="stSidebar"] hr {
    border: none;
    border-top: 1px solid rgba(255,255,255,0.08);
    margin: 1rem 0;
}

/* Existing card/row surfaces get a hairline border instead of relying
   only on a faint fill, for definition against the new dark surface.
   (.category-card keeps its own 2px transparent->color border further
   up, for the selection highlight - not touched here.) */
.atliq-card {
    border: 1px solid #2D2438;
}
/* Hidden Opportunity empty-state demo card - dashed border + slightly
   desaturated/muted so it visually reads as a mockup, never mistakable for
   a real live result. */
.example-card {
    border-style: dashed !important;
    border-color: rgba(167,159,184,0.45) !important;
    opacity: 0.82;
    filter: saturate(0.75);
}

/* AtliQ brand color scheme: purple-to-magenta gradient accent, replacing
   the previous teal, applied to structural surfaces/headings only -
   semantic status colors (urgent/red, missing-info/yellow-orange,
   follow-up/blue, etc. in BADGE_LEVELS/CATEGORIES/quadrant colors) are
   deliberately untouched, since those carry meaning, not brand identity. */
:root { --atliq-gradient: linear-gradient(135deg, #8B5CF6 0%, #EC4899 100%); }

/* "Command Center" gradient text-fill treatment on the app's own title
   (sidebar + main) and each tab's own page header - not on every heading
   (e.g. not on dataframe/table headers, which aren't h1/h2 anyway). */
[data-testid="stSidebar"] h1,
[data-testid="stMainBlockContainer"] h1,
[data-testid="stMainBlockContainer"] h2 {
    background: var(--atliq-gradient);
    -webkit-background-clip: text;
    background-clip: text;
    color: transparent;
}

/* Sidebar navigation as an icon-only rail: each nav button already carries
   a unique "st-key-navtab_<tab>" class - hide the label text, keep the
   icon, center a small square button, and stack them with even vertical
   gaps via the wrapping [class*="st-key-nav_rail"] container. Hover
   tooltip is Streamlit's own native `help=` mechanism (a small (?) that
   reveals the tab name on hover) rather than a custom ::after tooltip,
   since that's the one method guaranteed to render correctly across
   Streamlit's own nested button DOM (learned the hard way earlier in this
   project - see the Fix 3 comment below on how deep that nesting goes). */
/* Root cause of the icons hugging the left edge: this stVerticalBlock is
   itself a flex column whose default cross-axis alignment is
   align-items:start, so each 72px-wide button container sizes to its own
   content and sits flush-left within the rail's full (sidebar) width - the
   justify-content:center on the child element containers below was a
   no-op, since those children were never stretched wide enough to have
   room to center anything within. align-items:center here is the actual
   fix - it centers each 72px child within the rail's full width. */
[class*="st-key-nav_rail"] {
    align-items: center;
}
[class*="st-key-nav_rail"] [data-testid="stElementContainer"] {
    display: flex;
    justify-content: center;
}
[class*="st-key-navtab_"] button {
    width: 72px !important;
    height: 72px !important;
    border-radius: 18px !important;
    padding: 0 !important;
    display: flex;
    align-items: center;
    justify-content: center;
    background: transparent !important;
    border: 1px solid transparent !important;
    color: #A79FB8 !important;
}
[class*="st-key-navtab_"] button p { display: none !important; }
[class*="st-key-navtab_"] button .msi,
[class*="st-key-navtab_"] button span[data-testid="stIconMaterial"] {
    font-size: 38px !important;
}
[class*="st-key-navtab_"] button:hover {
    background: rgba(139,92,246,0.15) !important;
    color: #F1EEF7 !important;
}
[class*="st-key-navtab_"] button[kind="primary"] {
    background: var(--atliq-gradient) !important;
    border: none !important;
    color: #ffffff !important;
}

/* Owner filter row restyled as rounded pills - active pill uses the brand
   gradient with white text; inactive pills are dark with a subtle purple
   border, same purple-tinted hover as the nav rail above. */
[class*="st-key-owner_btn_"] button {
    border-radius: 999px !important;
    border: 1px solid #2D2438 !important;
    background: #15121F !important;
    color: #A79FB8 !important;
}
[class*="st-key-owner_btn_"] button:hover {
    background: rgba(139,92,246,0.15) !important;
    border-color: rgba(139,92,246,0.5) !important;
    color: #F1EEF7 !important;
}
[class*="st-key-owner_btn_"] button[kind="primary"] {
    background: var(--atliq-gradient) !important;
    border: none !important;
    color: #ffffff !important;
}

/* Brain icon beside the main title - AI Memory Layer glyph, gradient
   stroke via background-clip:text (same technique as the gradient
   headings above), on a dark circular chip with a soft two-tone glow
   (same layered-drop-shadow / rgba-glow pattern already used for the
   floating hourglass/chat launcher icons - not a flat single color). */
.brain-icon-wrap {
    width: 64px;
    height: 64px;
    min-width: 64px;
    border-radius: 50%;
    background: rgba(139,92,246,0.10);
    border: 1px solid rgba(139,92,246,0.35);
    display: flex;
    align-items: center;
    justify-content: center;
    box-shadow: 0 0 12px rgba(139,92,246,0.35), 0 0 18px rgba(236,72,153,0.28);
}
.brain-icon-glyph {
    font-size: 36px;
    background: var(--atliq-gradient);
    -webkit-background-clip: text;
    background-clip: text;
    color: transparent;
}

/* Header control stack (API key badge / filters / Refresh), right-aligned
   in line with the title - tighter vertical gap than the app's default
   block spacing so it reads as one compact stack, not spread-out rows. */
[class*="st-key-header_controls_stack"] [data-testid="stVerticalBlock"] {
    gap: 0.3rem;
}
[class*="st-key-header_controls_stack"] {
    display: flex;
    flex-direction: column;
    align-items: flex-end;
}
[class*="st-key-header_controls_stack"] [data-testid="stElementContainer"] {
    width: 100%;
    display: flex;
    justify-content: flex-end;
}

/* Refresh button: content-sized pill (not a full-width stretched block) so
   it doesn't visually crowd the owner-pill row directly beneath it -
   matches the owner pills' own border-radius/weight for consistency.
   use_container_width was removed in Python so this button is naturally
   content-width; the rules below just add padding/radius/hover on top of
   that, they don't fight a width:100% from Streamlit itself.

   Root cause of the "stuck at the left edge" misalignment: the outer
   [class*="st-key-header_controls_stack"] [data-testid="stElementContainer"]
   rule right-justifies its direct flex child, but that child is
   Streamlit's own [data-testid="stButton"] wrapper div - which still spans
   the full container width, so the actual <button> (now auto-width instead
   of the 100%-wide default) was sitting at THAT wrapper's own left edge,
   one level down from where the outer rule's justify-content applies. The
   badge/checkboxes don't have this extra wrapper level, which is why only
   Refresh drifted left. Forcing every element between the stElementContainer
   and the button back to flex/justify-end (not a manual margin/position
   offset) closes that gap so the button aligns the same way as the rest. */
[class*="st-key-refresh_btn"] {
    margin-top: 0 !important;
}
[class*="st-key-refresh_btn"] [data-testid="stButton"] {
    display: flex !important;
    justify-content: flex-end !important;
    width: 100% !important;
}
[class*="st-key-refresh_btn"] button {
    width: auto !important;
    padding: 10px 26px !important;
    font-weight: 500 !important;
    border-radius: 999px !important;
    background: var(--atliq-gradient) !important;
    border: none !important;
    transition: transform 0.15s ease, filter 0.15s ease;
}
[class*="st-key-refresh_btn"] button:hover {
    filter: brightness(1.1);
    transform: scale(1.03);
}

/* Carousel: circular score indicator + a compact numbered dot/jump row. */
.score-circle {
    width: 52px;
    height: 52px;
    min-width: 52px;
    border-radius: 50%;
    display: flex;
    align-items: center;
    justify-content: center;
    font-weight: 700;
    font-size: 1.05rem;
    border: 3px solid var(--score-color);
    flex-shrink: 0;
}
.source-doc-label {
    font-size: 0.78rem;
    font-weight: 600;
    opacity: 0.75;
    text-transform: uppercase;
    letter-spacing: 0.03em;
}

/* Fix 3 (re-diagnosed): the company-name buttons in category top-3/view-all
   lists and Weekly Attention's view-all list are list ROWS, not centered
   actions - left-align them like any other list, matching e.g. the Effort
   vs. Payoff table view. Streamlit stamps a "st-key-<key>" class on a
   widget's wrapper when it's given an explicit key.
   The first attempt at this only set justify-content on the <button> itself
   and text-align on its inner <p>, but real DOM inspection (Playwright)
   showed Streamlit wraps button content in two MORE nested flex containers
   between the button and the <p> (a div, then a span), each with its own
   justify-content: center - those, not the <p>'s text-align, are what
   actually position the label's box, so the fix has to reach every flex
   layer, not just the outermost and innermost one. */
[class*="st-key-cat_top3_"] button,
[class*="st-key-cat_viewall_"] button,
[class*="st-key-wa_viewall_"] button,
[class*="st-key-cat_top3_"] button div,
[class*="st-key-cat_viewall_"] button div,
[class*="st-key-wa_viewall_"] button div,
[class*="st-key-cat_top3_"] button span,
[class*="st-key-cat_viewall_"] button span,
[class*="st-key-wa_viewall_"] button span {
    justify-content: flex-start !important;
}
[class*="st-key-cat_top3_"] button p,
[class*="st-key-cat_viewall_"] button p,
[class*="st-key-wa_viewall_"] button p,
[class*="st-key-cat_top3_"] button div,
[class*="st-key-cat_viewall_"] button div,
[class*="st-key-wa_viewall_"] button div {
    text-align: left !important;
}

/* AI Sales Coach: floating launcher + panel. New accent purple - #8b5cf6 -
   introduced specifically for this one conversational feature (there is no
   existing purple anywhere else in this app to reuse), so it reads as a
   deliberate accent, not an accidental color. */
:root { --coach-accent: #8b5cf6; }
/* Wrapper is the one position:fixed anchor for both the button and the
   status dot (see coach-pulse-dot below) - anchoring the dot to this same
   box, instead of giving it its own independent fixed coordinates, is what
   keeps it visually attached to the icon's corner regardless of any future
   icon-size tweak. */
[class*="st-key-coach_launcher_wrap"] {
    position: fixed !important;
    /* Raised from 22px so Streamlit Cloud's own "Manage app" toolbar
       (bottom-right, platform-level, can't be removed/edited) never
       overlaps this icon - every other fixed-position launcher/panel
       below is shifted up by this same +56px so their relative spacing
       to each other is unchanged, only their clearance from the bottom
       edge changed. */
    bottom: 78px;
    right: 22px;
    z-index: 999999;
    width: 58px;
    height: 58px;
}
[class*="st-key-coach_toggle_btn"] {
    position: absolute !important;
    inset: 0;
    width: 58px;
}
[class*="st-key-coach_toggle_btn"] button {
    width: 58px !important;
    height: 58px !important;
    border-radius: 0 !important;
    background: transparent !important;
    border: none !important;
    box-shadow: none !important;
    font-size: 26px !important;
    color: #ffffff !important;
}
[class*="st-key-coach_toggle_btn"] button:hover {
    filter: drop-shadow(0 0 5px rgba(139,92,246,0.55)) drop-shadow(0 0 7px rgba(236,72,153,0.5)) brightness(1.08);
}

/* Feature 1 — 30-Minute Work Plan: floating hourglass launcher, stacked
   directly above the chat launcher (same fixed-position pattern, same
   accent) - a separate button, not merged with the chat icon. Custom
   CSS/SVG animation, no video/gif asset. */
[class*="st-key-workplan_toggle_btn"] {
    position: fixed !important;
    bottom: 144px;
    right: 22px;
    z-index: 999999;
    width: 58px;
}
[class*="st-key-workplan_toggle_btn"] button {
    width: 58px !important;
    height: 58px !important;
    border-radius: 0 !important;
    background: transparent !important;
    border: none !important;
    box-shadow: none !important;
    padding: 0 !important;
}
[class*="st-key-workplan_toggle_btn"] button:hover { filter: brightness(1.2); }
[class*="st-key-workplan_toggle_btn"] button p { visibility: hidden !important; }

/* Launcher-icon-only gradient - recolored from the earlier electric-blue/
   emerald palette to match the app-wide purple->magenta rebrand
   (var(--atliq-gradient)'s own #8B5CF6/#EC4899), so these two floating
   icons no longer stand out as unstyled/off-theme. Deliberately still NOT
   --coach-accent (#8b5cf6 flat), which stays untouched everywhere else
   (chat bubbles, timeline dots, workplan card badges, evidence badges) -
   only these two floating launcher icons use this gradient variable. */
:root { --icon-gradient-start: #8B5CF6; --icon-gradient-end: #EC4899; }
.hourglass-icon-wrap {
    position: fixed;
    bottom: 144px;
    right: 22px;
    width: 58px;
    height: 58px;
    z-index: 1000001;
    pointer-events: none;
    display: flex;
    align-items: center;
    justify-content: center;
    filter: drop-shadow(0 0 5px rgba(139,92,246,0.55)) drop-shadow(0 0 7px rgba(236,72,153,0.5));
}
/* Now a flat PNG (watch-glass-recolored.png) instead of hand-drawn SVG
   paths, so the old isolated "sand grain" element no longer exists to
   animate on its own - substituted with a gentle idle sway on the whole
   image so it doesn't read as fully static; disclosed as an interpretation
   change, not a literal preservation of the old grain-falling effect. */
.hourglass-frame {
    display: inline-block;
    transform-origin: 50% 50%;
    animation: hourglass-sway 2.6s ease-in-out infinite;
}
@keyframes hourglass-sway {
    0%, 100% { transform: rotate(0deg); }
    50%      { transform: rotate(4deg); }
}
.hourglass-icon-wrap.poured .hourglass-frame {
    animation: hourglass-flip 0.5s ease-in-out 1;
}
@keyframes hourglass-flip {
    0%   { transform: rotate(0deg); }
    65%  { transform: rotate(185deg); }
    100% { transform: rotate(180deg); }
}
@media (prefers-reduced-motion: reduce) {
    .hourglass-frame { animation: none; }
    .hourglass-icon-wrap.poured .hourglass-frame { animation: none; }
}

[class*="st-key-workplan_panel_container"] {
    position: fixed !important;
    bottom: 214px;
    right: 22px;
    z-index: 999997;
    width: 380px;
    max-height: 55vh;
    overflow-y: auto;
    background: #1c1f26;
    border: 1px solid rgba(255,255,255,0.10);
    border-radius: 16px;
    box-shadow: 0 10px 40px rgba(0,0,0,0.55);
    padding: 14px 16px 16px 16px;
}
.workplan-card {
    opacity: 0;
    transform: translateY(10px);
    animation: workplan-card-in 0.35s ease-out forwards;
}
@keyframes workplan-card-in {
    to { opacity: 1; transform: translateY(0); }
}
@media (prefers-reduced-motion: reduce) {
    .workplan-card { animation: none !important; opacity: 1 !important; transform: none !important; }
}

/* Status dot stays its own green (not the purple/magenta icon-gradient
   variable used for the two icons' surrounding glow) - kept green
   specifically per explicit instruction, independent of the icons'
   colors reverting to native asset colors above.
   Anchored (position:absolute) to [class*="st-key-coach_launcher_wrap"]
   (58x58, position:fixed) rather than its own independent fixed
   coordinates - the icon itself renders centered at 34x34 within that
   58x58 box (background-size: 34px 34px), a 12px inset on every side;
   offsetting the dot's own top/right by inset-minus-half-its-own-size
   (12px - 6px = 6px) centers it exactly on the icon's visible top-right
   corner, notification-badge style. */
.coach-pulse-dot {
    position: absolute;
    top: 6px;
    right: 6px;
    z-index: 1000000;
    width: 12px;
    height: 12px;
    border-radius: 50%;
    background: linear-gradient(135deg, #34D399 0%, #10B981 100%);
    border: 2px solid #14161a;
    box-shadow: 0 0 5px rgba(52,211,153,0.6), 0 0 7px rgba(16,185,129,0.5);
    animation: coach-pulse 2s ease-in-out infinite;
    pointer-events: none;
}
@keyframes coach-pulse { 0% { opacity: 1; } 50% { opacity: 0.35; } 100% { opacity: 1; } }
/* Fix 2: same gradient glow treatment on the chat icon itself (a raster
   PNG shown via background-image) so both launcher icons read as an
   obvious matched pair - drop-shadow follows the image's own alpha
   silhouette, so the artwork's own colors are untouched. */
[class*="st-key-coach_toggle_btn"] button {
    filter: drop-shadow(0 0 5px rgba(139,92,246,0.55)) drop-shadow(0 0 7px rgba(236,72,153,0.5));
}
@media (prefers-reduced-motion: reduce) {
    .coach-pulse-dot { animation: none; }
}
[class*="st-key-coach_panel_container"] {
    position: fixed !important;
    bottom: 148px;
    right: 22px;
    z-index: 999998;
    width: 380px;
    max-height: 65vh;
    overflow-y: auto;
    background: #1c1f26;
    border: 1px solid rgba(255,255,255,0.10);
    border-radius: 16px;
    box-shadow: 0 10px 40px rgba(0,0,0,0.55);
    padding: 14px 16px 16px 16px;
}
.coach-header {
    display: flex;
    align-items: center;
    gap: 10px;
    margin-bottom: 10px;
}
.coach-avatar {
    width: 34px;
    height: 34px;
    background: transparent;
    color: #ffffff;
    display: flex;
    align-items: center;
    justify-content: center;
    flex-shrink: 0;
    font-size: 18px;
}
.coach-avatar img {
    width: 100%;
    height: 100%;
    object-fit: contain;
}
.coach-bubble {
    border-radius: 12px;
    padding: 8px 12px;
    margin-bottom: 8px;
    font-size: 0.88rem;
    line-height: 1.4;
}
.coach-bubble.user {
    background: rgba(139,92,246,0.16);
    margin-left: 32px;
}
.coach-bubble.assistant {
    background: rgba(255,255,255,0.05);
    margin-right: 12px;
}

/* Feature 2 - Opportunity Timeline: connected dots, vertical (fits this
   single-column layout without horizontal scroll math), purple accent to
   match the rest of the dashboard's one conversational/temporal accent.
   Dot styling/colors/TODAY marker are unchanged.

   Fix 2: the connecting line is now ONE continuous element
   (.timeline-vline, absolutely positioned top:0/bottom:0 inside
   [class*="st-key-timeline_wrapper"]) instead of a separate short segment
   per item - it automatically stretches to whatever height the wrapper
   ends up with, including any expanded "Show evidence" content, so
   expanding one dot never breaks the line down to the next one. TODAY is
   rendered OUTSIDE this wrapper (as before, it never had a line below it). */
.timeline-item {
    position: relative;
    padding-left: 22px;
    padding-bottom: 10px;
    margin-bottom: 2px;
}
.timeline-dot {
    position: absolute;
    left: 0;
    top: 4px;
    width: 12px;
    height: 12px;
    border-radius: 50%;
    background: #8b5cf6;
    border: 2px solid #14161a;
}
.timeline-content {
    font-size: 0.9rem;
}
.timeline-today .timeline-dot {
    width: 14px;
    height: 14px;
    left: -1px;
    top: 3px;
    background: #22c55e;
    box-shadow: 0 0 0 3px rgba(34,197,94,0.25);
}
/* Fix 2 (rechecked): the single continuous connecting line - see comment
   above .timeline-item. left:5px/width:2px matches .timeline-dot's own
   left:0/12px-wide box (dot center and line center both land at x=6px).

   Root cause of it still breaking: Streamlit stamps its own
   position:relative on EVERY [data-testid="stElementContainer"] (each
   st.markdown/st.expander/st.columns call gets one). Since .timeline-vline
   is rendered via st.markdown, its own immediate stElementContainer parent
   -  not the outer st-key-timeline_wrapper further up - was the nearest
   positioned ancestor, so top:4px/bottom:4px was resolving against that
   single small element's own (zero-height) box, not the whole wrapper.
   Forcing every element container INSIDE the wrapper back to static
   makes st-key-timeline_wrapper itself the containing block again, so the
   line correctly stretches the full wrapper height regardless of how many
   "Show evidence" expanders are open inside it. */
[class*="st-key-timeline_wrapper"] {
    position: relative;
}
[class*="st-key-timeline_wrapper"] [data-testid="stElementContainer"],
[class*="st-key-timeline_wrapper"] [data-testid="stHorizontalBlock"],
[class*="st-key-timeline_wrapper"] [data-testid="stVerticalBlockBorderWrapper"] {
    position: static !important;
}
.timeline-vline {
    position: absolute;
    top: 4px;
    bottom: 4px;
    left: 5px;
    width: 2px;
    background: rgba(139,92,246,0.35);
    z-index: 0;
}
</style>
"""
st.markdown(CSS, unsafe_allow_html=True)

# Separate, small f-string block (kept apart from the large static CSS
# string above, which is full of literal {}'s) - renders the user-supplied
# actual image files as the launcher icon's background, replacing the
# earlier Material Symbols glyph. The purple accent stays as the circular
# background color; the image itself keeps its own original colors on top.
st.markdown(
    f"""
<style>
[class*="st-key-coach_toggle_btn"] button {{
    background-image: url('data:image/png;base64,{COACH_LAUNCHER_ICON_B64}') !important;
    background-repeat: no-repeat !important;
    background-position: center !important;
    background-size: 34px 34px !important;
}}
[class*="st-key-coach_toggle_btn"] button p {{
    visibility: hidden !important;
}}
</style>
""",
    unsafe_allow_html=True,
)


def badge_html(level: str) -> str:
    """level is 'red'/'yellow'/'green' (scoring.py's existing urgency vocabulary)."""
    badge = BADGE_LEVELS[URGENCY_TO_BADGE.get(level, "watch")]
    return (
        f'<span class="icon-badge" style="color:{badge["color"]};background:{badge["bg"]};">'
        f'<span class="msi">{badge["icon"]}</span>{badge["label"]}</span>'
    )


def badge_html_direct(badge_key: str) -> str:
    """badge_key is already one of 'urgent'/'check'/'watch'/'consider'."""
    badge = BADGE_LEVELS[badge_key]
    return (
        f'<span class="icon-badge" style="color:{badge["color"]};background:{badge["bg"]};">'
        f'<span class="msi">{badge["icon"]}</span>{badge["label"]}</span>'
    )


def category_tag_html(card_key: str) -> str:
    """A small icon+label chip for one of CATEGORIES, reusing the icon-badge style."""
    meta = CATEGORIES_BY_KEY[card_key]
    return (
        f'<span class="icon-badge" style="color:{meta["color"]};background:{meta["bg"]};">'
        f'<span class="msi">{meta["icon"]}</span>{meta["label"]}</span>'
    )


# --------------------------------------------------------------------------
# Data loading (cached — pure function of the on-disk files)
# --------------------------------------------------------------------------
@st.cache_resource(show_spinner="Loading CRM, emails, and meeting notes...")
def load_everything():
    crm_df = load_crm()
    docs = load_all_docs()
    case_files, orphans = build_case_files(crm_df, docs)
    duplicate_pairs = find_duplicates(crm_df)
    return crm_df, case_files, orphans, duplicate_pairs


crm_df, case_files, orphans, duplicate_pairs = load_everything()
case_files_by_id = {cf.lead_id: cf for cf in case_files}
# Linked documents are known the moment linker.py runs - independent of
# whether a lead has been analyzed yet - so this covers every lead/orphan,
# not just analyzed ones (used by the carousel/drill-down doc viewer).
docs_by_key = {cf.lead_id: cf.docs for cf in case_files}
docs_by_key.update({"ORPHAN::" + o.company_guess: o.docs for o in orphans})


def evidence_html(quote: str, source_file: str) -> str:
    return f'<div class="atliq-evidence">&ldquo;{quote}&rdquo;</div><div class="atliq-source">Source: {source_file}</div>'


_DOC_TYPE_LABEL = {"email": "Email", "note": "Meeting note"}


def _ordinal_suffix(day: int) -> str:
    if 11 <= (day % 100) <= 13:
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")


def _format_ordinal_date(dt: datetime) -> str:
    """Fix 1: human-readable ordinal date - "14th June", not "14 Jun" or
    "06/06" - the single shared formatter for every date the app displays
    (Linked documents labels and Opportunity Timeline dots both call this)."""
    return f"{dt.day}{_ordinal_suffix(dt.day)} {dt.strftime('%B')}"


def _doc_short_label(d: Document) -> str:
    """"Email — 19th June" / "Meeting note — 3rd July" - friendlier than the raw filename."""
    try:
        day_month = _format_ordinal_date(datetime.strptime(d.date, "%Y-%m-%d"))
    except (ValueError, TypeError):
        day_month = d.date or "undated"
    return f"{_DOC_TYPE_LABEL.get(d.doc_type, d.doc_type.title())} — {day_month}"


def render_linked_documents(docs: list[Document]):
    """Part 3/4: the documents that fed a lead's analysis, with a read-only,
    visually distinct viewer for each one's original text - no new lookup
    needed, `docs` already *is* the linked set (linker.py populated it)."""
    st.markdown("##### Linked documents")
    if not docs:
        st.caption("No linked emails or meeting notes.")
        return
    for d in docs:
        with st.expander(f"{_doc_short_label(d)} ({d.filename})"):
            with st.container(border=True):
                st.markdown('<span class="source-doc-label">Original source — read-only</span>', unsafe_allow_html=True)
                st.text(d.raw_text)


def _findings_by_source_file(lead_analysis: LeadAnalysis | None) -> dict[str, list[dict]]:
    """Full finding detail (not just a summary string, and not
    all_individual_findings(), which drops source_file) grouped by source
    document. This now powers each timeline dot's 'Show evidence' expander
    - the CRM-says/quote/source content the old standalone Findings section
    used to show, per event, so nothing is lost when that section is
    removed. No new AI call."""
    by_file: dict[str, list[dict]] = {}
    if not lead_analysis:
        return by_file
    for m in lead_analysis.mismatches:
        by_file.setdefault(m["source_file"], []).append({
            "category": "Mismatch", "summary": m["summary"], "crm_says": m.get("crm_says"),
            "evidence_quote": m["evidence_quote"], "source_file": m["source_file"],
        })
    for f in lead_analysis.missing_info_flags:
        by_file.setdefault(f["source_file"], []).append({
            "category": "Missing info", "summary": f"{f['summary']} (missing: {f['missing_field']})", "crm_says": None,
            "evidence_quote": f["evidence_quote"], "source_file": f["source_file"],
        })
    for t in lead_analysis.timing_signals:
        by_file.setdefault(t["source_file"], []).append({
            "category": "Timing", "summary": f"{t['summary']} ({t['estimated_date_or_window']})", "crm_says": None,
            "evidence_quote": t["evidence_quote"], "source_file": t["source_file"],
        })
    for q in lead_analysis.open_questions_or_dropped_commitments:
        label = "Unanswered question" if q["kind"] == "unanswered_question" else "Dropped commitment"
        by_file.setdefault(q["source_file"], []).append({
            "category": label, "summary": q["summary"], "crm_says": None,
            "evidence_quote": q["evidence_quote"], "source_file": q["source_file"],
        })
    for d in lead_analysis.deprioritize_signals:
        signal_label = DEPRIORITIZE_SIGNAL_LABELS.get(d["signal_type"], d["signal_type"])
        by_file.setdefault(d["source_file"], []).append({
            "category": signal_label, "summary": d["summary"], "crm_says": None,
            "evidence_quote": d["evidence_quote"], "source_file": d["source_file"],
        })
    return by_file


def render_opportunity_timeline(docs: list[Document], lead_analysis: LeadAnalysis | None, status_label: str) -> None:
    """Feature 2: a chronological connected-dot timeline built entirely from
    data already on hand - each linked document's real date/title (from
    data_loader.py), enriched with any findings sourced from that document
    (matched by source_file), plus a final 'TODAY' dot with the lead's
    current status. Free version only - no new AI call (the optional
    AI-cleaned-label add-on was flagged to the user, not built, pending
    their confirmation).

    Each dot with related findings gets its own "Show evidence" expander -
    the CRM-says/quote/source detail that used to live in a separate,
    duplicated Findings section (now removed; see 'Drill into a lead').

    Fix 2: all the doc dots (not TODAY) render inside one st.container
    (position:relative) with a single continuous connecting line
    (.timeline-vline, absolutely positioned top:0/bottom:0) - it stretches
    to match however tall the container gets, so expanding a "Show
    evidence" block never leaves a gap in the line down to the next dot.
    The expander itself is indented into a spacer/content column split
    (not a full-width row) so it reads as belonging to its own dot, not as
    a new step in the timeline. TODAY renders after the container closes,
    same as before it never had a line below it.
    """
    if not docs:
        return
    findings_by_file = _findings_by_source_file(lead_analysis)
    sorted_docs = sorted(docs, key=lambda d: d.date or "")

    st.markdown("##### Opportunity Timeline")
    with st.container(key="timeline_wrapper"):
        st.markdown('<div class="timeline-vline"></div>', unsafe_allow_html=True)
        for doc in sorted_docs:
            related = findings_by_file.get(doc.filename, [])
            detail = f" — {related[0]['summary']}" if related else ""
            st.markdown(
                f'<div class="timeline-item"><div class="timeline-dot"></div>'
                f'<div class="timeline-content"><b>{_doc_short_label(doc)}</b> — {doc.title}{detail}</div></div>',
                unsafe_allow_html=True,
            )
            if related:
                _evidence_spacer, evidence_col = st.columns([1, 14])
                with evidence_col:
                    with st.expander(f"Show evidence ({len(related)})"):
                        for f in related:
                            st.markdown(f"**{f['category']}:** {f['summary']}")
                            if f["crm_says"]:
                                st.caption(f"CRM says: {f['crm_says']}")
                            st.markdown(evidence_html(f["evidence_quote"], f["source_file"]), unsafe_allow_html=True)

    st.markdown(
        f'<div class="timeline-item timeline-today"><div class="timeline-dot"></div>'
        f'<div class="timeline-content"><b>TODAY</b> — Current status: {status_label}</div></div>',
        unsafe_allow_html=True,
    )


# --------------------------------------------------------------------------
# Sidebar — icon-only navigation rail. Everything else (branding, API key
# status, filters, Refresh) has moved to the main-page header/control row
# below, per "Sidebar Cleanup + Move Info/Controls to Main Page Header".
# --------------------------------------------------------------------------
# Left-sidebar tab navigation: swaps which section renders in the main area
# instead of one long scrolling page. Same session-state + st.rerun() pattern
# (and the same primary/secondary highlight) as the owner filter buttons
# further down, so the active tab is highlighted consistently with how
# selection already looks everywhere else in this app.
# Icon choices are a judgment call, not a literal 1:1 match to any fixed
# reference set - mapped for meaning against this app's actual 6 tabs:
# dashboard (Overview - 2x2 grid glyph, not a house), star (top-priority leads), category (the 6 finding
# categories), target (the Effort/Payoff quadrant chart), insights (AI
# noticing hidden signals), person_search (drilling into one lead's record).
TAB_DEFS = [
    ("attention", "This Week's Attention", "star"),
    ("categories", "Categories", "category"),
    ("effort_payoff", "Effort vs. Payoff", "target"),
    ("hidden_opportunity", "Hidden Opportunity", "insights"),
    ("drill_down", "Drill into a Lead", "person_search"),
    ("portfolio_insights", "Portfolio Insights", "bar_chart"),
    ("overview", "Overview", "dashboard"),  # 2x2 grid glyph (Material Symbols), not a house
]
st.session_state.setdefault("selected_tab", "attention")
with st.sidebar.container(key="nav_rail"):
    for _tab_key, _tab_label, _tab_icon in TAB_DEFS:
        if st.button(
            " ", key=f"navtab_{_tab_key}", icon=f":material/{_tab_icon}:", help=_tab_label,
            type="primary" if st.session_state["selected_tab"] == _tab_key else "secondary",
        ):
            st.session_state["selected_tab"] = _tab_key
            st.rerun()
selected_tab = st.session_state["selected_tab"]

env_key = os.environ.get("ANTHROPIC_API_KEY")

UNASSIGNED_OWNER_LABEL = "Unassigned"

owners = sorted({cf.crm_row["owner"] for cf in case_files if cf.crm_row.get("owner")})
# Fix 1: leads with a blank owner field get their own explicit bucket instead
# of being silently mishandled by the filter (see visible_case_file below) -
# only offered as an option if at least one lead is actually unassigned.
has_unassigned_owner = any(not cf.crm_row.get("owner") for cf in case_files)
owner_options = owners + ([UNASSIGNED_OWNER_LABEL] if has_unassigned_owner else [])
# V2 section 5: the actual selector widgets (buttons) render in the main
# header below - this just resolves the filter VALUE from session_state
# early, in the same session-state-driven-rerun pattern already used for
# category/carousel selection elsewhere in this file, so it's available to
# visible_case_file()/visible_analyses() below.
st.session_state.setdefault("selected_owner", "All")
if st.session_state["selected_owner"] not in (["All"] + owner_options):
    st.session_state["selected_owner"] = "All"
owner_filter = owner_options if st.session_state["selected_owner"] == "All" else [st.session_state["selected_owner"]]

# --------------------------------------------------------------------------
# Header — title+brain icon / description on the left, a stacked control
# column (API key status, filters, Refresh) on the right, in line with the
# title. Refresh calls the exact same run_clicked-triggered analysis logic
# just below, unchanged - only the layout moved.
# --------------------------------------------------------------------------
header_left, header_right = st.columns([2.4, 1.15])
with header_left:
    st.markdown(
        '<div style="display:flex;align-items:center;gap:14px;">'
        '<div class="brain-icon-wrap"><span class="msi brain-icon-glyph">neurology</span></div>'
        '<h1 style="margin:0;">AI Sales Command Center</h1>'
        '</div>',
        unsafe_allow_html=True,
    )
    st.caption(
        "A read-only memory layer under the CRM. It never edits the CRM, emails, or notes, and never sends anything. "
        "Loads the CRM, every email thread, and every meeting note — compares them, and shows what's inconsistent, "
        "missing, or time-sensitive. Nothing here is auto-applied to the CRM."
    )
with header_right:
    with st.container(key="header_controls_stack"):
        if env_key:
            # Small pill/badge (reusing the existing icon-badge + success color
            # already used elsewhere, e.g. EVIDENCE_LEVEL_COLORS["success"]) -
            # not a full-width st.success() block.
            st.markdown(
                '<span class="icon-badge" style="color:#15803d;background:rgba(21,128,61,0.14);">'
                '<span class="msi">check_circle</span>Using ANTHROPIC_API_KEY from .env</span>',
                unsafe_allow_html=True,
            )
            api_key = env_key
        else:
            api_key = st.text_input(
                "Anthropic API key", type="password", label_visibility="collapsed",
                placeholder="Anthropic API key", help="Only kept in this session's memory — never written to disk.",
            )
        include_orphans = st.checkbox("Include leads not in CRM", value=True)
        st.caption(f"Cache file: `{CACHE_PATH.name}` ({'exists' if CACHE_PATH.exists() else 'empty'})")
        # Local-only escape hatch: ALLOW_FORCE_REFRESH must be explicitly set
        # to "true" in .env (never in Streamlit Cloud's Secrets, never
        # hardcoded here) for this checkbox to do anything. Unset or any
        # other value -> disabled, exactly like the public deployment, so a
        # forced re-analysis (real paid API calls on already-cached leads)
        # can never ship enabled by accident.
        allow_force_refresh = os.environ.get("ALLOW_FORCE_REFRESH", "").strip().lower() == "true"
        force_refresh = st.checkbox(
            "Ignore cache", value=False, disabled=not allow_force_refresh,
            help=(
                "Force re-analysis of every eligible lead this run, even ones already cached — "
                "this re-bills the API for each one. Local-only (ALLOW_FORCE_REFRESH=true in .env)."
                if allow_force_refresh else
                "Disabled in public demo — Refresh still works, but only ever analyzes leads that aren't already cached."
            ),
        )
        run_clicked = st.button("Refresh", key="refresh_btn", type="primary")

# V2 section 5: owner filter as clearly-labeled pill buttons - reuses the
# exact owner_filter value computed above (and thus every downstream use of
# it: visible_case_file, visible_analyses, the Effort vs. Payoff chart), just
# changes the control. Same session-state + st.rerun() pattern as the
# category cards below.
owner_button_cols = st.columns(len(owner_options) + 1)
with owner_button_cols[0]:
    if st.button("All owners", key="owner_btn_all", type="primary" if st.session_state["selected_owner"] == "All" else "secondary", use_container_width=True):
        st.session_state["selected_owner"] = "All"
        st.rerun()
for i, owner_name in enumerate(owner_options):
    with owner_button_cols[i + 1]:
        if st.button(owner_name, key=f"owner_btn_{owner_name}", type="primary" if st.session_state["selected_owner"] == owner_name else "secondary", use_container_width=True):
            st.session_state["selected_owner"] = owner_name
            st.rerun()

st.divider()


# --------------------------------------------------------------------------
# Run analysis (LLM calls) — only on button click, cached across reruns
# --------------------------------------------------------------------------
def analyzable_items() -> list[CaseFile | OrphanLead]:
    items = [cf for cf in case_files if cf.docs]
    if include_orphans:
        items += [o for o in orphans if o.docs]
    return items


def _describe_exception(e: Exception) -> str:
    """A short, human-readable first line for a run error (status code / type)."""
    if isinstance(e, APIStatusError):
        return f"API error (HTTP {e.status_code}): {getattr(e, 'message', str(e))}"
    return f"{type(e).__name__}: {e}"


if run_clicked:
    # Reset diagnostics from any previous run immediately so stale results can't
    # be mistaken for this run's outcome.
    st.session_state["run_diagnostics"] = None

    # force_refresh is passed per-lead to get_or_analyze() below, which is
    # sufficient to force a fresh call for every lead in this run - no need
    # to also wipe the whole cache file (that would additionally destroy
    # every other lead's, and every past PROMPT_VERSION's, cached history).

    if not api_key:
        st.session_state["run_diagnostics"] = {
            "ok": False,
            "fatal": "No API key provided. Enter one at the top of the page, or set ANTHROPIC_API_KEY in .env.",
        }
    else:
        client = None
        try:
            client = get_client(api_key)
        except RuntimeError as e:
            st.session_state["run_diagnostics"] = {"ok": False, "fatal": str(e)}

        if client:
            items = analyzable_items()
            # Fix 3: real measured wall-clock time (not an estimate), start to
            # finish of the actual analysis loop below.
            run_start_time = time.time()
            results: dict[str, LeadAnalysis] = {}
            errors: list[dict] = []
            cache_hits = 0
            fresh_calls = 0
            forced_leads: list[str] = []
            shared_cache = load_cache()

            # This loop can fire dozens of large prompts back to back. Keep concurrency
            # low AND stagger submissions so we don't burst past a tokens-per-minute
            # limit before any 429 even has a chance to back off - a cache hit
            # (already-analyzed lead) still returns
            # near-instantly despite the stagger, since it never calls the API.
            MAX_WORKERS = 2
            SUBMIT_STAGGER_SECONDS = 1.2

            # V2 section 10: a spinner (animated, purely visual) wraps the run
            # in addition to the existing progress bar's numeric N/M text.
            with st.spinner(f"Running analysis on {len(items)} lead(s)..."):
                progress = st.progress(0.0, text=f"Analyzing 0/{len(items)} leads...")
                try:
                    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
                        futures = {}
                        for i, item in enumerate(items):
                            if i > 0:
                                time.sleep(SUBMIT_STAGGER_SECONDS)
                            futures[pool.submit(get_or_analyze, client, item, DEFAULT_MODEL, force_refresh, shared_cache)] = item

                        done = 0
                        for fut in as_completed(futures):
                            item = futures[fut]
                            # Duck-typed, not isinstance(item, CaseFile) - st.cache_resource
                            # (load_everything, above) can hold CaseFile/OrphanLead instances
                            # across a Streamlit redeploy's in-process module reload, which
                            # leaves them as a *different* CaseFile class object than the one
                            # just re-imported here - isinstance then wrongly returns False for
                            # a real CaseFile and this line crashes trying item.company_guess
                            # (which only OrphanLead has). hasattr sidesteps class identity
                            # entirely, so it's correct regardless of which reload generation
                            # the object came from.
                            label = item.lead_id if hasattr(item, "lead_id") else item.company_guess
                            try:
                                analysis, from_cache = fut.result()
                                results[analysis.key] = analysis
                                if from_cache:
                                    cache_hits += 1
                                else:
                                    fresh_calls += 1
                                    if force_refresh:
                                        forced_leads.append(label)
                            except Exception as e:  # noqa: BLE001 - surface every failure, never swallow
                                errors.append({"label": label, "summary": _describe_exception(e), "traceback": traceback.format_exc()})
                            done += 1
                            progress.progress(done / len(items), text=f"Analyzing {done}/{len(items)} leads...")
                    save_cache(shared_cache)
                except Exception as e:  # noqa: BLE001 - a failure outside the per-lead loop (e.g. pool setup)
                    errors.append({"label": "(setup)", "summary": _describe_exception(e), "traceback": traceback.format_exc()})
                finally:
                    progress.empty()

            elapsed_seconds = time.time() - run_start_time

            st.session_state["analyses"] = results
            st.session_state["run_diagnostics"] = {
                "ok": not errors,
                "model": DEFAULT_MODEL,
                "attempted": len(items),
                "succeeded": len(results),
                "elapsed_seconds": elapsed_seconds,
                "cache_hits": cache_hits,
                "fresh_calls": fresh_calls,
                "forced_leads": forced_leads,
                "errors": errors,
            }

            # V2 section 2: Executive Summary - ONE extra Claude call per full
            # run, fed only these pre-computed numbers/top-leads (not raw
            # documents), and cached on disk keyed by a signature of this
            # run's own results, so an unchanged outcome (e.g. every lead
            # served from the per-lead cache) reuses the existing summary
            # instead of paying for a new call.
            def _category_counts(leads: list[LeadAnalysis]) -> dict:
                return {
                    "mismatches": sum(1 for a in leads if a.mismatches),
                    "missing_info": sum(1 for a in leads if a.missing_info_flags),
                    "followup_open_questions": sum(1 for a in leads if a.timing_signals or a.open_questions_or_dropped_commitments),
                    "duplicates": len(duplicate_pairs),
                    "not_in_crm": sum(1 for a in leads if a.is_orphan),
                    "consider_deprioritizing": sum(1 for a in leads if a.deprioritize_signals),
                }

            all_results = list(results.values())
            pipeline_stats = {"total_leads_analyzed": len(all_results), **_category_counts(all_results)}
            top_attention = rank_attention(all_results, duplicate_pairs, top_n=8)
            top_items = [
                {
                    "company": it.company,
                    "owner": (results[it.key].crm_row or {}).get("owner", "") if it.key in results else "",
                    "score": it.score,
                    "top_reason": it.reasons[0] if it.reasons else "elevated score",
                }
                for it in top_attention
            ]

            # "insights_fmt=bullets_v1" ensures a prior run's paragraph-format
            # cached summary (from before this bullet-point change) is never
            # served as-is - it naturally falls out of the cache and gets
            # regenerated once under the new format, same as any other digest
            # change would trigger. Not a re-analysis of the 43 leads.
            signature_parts = ["insights_fmt=bullets_v1"] + sorted(f"{it.key}:{it.score}" for it in top_attention) + [f"{k}:{v}" for k, v in sorted(pipeline_stats.items())]
            signature = hashlib.sha256("|".join(signature_parts).encode("utf-8")).hexdigest()

            insights_cache = load_insights_cache()
            st.session_state["exec_summary_error"] = None
            if signature in insights_cache:
                st.session_state["exec_summary"] = insights_cache[signature]
            else:
                with st.spinner("Generating executive summary..."):
                    try:
                        exec_summary = generate_executive_summary(client, pipeline_stats, top_items, model=DEFAULT_MODEL)
                        insights_cache[signature] = exec_summary
                        save_insights_cache(insights_cache)
                        st.session_state["exec_summary"] = exec_summary
                    except Exception as e:  # noqa: BLE001 - never let this block the rest of the run
                        st.session_state["exec_summary_error"] = _describe_exception(e)

            # Part 3 (memory trail): record a compact snapshot of this run's
            # findings - not filtered by the sidebar's owner/status/orphan
            # toggles, since the trail should reflect what analysis actually
            # found, independent of how it's currently being viewed.
            def _fp(company: str, category: str, basis: str) -> str:
                raw = f"{company}|{category}|{(basis or '')[:200]}"
                return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

            def _label(a: LeadAnalysis) -> str:
                return f"{a.company} ({a.lead_id})" if a.lead_id else f"{a.company} — not in CRM"

            snapshot_findings: list[dict] = []
            for a in results.values():
                if a.is_orphan:
                    continue
                label = _label(a)
                for m in a.mismatches:
                    snapshot_findings.append({"fp": _fp(label, "mismatches", m["evidence_quote"]), "company": label, "category": "mismatches", "summary": m["summary"][:140]})
                for f in a.missing_info_flags:
                    snapshot_findings.append({"fp": _fp(label, "missing_info", f["evidence_quote"]), "company": label, "category": "missing_info", "summary": f["summary"][:140]})
                for t in a.timing_signals:
                    snapshot_findings.append({"fp": _fp(label, "timing", t["evidence_quote"]), "company": label, "category": "timing", "summary": t["summary"][:140]})
                for q in a.open_questions_or_dropped_commitments:
                    snapshot_findings.append({"fp": _fp(label, "open_questions", q["evidence_quote"]), "company": label, "category": "open_questions", "summary": q["summary"][:140]})
                for d in a.deprioritize_signals:
                    snapshot_findings.append({"fp": _fp(label, "deprioritize", d["evidence_quote"]), "company": label, "category": "deprioritize", "summary": d["summary"][:140]})
            for p in duplicate_pairs:
                label = f"{p.company_a} ({p.lead_id_a}) ↔ {p.company_b} ({p.lead_id_b})"
                basis = "; ".join(p.reasons)
                snapshot_findings.append({"fp": _fp(label, "duplicates", basis), "company": label, "category": "duplicates", "summary": basis[:140]})
            for o in orphans:
                if "ORPHAN::" + o.company_guess in results:
                    snapshot_findings.append({"fp": _fp(o.company_guess, "not_in_crm", o.company_guess), "company": o.company_guess, "category": "not_in_crm", "summary": "Mentioned in emails/notes, not in CRM"})

            append_history_snapshot(datetime.now().isoformat(timespec="seconds"), snapshot_findings)

analyses: dict[str, LeadAnalysis] = st.session_state.get("analyses", {})

# Diagnostics panel — persists across reruns (tab switches, filter changes) until
# the next run, so a failure is never just a message that flashes and disappears.
def _format_elapsed(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f} seconds"
    minutes, rest = divmod(seconds, 60)
    return f"{int(minutes)}m {rest:.0f}s"


diag = st.session_state.get("run_diagnostics")
if diag:
    if diag.get("fatal"):
        st.error(f"Analysis could not start: {diag['fatal']}")
    elif diag["ok"]:
        # Fix 3: time-saved/speed indicator - real measured elapsed time for
        # this run, shown right in the existing top-of-dashboard banner.
        elapsed_text = f" in {_format_elapsed(diag['elapsed_seconds'])}" if "elapsed_seconds" in diag else ""
        st.success(
            f"Analyzed {diag['succeeded']}/{diag['attempted']} leads using `{diag['model']}`{elapsed_text} "
            f"({diag.get('cache_hits', 0)} from cache, {diag.get('fresh_calls', 0)} fresh API calls)."
        )
    else:
        elapsed_text = f" in {_format_elapsed(diag['elapsed_seconds'])}" if "elapsed_seconds" in diag else ""
        st.error(
            f"{len(diag['errors'])} of {diag['attempted']} leads failed to analyze (model: `{diag['model']}`{elapsed_text}). "
            f"{diag['succeeded']} succeeded ({diag.get('cache_hits', 0)} from cache, {diag.get('fresh_calls', 0)} fresh API calls). "
            f"Click **Refresh** again — leads that already succeeded are cached and won't be re-billed; only the failed ones below will be retried."
        )
        with st.expander("Show error details", expanded=True):
            for err in diag["errors"]:
                st.markdown(f"**{err['label']}** — {err['summary']}")
                with st.expander("Full traceback", expanded=False):
                    st.code(err["traceback"])
    if diag.get("forced_leads"):
        # Local-only (ALLOW_FORCE_REFRESH=true) - visible every time "Ignore
        # cache" actually forced real, billable API calls on leads that were
        # already cached, so this can never happen silently. Cost basis is
        # the measured ~$0.0698/lead figure used elsewhere this session.
        _n_forced = len(diag["forced_leads"])
        _est_cost = _n_forced * 0.0698
        st.warning(
            f"⚠️ **Ignore cache** was ON: {_n_forced} lead(s) were force re-analyzed even though already cached "
            f"(~$0.0698/lead ≈ ${_est_cost:.2f}) — {', '.join(diag['forced_leads'])}"
        )
elif not api_key:
    st.warning("No Anthropic API key detected yet (checked `.env` and the field at the top of the page). Analysis tabs will stay empty until one is provided.")


# --------------------------------------------------------------------------
# Debug panel — always visible, regardless of whether a run just happened.
# Shows exactly how many leads were eligible, how many actually got analyzed
# this session, and per-category raw-vs-grounded counts, so "empty" can be
# told apart from "ran but found nothing" vs "ran but got filtered out".
# --------------------------------------------------------------------------
FINDING_CATEGORIES = ["mismatches", "missing_info_flags", "timing_signals", "open_questions_or_dropped_commitments", "deprioritize_signals"]

# Reachable from the Overview tab (not shown on every tab) - collapsed by
# default either way.
if selected_tab == "overview":
    with st.expander("Debug: analysis pipeline internals", expanded=False):
        eligible_items = analyzable_items()
        # Duck-typed - see the matching comment at the label-computation line above.
        eligible_keys = {(item.lead_id if hasattr(item, "lead_id") else "ORPHAN::" + item.company_guess) for item in eligible_items}
        analyzed_keys = set(analyses.keys())

        st.markdown(
            f"- CRM leads total: **{len(case_files)}**\n"
            f"- Leads/orphans eligible for LLM analysis (have ≥1 linked doc): **{len(eligible_items)}**\n"
            f"- Leads with an analysis result in this session: **{len(analyses)}**\n"
            f"- Eligible items with NO result yet (never run, or errored last run): **{len(eligible_keys - analyzed_keys)}**"
        )
        missing = eligible_keys - analyzed_keys
        if missing:
            st.caption("Not yet analyzed / failed: " + ", ".join(sorted(missing)))

        if not analyses:
            st.info("No analyses in session state yet — click **Refresh** at the top of the page, then re-check this panel.")
        else:
            totals = {cat: {"raw": 0, "passed": 0} for cat in FINDING_CATEGORIES}
            rows = []
            for a in analyses.values():
                row = {"lead": a.company + (f" ({a.lead_id})" if a.lead_id else " (not in CRM)")}
                for cat in FINDING_CATEGORIES:
                    passed = len(getattr(a, cat))
                    dropped = len([d for d in a.dropped_findings if d["category"] == cat])
                    raw = passed + dropped
                    totals[cat]["raw"] += raw
                    totals[cat]["passed"] += passed
                    row[f"{cat} (passed/raw)"] = f"{passed}/{raw}"
                rows.append(row)

            # Raw INDIVIDUAL FINDINGS (not deduplicated by lead, and includes
            # orphan/not-in-CRM leads) - intentionally a different unit than
            # the Categories page's card counts, which dedupe to distinct
            # non-orphan leads. A lead with 3 mismatches contributes 3 here.
            st.markdown("**Totals across all analyzed leads — raw individual findings, not deduplicated by lead (passed the grounding check / raw count returned by the model):**")
            st.markdown(
                "\n".join(
                    f"- `{cat}`: {totals[cat]['passed']} finding(s) passed / {totals[cat]['raw']} finding(s) raw "
                    f"({totals[cat]['raw'] - totals[cat]['passed']} dropped for failing the verbatim-quote check)"
                    for cat in FINDING_CATEGORIES
                )
            )
            st.caption("Note: these are raw finding counts, not distinct leads - the Categories page's card counts dedupe to \"N leads with ≥1 finding\" and exclude orphan/not-in-CRM leads, so the two will legitimately differ.")
            st.markdown("**Per-lead breakdown (finding counts per lead, passed/raw):**")
            _debug_df = pd.DataFrame(rows)
            _passed_raw_cols = [c for c in _debug_df.columns if c != "lead"]

            def _row_band(row):
                bg = "rgba(255,255,255,0.03)" if row.name % 2 == 1 else ""
                return [f"background-color: {bg};" if bg else "" for _ in row]

            def _passed_raw_style(val: str) -> str:
                # Reuses EVIDENCE_LEVEL_COLORS (already used for the
                # urgent/check badges and the memory-trail "resolved" green)
                # instead of introducing a new color set.
                if not isinstance(val, str) or "/" not in val:
                    return "text-align: right;"
                try:
                    passed, raw = (int(x) for x in val.split("/"))
                except ValueError:
                    return "text-align: right;"
                if raw == 0:
                    return "text-align: right;"
                level = "success" if passed == raw else "warning"
                color, bg = EVIDENCE_LEVEL_COLORS[level]
                return f"text-align: right; color: {color}; background-color: {bg}; font-weight: 600;"

            st.dataframe(
                _debug_df.style.apply(_row_band, axis=1).map(_passed_raw_style, subset=_passed_raw_cols),
                use_container_width=True,
            )


def visible_case_file(cf: CaseFile) -> bool:
    row = cf.crm_row
    # Fix 1: a blank owner is its own explicit "Unassigned" bucket, not a
    # free pass through every filter - previously, `row.get("owner")` being
    # falsy short-circuited this check entirely, so unassigned leads showed
    # up under every single-owner selection instead of only "All"/"Unassigned".
    effective_owner = row.get("owner") or UNASSIGNED_OWNER_LABEL
    if owner_filter and effective_owner not in owner_filter:
        return False
    return True


visible_lead_ids = {cf.lead_id for cf in case_files if visible_case_file(cf)}

# Duplicates are rule-based (linker.py/duplicates.py), so this doesn't depend on
# whether any LLM analysis has run yet - unlike the finding categories below.
duplicate_pairs_visible = [p for p in duplicate_pairs if p.lead_id_a in visible_lead_ids or p.lead_id_b in visible_lead_ids]


def visible_analyses() -> list[LeadAnalysis]:
    out = []
    for a in analyses.values():
        if a.is_orphan:
            if include_orphans:
                out.append(a)
        elif a.lead_id in visible_lead_ids:
            out.append(a)
    return out


# --------------------------------------------------------------------------
# Weekly Attention Summary (title/caption/owner-buttons now live in the
# header/control-row section above, right after the sidebar).
# --------------------------------------------------------------------------

def compute_memory_trail(history: list[dict]) -> dict | None:
    """Client-based memory trail (Fix C): the 40 CRM clients are the primary
    entity, so "new/resolved/persisting" are reported as distinct clients,
    not raw finding-fingerprint counts - grouping the existing per-finding
    history entries by their "company" field."""
    if len(history) < 2:
        return None
    current, previous = history[-1], history[-2]
    current_map = {f["fp"]: f for f in current["findings"]}
    previous_map = {f["fp"]: f for f in previous["findings"]}
    current_fps, previous_fps = set(current_map), set(previous_map)

    def streak(fp: str) -> int:
        count = 0
        for run in reversed(history):
            if fp in {f["fp"] for f in run["findings"]}:
                count += 1
            else:
                break
        return count

    new_fps = current_fps - previous_fps
    resolved_fps = previous_fps - current_fps
    persisting_fps = current_fps & previous_fps

    def group_by_client(fps: set[str], source_map: dict) -> dict[str, list[dict]]:
        by_company: dict[str, list[dict]] = {}
        for fp in fps:
            item = source_map[fp]
            by_company.setdefault(item["company"], []).append(item)
        return by_company

    new_by_client = group_by_client(new_fps, current_map)
    resolved_by_client = group_by_client(resolved_fps, previous_map)
    persisting_by_client = group_by_client(persisting_fps, current_map)

    persisting_clients = [
        {"company": company, "count": len(items), "streak": max(streak(i["fp"]) for i in items)}
        for company, items in persisting_by_client.items()
    ]
    persisting_clients.sort(key=lambda x: -x["streak"])

    return {
        "current_timestamp": current["timestamp"],
        "previous_timestamp": previous["timestamp"],
        "new_by_client": new_by_client,
        "resolved_by_client": resolved_by_client,
        "persisting_clients": persisting_clients,
    }


# --------------------------------------------------------------------------
# Part 3 — Memory trail: "Since your last check". Pure comparison of stored
# history snapshots, no LLM calls involved. Reordered to render above
# Executive Summary - see comment on that section below.
# --------------------------------------------------------------------------
# _history/_trail computed unconditionally (not just on the Overview tab) -
# the Dashboard Assistant's "What's changed since my last check?" insight
# (_build_insight_dispatch below) reads _trail too, and that chatbot is also
# reachable from the This Week's Attention tab.
_history = load_history()
_trail = compute_memory_trail(_history)

if selected_tab == "overview":
    st.subheader("Since your last check")
    if _trail is None:
        st.info("First check — memory trail will build up from your next analysis." if len(_history) <= 1 else
                "Need at least two completed runs to compare — run the analysis again to start the trail.")
    else:
        st.caption(f"Comparing this check ({_trail['current_timestamp']}) to your previous one ({_trail['previous_timestamp']}).")
        n_new = len(_trail["new_by_client"])
        n_resolved = len(_trail["resolved_by_client"])
        n_persisting = len(_trail["persisting_clients"])
        stat_cols = st.columns(3)
        with stat_cols[0]:
            st.markdown(
                f'<span class="icon-badge" style="color:#14b8a6;background:rgba(20,184,166,0.14);">'
                f'<span class="msi">fiber_new</span>{n_new} client{"s" if n_new != 1 else ""} have new issues</span>', unsafe_allow_html=True,
            )
        with stat_cols[1]:
            st.markdown(
                f'<span class="icon-badge" style="color:#15803d;background:rgba(21,128,61,0.14);">'
                f'<span class="msi">task_alt</span>{n_resolved} client{"s" if n_resolved != 1 else ""}\' issues resolved</span>', unsafe_allow_html=True,
            )
        with stat_cols[2]:
            st.markdown(
                f'<span class="icon-badge" style="color:#92400e;background:rgba(180,83,9,0.14);">'
                f'<span class="msi">history</span>{n_persisting} client{"s" if n_persisting != 1 else ""} have persisting issues</span>', unsafe_allow_html=True,
            )

        if _trail["persisting_clients"]:
            top_persisting = _trail["persisting_clients"][:5]
            with st.expander(f"Clients with persisting issues (top {len(top_persisting)} of {n_persisting}, by how long they've lingered)"):
                for c in top_persisting:
                    plural = "issue" if c["count"] == 1 else "issues"
                    st.markdown(f"- **{c['company']}** — {c['count']} persisting {plural} _(flagged for {c['streak']} consecutive checks)_")
        if _trail["new_by_client"]:
            with st.expander(f"Clients with new issues since last check ({n_new})"):
                for company, items in sorted(_trail["new_by_client"].items()):
                    plural = "issue" if len(items) == 1 else "issues"
                    st.markdown(f"- **{company}** — {len(items)} new {plural}: {items[0]['summary']}")
        if _trail["resolved_by_client"]:
            with st.expander(f"Clients whose issues were resolved since last check ({n_resolved})"):
                for company, items in sorted(_trail["resolved_by_client"].items()):
                    plural = "issue" if len(items) == 1 else "issues"
                    st.markdown(f"- **{company}** — {len(items)} {plural} resolved: {items[0]['summary']}")


# V2 section 2: the full bullet-point Executive Summary panel that used to
# render here on the Overview tab has moved to the new Portfolio Insights
# tab (condensed to its first 2 bullets, same cached data, no new AI call -
# see "if selected_tab == portfolio_insights" further down). "Since your
# last check" directly above this comment is untouched.
# --------------------------------------------------------------------------
# Feature 1 — 30-Minute Work Plan: filtered by the currently-selected owner
# (the same buttons above), using only the already-computed Urgency Score
# and suggested_next_action text - no new AI call, no new scoring logic.
# Rendered on demand from the floating hourglass launcher (see the bottom
# of this file), not inline here - this is just the shared render function.
# --------------------------------------------------------------------------
_QUICK_ACTION_KEYWORDS = ("reply", "confirm", "answer", "acknowledge", "check in", "schedule a call", "follow up")
_BIG_ACTION_KEYWORDS = (
    "questionnaire", "proposal", "statement of work", " sow", "case stud", "tco comparison",
    "documentation", "board", "quote", "vendor security",
)


def _estimate_task_minutes(suggested_action: str, has_draft: bool) -> tuple[int, bool]:
    """Heuristic only, over text already generated - never a new AI call.
    Returns (minutes, too_big_for_this_plan)."""
    if has_draft:
        return 5, False
    text = suggested_action.lower()
    if any(k in text for k in _BIG_ACTION_KEYWORDS):
        return 30, True
    if any(k in text for k in _QUICK_ACTION_KEYWORDS):
        return 10, False
    return 15, False


# Fix 1: restructure the single suggested_next_action sentence into short
# bullets - who/what, the reference/evidence, and the deadline/reason it's
# time-sensitive - purely by splitting on the sentence's own existing
# clause boundaries and labeling each by keyword. Same content, no new AI
# call, matching the bullet style already used in the chatbot's answers.
_TIMELINE_DATE_RE = re.compile(
    r"\b(\d{4}-\d{2}-\d{2}|\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*|mid-\w+|end-of-\w+|early \w+|the \d+(?:st|nd|rd|th))\b",
    re.I,
)
_DEADLINE_CLAUSE_KEYWORDS = ("by ", "before ", "ahead of", "deadline", "due ", "goes on leave")
_REFERENCE_CLAUSE_KEYWORDS = ("email", "call", "note", "meeting", "message", "conversation", "thread", "card")


def _split_action_into_bullets(action_text: str, max_bullets: int = 3) -> list[str]:
    clauses = [c.strip().rstrip(".") for c in re.split(r",\s+|;\s+", action_text) if c.strip()]
    if not clauses:
        return [f"<b>Action:</b> {action_text}"]

    bullets = [f"<b>Action:</b> {clauses[0]}."]
    for clause in clauses[1:]:
        cl = clause.lower()
        if _TIMELINE_DATE_RE.search(clause) and any(k in cl for k in _DEADLINE_CLAUSE_KEYWORDS):
            bullets.append(f"<b>Why time-sensitive:</b> {clause}.")
        elif any(k in cl for k in _REFERENCE_CLAUSE_KEYWORDS):
            bullets.append(f"<b>Reference:</b> {clause}.")
        else:
            bullets.append(f"<b>Also:</b> {clause}.")
        if len(bullets) >= max_bullets:
            break
    leftover = clauses[len(bullets):]
    if leftover:
        bullets[-1] = bullets[-1][:-1] + "; " + "; ".join(leftover) + "."
    return bullets


def render_thirty_minute_plan() -> None:
    if st.session_state["selected_owner"] == "All":
        st.info("Pick an owner above to see a focused 30-minute work plan.")
        return
    if not analyses:
        st.info("Click **Refresh** at the top of the page first.")
        return

    plan_items = []
    for wa_item in rank_attention(visible_analyses(), duplicate_pairs_visible, top_n=1000):
        wa_lead = analyses.get(wa_item.key)
        if not wa_lead or not wa_lead.suggested_next_action:
            continue
        has_draft = bool(st.session_state.get(f"followup_email_card_{wa_item.key}", {}).get("draft"))
        minutes, too_big = _estimate_task_minutes(wa_lead.suggested_next_action, has_draft)
        if too_big:
            continue
        plan_items.append((wa_item, minutes, wa_lead.suggested_next_action, has_draft))
        if len(plan_items) == 3:
            break

    if not plan_items:
        st.caption(f"No quick wins found for {st.session_state['selected_owner']} right now - every open action looks bigger than 30 minutes.")
        return

    for i, (wa_item, minutes, action_text, has_draft) in enumerate(plan_items):
        wa_tag = f" ({wa_item.lead_id})" if wa_item.lead_id else " (not in CRM)"
        if has_draft:
            bullet_items = [f"<b>Action:</b> {action_text}", "<b>Status:</b> draft ready — just review &amp; send"]
        else:
            bullet_items = _split_action_into_bullets(action_text)
        bullets_html = "".join(f"<li style='margin-bottom:4px;'>{b}</li>" for b in bullet_items)
        # Staggered reveal: each card's CSS animation-delay increases by
        # ~180ms, so they settle into place one at a time rather than all
        # appearing instantly - purely a CSS entrance animation on mount,
        # replayed fresh every time Streamlit redraws this (e.g. on open).
        st.markdown(
            f'<div class="atliq-card workplan-card" style="animation-delay:{i * 180}ms;">'
            f'<span class="msi" style="font-size:20px;color:#8b5cf6;vertical-align:middle;">hourglass_top</span> '
            f'<b>{wa_item.company}{wa_tag}</b>'
            f'<span class="icon-badge" style="color:#8b5cf6;background:rgba(139,92,246,0.14);margin-left:8px;">~{minutes} min</span>'
            f"<ul style='margin:6px 0 0 0;padding-left:1.2em;'>{bullets_html}</ul>"
            f'</div>',
            unsafe_allow_html=True,
        )


def _short_reason_label(r: dict) -> str:
    """Compact descriptor for the score-breakdown line (distinct from the full
    one-sentence finding text used for the card's primary line)."""
    cat, pts = r["category"], r["points"]
    if cat == "mismatch":
        sev = "high" if pts >= 3 else "medium" if pts >= 1.5 else "low"
        return f"{sev}-severity mismatch"
    if cat == "timing":
        return "due-now timing" if pts >= 3 else "due-this-month timing"
    if cat == "open_question":
        return "open question(s)"
    if cat == "missing_info":
        return "missing info"
    if cat == "duplicate":
        return "likely duplicate"
    return r["text"]  # deal-size / staleness context reasons are already short


def _primary_and_rest(item: AttentionItem) -> tuple[dict | None, list[dict]]:
    """The single highest-severity finding, plus everything else, from
    scoring.py's reason_details - shared by every lead view (Weekly Attention,
    category detail, view-all detail) so there's exactly one selection rule."""
    finding_reasons = [r for r in item.reason_details if r["category"] is not None]
    primary = finding_reasons[0] if finding_reasons else None
    other_reasons = [r for r in item.reason_details if r is not primary]
    return primary, other_reasons


def all_individual_findings(lead_analysis: LeadAnalysis) -> list[dict]:
    """Fix 4 (consolidation): every individual finding for one lead, across
    all five categories, with its real content - not scoring.py's
    reason_details, which uses a count-only summary for open_questions/
    missing_info (e.g. "2 open question(s)") and omits deprioritize_signals
    entirely (they're deliberately not part of the urgency score). This is
    the complete picture for that lead, independent of how it was reached."""
    items: list[dict] = []
    for m in lead_analysis.mismatches:
        items.append({"category": "mismatches", "text": f'Mismatch: {m["summary"]}'})
    for f in lead_analysis.missing_info_flags:
        items.append({"category": "missing_info", "text": f'{f["summary"]} (missing: {f["missing_field"]})'})
    for t in lead_analysis.timing_signals:
        items.append({"category": "timing", "text": f'{t["summary"]} ({t["estimated_date_or_window"]})'})
    for q in lead_analysis.open_questions_or_dropped_commitments:
        kind_label = "Unanswered question" if q["kind"] == "unanswered_question" else "Dropped commitment"
        items.append({"category": "open_questions", "text": f'{kind_label}: {q["summary"]}'})
    for d in lead_analysis.deprioritize_signals:
        signal_label = DEPRIORITIZE_SIGNAL_LABELS.get(d["signal_type"], d["signal_type"])
        items.append({"category": "deprioritize", "text": f'{signal_label}: {d["summary"]}'})
    return items


def duplicate_note_for_lead(lead_id: str | None) -> dict | None:
    """Fix 4: if this lead is one half of a likely-duplicate CRM pair, name
    the specific other company (not just a generic "likely duplicate" flag),
    so the relationship is visible from either twin's own record."""
    if not lead_id:
        return None
    for p in duplicate_pairs:
        if p.lead_id_a == lead_id:
            return {"category": "duplicates", "text": f"Likely duplicate of {p.company_b} ({p.lead_id_b})"}
        if p.lead_id_b == lead_id:
            return {"category": "duplicates", "text": f"Likely duplicate of {p.company_a} ({p.lead_id_a})"}
    return None


def render_followup_email_section(lead_analysis: LeadAnalysis, dup_note: dict | None, key_suffix: str) -> None:
    """The one Gmail flow, used everywhere a lead's suggested action is
    shown (This Week's Attention, category detail views, and the
    drill-down) - not two different implementations. "Generate follow-up
    email (draft)" makes ONE Claude call, only when actually clicked, using
    that lead's real findings/suggested action - never part of a batch/
    full-analysis run. The resulting well-articulated draft (proper
    greeting, natural phrasing, sign-off) is shown in the dashboard, then
    "Open in Gmail" pre-fills that SAME draft in a new Gmail tab - nothing
    is ever sent automatically; the user reviews and sends it themselves.
    """
    email_findings = all_individual_findings(lead_analysis)
    if dup_note:
        email_findings = email_findings + [dup_note]
    email_key = f"followup_email_{key_suffix}"
    if st.button("Generate follow-up email (draft)", key=f"gen_email_btn_{key_suffix}"):
        if not api_key:
            st.session_state[email_key] = {"error": "No Anthropic API key provided. Enter one at the top of the page, or set ANTHROPIC_API_KEY in .env."}
        else:
            try:
                email_client = get_client(api_key)
                draft = generate_followup_email(email_client, lead_analysis.company, email_findings, lead_analysis.suggested_next_action, model=DEFAULT_MODEL)
                st.session_state[email_key] = {"draft": draft}
            except Exception as e:  # noqa: BLE001 - surface it, don't swallow it
                st.session_state[email_key] = {"error": _describe_exception(e)}

    stored_email = st.session_state.get(email_key)
    if stored_email:
        if stored_email.get("error"):
            st.error(f"Could not generate a draft: {stored_email['error']}")
        else:
            draft = stored_email["draft"]
            st.markdown(f"**Subject:** {draft['subject']}")
            st.text_area("Draft body (copy manually — nothing is sent automatically)", value=draft["body"], height=160, key=f"email_body_{key_suffix}")

            contact_email = (lead_analysis.crm_row or {}).get("contact_email", "").strip()
            if contact_email:
                gmail_url = (
                    "https://mail.google.com/mail/?view=cm&fs=1"
                    f"&to={quote(contact_email)}&su={quote(draft['subject'])}&body={quote(draft['body'])}"
                )
                st.link_button("Open in Gmail", gmail_url, icon=":material/mail:")
            else:
                st.link_button("Open in Gmail", "#", icon=":material/mail:", disabled=True)
                st.caption("No contact email on file — add one to enable this.")


def render_lead_card(item: AttentionItem, show_impact: bool = False):
    """The one shared lead-card layout, used identically in This Week's
    Attention, category detail views, and view-all detail views:
      [score circle]  Company name + urgency badge   <- same row
      Top finding (plain language, own line)          <- always visible
      Suggested action: ...                            <- always visible, labeled
      "Why did AI recommend this?" expander: score breakdown + every
      individual finding for this lead (all categories, real content, not
      just the current one) + linked docs - the single consolidated view
      Fix 4 requires.

    show_impact (section 6): "Potential impact" (High/Medium/Low) is only
    shown in This Week's Attention, per the spec - callers there pass True.
    """
    lead_tag = f" ({item.lead_id})" if item.lead_id else " (not in CRM)"
    display_score = min(round(item.score * 10), 100)
    badge = BADGE_LEVELS[URGENCY_TO_BADGE.get(item.urgency_level, "watch")]
    lead_analysis = analyses.get(item.key)
    evidence = evidence_completeness(lead_analysis) if lead_analysis else None
    findings = all_individual_findings(lead_analysis) if lead_analysis else []
    dup_note = duplicate_note_for_lead(item.lead_id)
    if dup_note:
        findings = findings + [dup_note]

    primary, _ = _primary_and_rest(item)
    raw_primary_category = primary["category"] if primary else None  # scoring.py's own vocab
    primary_card_key = REASON_CATEGORY_TO_CARD.get(raw_primary_category) if raw_primary_category else None
    # scoring.py's own text is already the real finding content for mismatch/
    # timing; for open_questions/missing_info it's just a count, and for
    # duplicates it's generic, so prefer the specific individual finding here.
    # Matched on the RAW (granular) category, not the merged display card, so
    # an "open_question" primary doesn't accidentally pick a timing entry.
    target_finding_type = {"open_question": "open_questions", "missing_info": "missing_info"}.get(raw_primary_category)
    same_type_findings = [f for f in findings if f["category"] == target_finding_type] if target_finding_type else []
    if raw_primary_category == "duplicate" and dup_note:
        primary_text = dup_note["text"]
    elif target_finding_type and same_type_findings:
        primary_text = same_type_findings[0]["text"]
    elif primary:
        primary_text = primary["text"]
    elif lead_analysis is None:
        primary_text = "Not yet analyzed — click Refresh at the top of the page."
    else:
        primary_text = "Flagged by deal size / staleness — see score breakdown."
    primary_tag = category_tag_html(primary_card_key) if primary_card_key else ""

    action = lead_analysis.suggested_next_action if lead_analysis else ""
    # Same .atliq-suggestion callout box the Opportunity Timeline/drill-down
    # already uses for "Suggested CRM update"/"Suggested action" - reused
    # here rather than duplicated, so both views read as one consistent
    # style instead of one being a bordered box and the other plain text.
    action_html = (
        f'<div class="atliq-suggestion" style="margin-top:8px;"><b>Suggested action:</b> {action}</div>' if action else ""
    )

    evidence_badge_html = ""
    if evidence:
        ec_color, ec_bg = EVIDENCE_LEVEL_COLORS[evidence["level"]]
        evidence_badge_html = (
            f'<span class="icon-badge" style="color:{ec_color};background:{ec_bg};margin-left:8px;">'
            f'<span class="msi">fact_check</span>Evidence completeness: {evidence["score"]}</span>'
        )

    source_badge_html = ""
    # Fix 2: sourced from the CRM row directly (via case_files_by_id), not
    # from lead_analysis, so it still shows even for a not-yet-analyzed lead
    # - it's static CRM data, independent of whether the LLM call has run.
    _cf_for_source = case_files_by_id.get(item.lead_id) if item.lead_id else None
    # Raw CRM field (crm_export.csv), same case_files_by_id lookup already
    # used for lead_source above - not re-fetched or reformatted from
    # anywhere else, and not something the AI pipeline ever touches. NaN
    # check mirrors the drill-down grid's own (`val == val`), so a blank
    # cell here matches exactly what the drill-down already treats as blank.
    service_interest = _cf_for_source.crm_row.get("service_interest") if _cf_for_source else None
    title_extra = ""
    if service_interest is not None and service_interest == service_interest and str(service_interest).strip():
        si_full = str(service_interest).strip()
        si_display = " ".join(si_full.split()[:4])
        if len(si_display) > 35:
            si_display = si_display[:35].rstrip()
        if si_display != si_full:
            si_display += "…"
        title_extra = f" — Service Interest: {si_display}"

    lead_source = _cf_for_source.crm_row.get("source") if _cf_for_source else None
    if lead_source:
        # Fix 2: plain descriptive metadata (not an urgency/quality signal),
        # so it's styled in a neutral gray distinct from the colored badges.
        source_badge_html = (
            f'<span class="icon-badge" style="color:#4b5563;background:rgba(107,114,128,0.12);margin-left:8px;">'
            f'<span class="msi">source</span>{lead_source}</span>'
        )

    impact_badge_html = ""
    if show_impact:
        # Section 6: derived label only - deal value + the existing urgency
        # score + staleness, all already computed elsewhere.
        impact = potential_impact(item, lead_analysis.crm_row if lead_analysis else None)
        impact_color, impact_bg = {
            "High": EVIDENCE_LEVEL_COLORS["danger"],
            "Medium": EVIDENCE_LEVEL_COLORS["warning"],
            "Low": EVIDENCE_LEVEL_COLORS["success"],
        }[impact]
        impact_badge_html = (
            f'<span class="icon-badge" style="color:{impact_color};background:{impact_bg};margin-left:8px;">'
            f'<span class="msi">bolt</span>Potential impact: {impact}</span>'
        )

    st.markdown(
        f'<div class="atliq-card">'
        f'<div style="display:flex;align-items:center;gap:12px;">'
        f'<div class="score-circle" style="--score-color:{badge["color"]};color:{badge["color"]};">{display_score}</div>'
        f'<div><b>{item.company}{lead_tag}{title_extra}</b><div style="margin-top:4px;">{badge_html(item.urgency_level)}{impact_badge_html}{evidence_badge_html}{source_badge_html}</div></div>'
        f'</div>'
        f'<div style="margin-top:10px;">{primary_tag}</div>'
        f'<div class="atliq-suggestion" style="margin-top:4px;text-decoration:none;">{primary_text}</div>'
        f'{action_html}'
        f'</div>',
        unsafe_allow_html=True,
    )

    if lead_analysis:
        # One Gmail flow, everywhere (This Week's Attention, category detail
        # views, and the drill-down) - not a second, thinner implementation.
        render_followup_email_section(lead_analysis, dup_note, key_suffix=f"card_{item.key}")

    if evidence:
        # Section 3: framed honestly as "evidence completeness" (computed from
        # real, checkable signals), never as a fabricated "AI confidence %".
        with st.popover(f"Evidence completeness: {evidence['score']} — why?", use_container_width=False):
            for reason in evidence["reasons"]:
                st.markdown(f"- {reason}")

    breakdown = ", ".join(_short_reason_label(r) for r in item.reason_details[:3])
    # Section 4 (explainability reframe): relabeled from "Show details" - same
    # underlying content (reasoning already generated, source documents used,
    # plus a brief summary line), no rebuild of the evidence system itself.
    details_label = f"Why did AI recommend this? ({len(findings)} finding(s))" if findings else "Why did AI recommend this?"
    with st.expander(details_label):
        if findings:
            category_labels = sorted({
                CATEGORIES_BY_KEY[FINDING_TYPE_TO_CARD.get(f["category"], f["category"])]["label"]
                for f in findings if FINDING_TYPE_TO_CARD.get(f["category"], f["category"]) in CATEGORIES_BY_KEY
            })
            st.caption(f"Summary: {len(findings)} finding(s) across {', '.join(category_labels)}.")
        if breakdown:
            st.caption(f"Score breakdown: {breakdown}")
        # Every individual finding for this lead, all categories together -
        # not just whatever category this card was reached through.
        for f in findings:
            tag = category_tag_html(FINDING_TYPE_TO_CARD.get(f["category"], f["category"]))
            st.markdown(f'<div style="margin-bottom:6px;">{tag} <span style="font-size:0.88rem;">{f["text"]}</span></div>', unsafe_allow_html=True)
        # docs_by_key works regardless of analysis status (linker.py knows the
        # linked documents the moment the app loads), so this covers
        # unanalyzed orphans too.
        render_linked_documents(docs_by_key.get(item.key, []))


if selected_tab == "attention":
    st.header("This Week's Attention")
    # Relocated here from the header row - same collapsed-by-default expander,
    # same content, just moved so the top header row stays uncluttered.
    with st.expander("Legend"):
        st.markdown(badge_html_direct("urgent") + " — act this week (large/stale deal, due-now timing, high-severity mismatch)", unsafe_allow_html=True)
        st.markdown(badge_html_direct("check") + " — some signal, worth a look", unsafe_allow_html=True)
        st.markdown(badge_html_direct("watch") + " — no pressing signal right now", unsafe_allow_html=True)
    if not analyses:
        st.info("Click **Refresh** at the top of the page to analyze all leads with linked emails/notes (requires an Anthropic API key).")
    else:
        attention_items = rank_attention(visible_analyses(), duplicate_pairs_visible, top_n=1000)
        if not attention_items:
            st.success("Nothing urgent found among the currently visible leads.")
        else:
            top5_wa = attention_items[:5]
            rest_wa = attention_items[5:]
            total_wa_slides = len(top5_wa) + 1  # +1 for the "view all" slide
            wa_slide = min(st.session_state.get("wa_slide", 0), total_wa_slides - 1)

            nav_left, nav_dots, nav_right = st.columns([1, 8, 1])
            with nav_left:
                if st.button("‹", key="wa_prev", use_container_width=True, disabled=(wa_slide == 0)):
                    st.session_state["wa_slide"] = max(0, wa_slide - 1)
                    st.session_state["wa_detail_key"] = None
                    st.rerun()
            with nav_right:
                if st.button("›", key="wa_next", use_container_width=True, disabled=(wa_slide == total_wa_slides - 1)):
                    st.session_state["wa_slide"] = min(total_wa_slides - 1, wa_slide + 1)
                    st.session_state["wa_detail_key"] = None
                    st.rerun()
            with nav_dots:
                dot_cols = st.columns(total_wa_slides)
                for i, dc in enumerate(dot_cols):
                    with dc:
                        dot_label = str(i + 1) if i < len(top5_wa) else "All"
                        dot_icon = ":material/radio_button_checked:" if i == wa_slide else ":material/radio_button_unchecked:"
                        if st.button(dot_label, key=f"wa_dot_{i}", icon=dot_icon, use_container_width=True,
                                     type="primary" if i == wa_slide else "secondary"):
                            st.session_state["wa_slide"] = i
                            st.session_state["wa_detail_key"] = None
                            st.rerun()

            if wa_slide < len(top5_wa):
                render_lead_card(top5_wa[wa_slide], show_impact=True)
            else:
                wa_detail_key = st.session_state.get("wa_detail_key")
                if wa_detail_key:
                    detail_item = next((i for i in attention_items if i.key == wa_detail_key), None)
                    if st.button("‹ Back to list", key="wa_back_to_list"):
                        st.session_state["wa_detail_key"] = None
                        st.rerun()
                    if detail_item:
                        render_lead_card(detail_item, show_impact=True)
                else:
                    st.caption(f"View all — every lead needing attention ({len(attention_items)})")
                    for wa_item in attention_items:
                        lead_tag = f" ({wa_item.lead_id})" if wa_item.lead_id else " (not in CRM)"
                        if st.button(f"{wa_item.company}{lead_tag}", key=f"wa_viewall_{wa_item.key}", use_container_width=True):
                            st.session_state["wa_detail_key"] = wa_item.key
                            st.rerun()


# --------------------------------------------------------------------------
# Tabs
# --------------------------------------------------------------------------
va = visible_analyses()
orphans_visible = [o for o in orphans if include_orphans]

duplicate_lead_ids_all: set[str] = set()
for p in duplicate_pairs:
    duplicate_lead_ids_all.add(p.lead_id_a)
    duplicate_lead_ids_all.add(p.lead_id_b)

# Full AttentionItem per visible, non-orphan-or-orphan lead - reused for the
# score circle/badge/primary-finding shown once you click into a lead's own
# detail view. This is NOT what ranks leads within a category (see Fix B
# below) - it's the same overall score "This Week's Attention" uses,
# unchanged, just displayed on whichever lead you're looking at.
attention_by_key: dict[str, AttentionItem] = {a.key: score_lead(a, duplicate_lead_ids_all) for a in va}
va_by_key: dict[str, LeadAnalysis] = {a.key: a for a in va}

# --------------------------------------------------------------------------
# V2 section 8 — Hidden Opportunity: medium (not high) urgency leads with
# real underlying-value signals (confirmed budget, a decision-maker present
# in the conversation) and no active mismatch - a heuristic over existing
# findings/CRM data, reusing the same `va`/`attention_by_key` just built
# above. No AI call.
# --------------------------------------------------------------------------
hidden_opportunities = sorted(
    (a for a in va if not a.is_orphan and is_hidden_opportunity(attention_by_key[a.key], a)),
    key=lambda a: (a.crm_row or {}).get("est_value_usd") or 0,
    reverse=True,
)

if selected_tab == "hidden_opportunity":
    st.header("Hidden Opportunity")
    if not analyses:
        st.info("Click **Refresh** at the top of the page to surface hidden opportunities.")
    elif not hidden_opportunities:
        # Static, hardcoded demo card - not derived from any real lead, no AI
        # call, nothing cached - shown only while len(hidden_opportunities)
        # is 0, and replaced automatically the instant real results exist
        # (same elif/else branching as before, just this branch's content).
        st.caption("No hidden opportunities found right now — here's an example of what this looks like when one is detected.")
        st.markdown(
            '<div class="atliq-card example-card">'
            '<span class="icon-badge" style="color:#A79FB8;background:rgba(167,159,184,0.16);">'
            '<span class="msi">visibility</span>EXAMPLE — not live data</span>'
            '<div style="margin-top:8px;"><b>Sample Corp (Demo) (L-0000)</b>' + badge_html_direct("consider") + '</div>'
            '<div style="margin-top:6px;font-size:0.9rem;">Hidden opportunity — AI noticed this deal has strong underlying signals despite not being top-ranked. '
            'Deal value: $62,000 (example), owner: Jordan (example).</div>'
            '</div>',
            unsafe_allow_html=True,
        )
    else:
        st.caption("AI noticed these deals have strong underlying signals despite not being top-ranked by urgency.")
        for a in hidden_opportunities:
            val = (a.crm_row or {}).get("est_value_usd")
            val_text = f"${float(val):,.0f}" if val is not None and val == val else "value not on record"
            st.markdown(
                f'<div class="atliq-card">'
                f'<b>{a.company} ({a.lead_id})</b>{badge_html_direct("consider")}'
                f'<div style="margin-top:6px;font-size:0.9rem;">Hidden opportunity — AI noticed this deal has strong underlying signals despite not being top-ranked. '
                f'Deal value: {val_text}, owner: {(a.crm_row or {}).get("owner", "unassigned")}.</div>'
                f'</div>',
                unsafe_allow_html=True,
            )

# Fix B: each category ranks leads by relevance to THAT category - not by
# the overall urgency score - so different categories actually surface
# different leads. Severity weights are local to this ranking only (distinct
# from scoring.py's own weights) since this is a display-level sort, not a
# change to how urgency is scored.
MISMATCH_SEVERITY_WEIGHT = {"high": 3, "medium": 2, "low": 1}


def _category_relevance(category_key: str, a: LeadAnalysis) -> tuple[float, float]:
    """(relevance, tiebreak_value) for one lead in one category - both read
    straight from already-cached LeadAnalysis finding lists, no new AI calls.
    Higher sorts first; est_value_usd only breaks ties on equal relevance."""
    value = a.crm_row.get("est_value_usd") if a.crm_row else None
    value = float(value) if value is not None and value == value else 0.0  # NaN-safe

    if category_key == "mismatches":
        relevance = float(sum(MISMATCH_SEVERITY_WEIGHT.get(m.get("severity", "low"), 1) for m in a.mismatches))
    elif category_key == "missing_info":
        relevance = float(len(a.missing_info_flags))
    elif category_key == "followup_open_questions":
        relevance = float(len(a.timing_signals) + len(a.open_questions_or_dropped_commitments))
    elif category_key == "deprioritize":
        relevance = float(len(a.deprioritize_signals))
    elif category_key == "not_in_crm":
        relevance = float(len(a.missing_info_flags) + len(a.open_questions_or_dropped_commitments))
    else:
        relevance = 0.0
    return relevance, value


def _leads_in_category(category_key: str) -> list[LeadAnalysis]:
    """Non-orphan leads with >=1 finding in this category. Fix A: the merged
    "Follow-up & open questions" category matches either underlying field."""
    if category_key == "mismatches":
        return [a for a in va if not a.is_orphan and a.mismatches]
    if category_key == "missing_info":
        return [a for a in va if not a.is_orphan and a.missing_info_flags]
    if category_key == "followup_open_questions":
        return [a for a in va if not a.is_orphan and (a.timing_signals or a.open_questions_or_dropped_commitments)]
    if category_key == "deprioritize":
        return [a for a in va if not a.is_orphan and a.deprioritize_signals]
    return []


def full_ranked_list_for_category(category_key: str):
    """Returns (items, kind) - the COMPLETE, deduplicated, ranked list for a
    category (kind is 'lead' or 'pair'). This is the single source of truth:
    the category card's count is len() of this list, and the top-3/view-all
    lists are slices of this exact same list, so they can never disagree.
    Deduplicated by lead/pair - a lead with 3 mismatches is still exactly one
    entry here, never three. Ranked by category-specific relevance (Fix B),
    not by the overall urgency score."""
    if category_key == "duplicates":
        pairs = sorted(duplicate_pairs_visible, key=lambda p: 0 if p.confidence == "high" else 1)
        return pairs, "pair"

    if category_key == "not_in_crm":
        # Every orphan counts, regardless of analysis status, so the count
        # never implies more items than actually show. Unanalyzed orphans
        # still appear (relevance 0), just ranked last.
        leads = []
        for o in orphans_visible:
            key = "ORPHAN::" + o.company_guess
            leads.append(va_by_key.get(key) or LeadAnalysis(key=key, lead_id=None, company=o.company_guess, is_orphan=True, crm_row=None, docs=o.docs))
        leads.sort(key=lambda a: _category_relevance(category_key, a), reverse=True)
        items = [attention_by_key.get(a.key) or score_lead(a, duplicate_lead_ids_all) for a in leads]
        return items, "lead"

    leads = _leads_in_category(category_key)
    leads.sort(key=lambda a: _category_relevance(category_key, a), reverse=True)
    items = [attention_by_key[a.key] for a in leads if a.key in attention_by_key]
    return items, "lead"


def ranked_items_for_category(category_key: str, top_n: int = 3):
    """(top, rest, kind) - a top_n/rest slice of full_ranked_list_for_category."""
    items, kind = full_ranked_list_for_category(category_key)
    return items[:top_n], items[top_n:], kind


def render_pair_card(p: DuplicatePair):
    conf_level = "red" if p.confidence == "high" else "yellow"
    reasons = "<br>".join(f"• {r}" for r in p.reasons)
    st.markdown(
        f'<div class="atliq-card"><b>{p.company_a} ({p.lead_id_a}, owner: {p.owner_a})</b> '
        f'&harr; <b>{p.company_b} ({p.lead_id_b}, owner: {p.owner_b})</b>{badge_html(conf_level)}'
        f'<div style="margin-top:8px;font-size:0.9rem;">{reasons}</div>'
        f'<div class="atliq-suggestion">Suggestion — review and merge manually; not applied automatically.</div></div>',
        unsafe_allow_html=True,
    )


# --------------------------------------------------------------------------
# V2 section 9 — Better metrics row: all derived from data already computed
# above (va, attention_by_key, hidden_opportunities), no new AI calls.
# --------------------------------------------------------------------------
if selected_tab == "categories":
    if va:
        avg_evidence = sum(evidence_completeness(a)["score"] for a in va) / len(va)
        avg_urgency = sum(attention_by_key[a.key].score for a in va) / len(va)
        n_high_risk = sum(1 for a in va if is_high_risk(a.crm_row))
        n_critical = sum(1 for a in va if attention_by_key[a.key].urgency_level == "red")

        metric_cols = st.columns(5)
        metric_cols[0].metric("Avg. evidence", f"{avg_evidence:.0f}", help="Average evidence completeness score across all currently visible leads.")
        metric_cols[1].metric("Avg. urgency score", f"{avg_urgency:.1f}")
        metric_cols[2].metric("High Risk leads", n_high_risk, help="Very stale (>90 days since contact) AND high value (≥$50k).")
        metric_cols[3].metric("Hidden Opportunities", len(hidden_opportunities))
        metric_cols[4].metric("Critical leads", n_critical, help="Matches the existing 'Urgent' definition.")
        st.divider()

    # --------------------------------------------------------------------------
    # All findings — clickable category cards + a filtered list below
    # --------------------------------------------------------------------------
    st.header("Categories")

    # Fix 1: the card's big number and the top-3/"View all N" lists must always
    # agree, so both are read from full_ranked_list_for_category() - one
    # deduplicated-by-lead list per category, counted once here.
    counts = {cat["key"]: len(full_ranked_list_for_category(cat["key"])[0]) for cat in CATEGORIES}
    # Same unit as the count above (pairs for duplicates, leads everywhere
    # else) - shown on each card so "22" is never mistaken for a raw finding
    # count (that's the debug panel's "N finding(s)" number instead; the two
    # intentionally differ - a lead with 3 mismatches is still 1 entry here).
    CATEGORY_COUNT_UNIT = {
        "mismatches": "leads", "missing_info": "leads", "followup_open_questions": "leads",
        "duplicates": "pairs", "not_in_crm": "leads", "deprioritize": "leads",
    }
    _FINDING_FIELD_MAP = {
        "mismatches": ["mismatches"], "missing_info": ["missing_info_flags"],
        "followup_open_questions": ["timing_signals", "open_questions_or_dropped_commitments"],
        "deprioritize": ["deprioritize_signals"],
    }

    def _category_finding_count(category_key: str) -> int:
        """Total individual findings behind this card's lead count - same
        underlying lead set as `counts` above (via _leads_in_category), so
        the two numbers can never drift out of sync with each other."""
        fields = _FINDING_FIELD_MAP.get(category_key)
        if not fields:
            return 0
        return sum(sum(len(getattr(a, f)) for f in fields) for a in _leads_in_category(category_key))

    st.session_state.setdefault("selected_category", None)

    card_cols = st.columns(6)
    for col, cat in zip(card_cols, CATEGORIES):
        with col:
            is_selected = st.session_state["selected_category"] == cat["key"]
            _unit = CATEGORY_COUNT_UNIT.get(cat["key"], "leads")
            st.markdown(
                f'<div class="category-card {"selected" if is_selected else ""}" '
                f'style="--card-color:{cat["color"]};background:{cat["bg"]};">'
                f'<span class="msi cat-icon">{cat["icon"]}</span>'
                f'<div class="cat-count" style="color:{cat["color"]};">{counts[cat["key"]]}</div>'
                f'<div class="cat-unit" style="color:{cat["color"]};">{_unit}</div>'
                f'<div class="cat-label">{cat["label"]}</div>'
                f'</div>',
                unsafe_allow_html=True,
            )
            _finding_count = _category_finding_count(cat["key"])
            _card_help = f"{counts[cat['key']]} {_unit} · {_finding_count} finding(s) total (a lead can have more than one)" if _finding_count else None
            if st.button(cat["label"], key=f"cardbtn_{cat['key']}", use_container_width=True, help=_card_help,
                         type="primary" if is_selected else "secondary"):
                st.session_state["selected_category"] = None if is_selected else cat["key"]
                st.rerun()

    selected_category = st.session_state["selected_category"]
    selected_cat_meta = next((c for c in CATEGORIES if c["key"] == selected_category), None)
    st.caption(f"Showing: {selected_cat_meta['label']}" if selected_cat_meta else "Showing all findings")

    def render_minimal_lead_row(item: AttentionItem, key_prefix: str):
        """Fix 1: just a clickable company name + its suggested action underneath
        - no badge, no findings text, no expander. Clicking opens the shared full
        detail view (render_lead_full) in place of the list."""
        lead_tag = f" ({item.lead_id})" if item.lead_id else " (not in CRM)"
        if st.button(f"{item.company}{lead_tag}", key=f"{key_prefix}_{item.key}", use_container_width=True):
            st.session_state["category_detail_key"] = item.key
            st.rerun()
        lead_analysis = analyses.get(item.key)
        if lead_analysis and lead_analysis.suggested_next_action:
            # Restored: was showing as a bare, unlabeled caption - easy to miss
            # and inconsistent with the clearly-labeled "Suggested action:" line
            # in the shared render_lead_card() component (Weekly Attention /
            # detail views). Same underlying suggested_next_action data either way.
            st.caption(f"**Suggested action:** {lead_analysis.suggested_next_action}")


    if selected_category is None:
        if analyses:
            st.info("Select a category above to browse its top leads.")
        else:
            st.info("No findings yet — click **Refresh** at the top of the page.")
    else:
        # Reset whenever the selected category changes, so an open detail view
        # from a different category doesn't linger.
        if st.session_state.get("category_for_detail") != selected_category:
            st.session_state["category_for_detail"] = selected_category
            st.session_state["category_detail_key"] = None

        top3, rest, kind = ranked_items_for_category(selected_category, top_n=3)
        all_items = top3 + rest
        detail_key = st.session_state.get("category_detail_key")

        if not all_items:
            st.info("No findings in this category for the currently visible leads.")
        elif kind == "lead" and detail_key:
            detail_item = next((i for i in all_items if i.key == detail_key), None)
            if st.button("‹ Back to list", key="category_back_to_list"):
                st.session_state["category_detail_key"] = None
                st.rerun()
            if detail_item:
                render_lead_card(detail_item)
        elif kind == "lead":
            for item in top3:
                render_minimal_lead_row(item, "cat_top3")
            if rest:
                with st.expander(f"View all {len(all_items)} leads"):
                    for item in rest:
                        render_minimal_lead_row(item, "cat_viewall")
        else:
            # Duplicates are pairs, not single leads - a pair card already has no
            # deeper level to click into, so top-3/view-all both just show the
            # (already compact) pair card, deduplicated by pair as before.
            for pair in top3:
                render_pair_card(pair)
            if rest:
                with st.expander(f"View all {len(all_items)} duplicate pairs"):
                    for pair in rest:
                        render_pair_card(pair)


# Same cutoffs scoring.py already uses for "large deal" / "meaningfully
# stale" - kept unconditional (not just on the Effort vs. Payoff tab) since
# the Dashboard Assistant's "quick wins count" insight also reads these.
VALUE_THRESHOLD = 50_000
STALENESS_THRESHOLD = 45


def _compute_quadrant_rows() -> tuple[list[dict], int]:
    """Pure CRM/scoring math, no LLM analysis required - shared verbatim by
    the Effort vs. Payoff tab and Portfolio Insights' Pipeline Health chart
    (extracted, not duplicated, so both can never silently diverge). Scoped
    by visible_lead_ids like every other tab, so it respects the owner
    filter row at the top of the page the same way everywhere it's used.
    """
    quad_rows = []
    skipped = 0
    for cf in case_files:
        if cf.lead_id not in visible_lead_ids:
            continue
        row = cf.crm_row
        status = (row.get("status") or "").strip().lower()
        if status in ("won", "lost"):
            continue
        value = row.get("est_value_usd")
        last_contact = pd.to_datetime(row.get("last_contact_date") or None, errors="coerce")
        if value is None or value != value or pd.isna(last_contact):
            skipped += 1
            continue
        days_stale = (pd.Timestamp(TODAY) - last_contact).days
        if value >= VALUE_THRESHOLD and days_stale < STALENESS_THRESHOLD:
            quadrant = "Prioritize"
        elif value < VALUE_THRESHOLD and days_stale < STALENESS_THRESHOLD:
            quadrant = "Quick wins"
        elif value >= VALUE_THRESHOLD:
            quadrant = "Worth the push"
        else:
            quadrant = "Consider letting go"
        quad_rows.append({
            "company": row["company"], "lead_id": cf.lead_id, "owner": row.get("owner", ""),
            "status": row.get("status", ""), "value": float(value), "days_stale": int(days_stale),
            "quadrant": quadrant,
        })
    return quad_rows, skipped


# Shared purple/magenta quadrant palette - defined once here so Portfolio
# Insights' Pipeline Health chart uses the exact same colors as Effort vs.
# Payoff, not a second hand-picked set.
QUADRANT_ORDER = ["Prioritize", "Quick wins", "Worth the push", "Consider letting go"]
_is_dark_theme = st.get_option("theme.base") == "dark"
QUADRANT_COLORS = (
    {"Prioritize": "#7C3AED", "Quick wins": "#A78BFA", "Worth the push": "#EC4899", "Consider letting go": "#6D5A82"}
    if _is_dark_theme else
    {"Prioritize": "#6D28D9", "Quick wins": "#8B5CF6", "Worth the push": "#DB2777", "Consider letting go": "#5B4B6E"}
)

if selected_tab == "effort_payoff":
    # ----------------------------------------------------------------------
    # Effort vs. Payoff — pure CRM/scoring data, no LLM analysis required, so
    # this renders even before any "Run analysis" click.
    # ----------------------------------------------------------------------
    st.header("Effort vs. Payoff")
    st.caption("Every open lead plotted by deal value against staleness — no AI calls, this uses only the CRM export.")

    quad_rows, skipped = _compute_quadrant_rows()

    if not quad_rows:
        st.info("No open leads with both a deal value and a last-contact date to plot.")
    else:
        quad_df = pd.DataFrame(quad_rows)

        # QUADRANT_ORDER/QUADRANT_COLORS are now shared module-level
        # constants (see _compute_quadrant_rows above) so Portfolio
        # Insights' Pipeline Health chart uses the exact same palette -
        # aliased to the old local name so nothing below this line changes.
        quadrant_colors = QUADRANT_COLORS
        color_scale = alt.Scale(domain=QUADRANT_ORDER, range=[quadrant_colors[q] for q in QUADRANT_ORDER])

        max_value = max(quad_df["value"].max() * 1.08, VALUE_THRESHOLD * 1.6)
        max_days = max(quad_df["days_stale"].max() * 1.08, STALENESS_THRESHOLD * 2.2)

        quadrant_bg = pd.DataFrame([
            {"quadrant": "Prioritize", "x0": VALUE_THRESHOLD, "x1": max_value, "y0": 0, "y1": STALENESS_THRESHOLD},
            {"quadrant": "Quick wins", "x0": 0, "x1": VALUE_THRESHOLD, "y0": 0, "y1": STALENESS_THRESHOLD},
            {"quadrant": "Worth the push", "x0": VALUE_THRESHOLD, "x1": max_value, "y0": STALENESS_THRESHOLD, "y1": max_days},
            {"quadrant": "Consider letting go", "x0": 0, "x1": VALUE_THRESHOLD, "y0": STALENESS_THRESHOLD, "y1": max_days},
        ])
        quadrant_labels = pd.DataFrame([
            {"quadrant": "Prioritize", "x": VALUE_THRESHOLD + (max_value - VALUE_THRESHOLD) / 2, "y": STALENESS_THRESHOLD * 0.08},
            {"quadrant": "Quick wins", "x": VALUE_THRESHOLD / 2, "y": STALENESS_THRESHOLD * 0.08},
            {"quadrant": "Worth the push", "x": VALUE_THRESHOLD + (max_value - VALUE_THRESHOLD) / 2, "y": max_days - (max_days - STALENESS_THRESHOLD) * 0.08},
            {"quadrant": "Consider letting go", "x": VALUE_THRESHOLD / 2, "y": max_days - (max_days - STALENESS_THRESHOLD) * 0.08},
        ])

        background = alt.Chart(quadrant_bg).mark_rect(opacity=0.10).encode(
            x=alt.X("x0:Q"), x2="x1:Q", y=alt.Y("y0:Q"), y2="y1:Q",
            color=alt.Color("quadrant:N", scale=color_scale, legend=None),
        )
        labels = alt.Chart(quadrant_labels).mark_text(fontSize=12, fontWeight="bold", opacity=0.6).encode(
            x="x:Q", y="y:Q", text="quadrant:N",
            color=alt.Color("quadrant:N", scale=color_scale, legend=None),
        )
        vline = alt.Chart(pd.DataFrame({"x": [VALUE_THRESHOLD]})).mark_rule(strokeDash=[4, 4], color="#898781").encode(x="x:Q")
        hline = alt.Chart(pd.DataFrame({"y": [STALENESS_THRESHOLD]})).mark_rule(strokeDash=[4, 4], color="#898781").encode(y="y:Q")

        points = alt.Chart(quad_df).mark_circle(size=110, opacity=0.9, stroke="#ffffff", strokeWidth=0.6).encode(
            x=alt.X("value:Q", title="Deal value (USD)", scale=alt.Scale(domain=[0, max_value])),
            y=alt.Y("days_stale:Q", title="Days since last contact", scale=alt.Scale(domain=[0, max_days])),
            color=alt.Color("quadrant:N", scale=color_scale, legend=alt.Legend(title="Quadrant")),
            tooltip=[
                alt.Tooltip("company:N", title="Company"),
                alt.Tooltip("lead_id:N", title="Lead ID"),
                alt.Tooltip("owner:N", title="Owner"),
                alt.Tooltip("status:N", title="Status"),
                alt.Tooltip("value:Q", title="Deal value", format="$,.0f"),
                alt.Tooltip("days_stale:Q", title="Days stale"),
                alt.Tooltip("quadrant:N", title="Quadrant"),
            ],
        )

        chart = (background + labels + vline + hline + points).properties(height=420)
        st.altair_chart(chart, use_container_width=True, theme="streamlit")

        if skipped > 0:
            st.caption(f"{skipped} visible lead(s) excluded from this chart — closed (Won/Lost), or missing a deal value / last-contact date.")

        with st.expander("Table view", expanded=True):
            # Same quadrant_colors mapping as the chart above, so table and
            # chart read as one consistent view - a tinted background +
            # matching text color per quadrant, not a plain unstyled cell.
            def _quadrant_cell_style(value: str) -> str:
                color = quadrant_colors.get(value)
                return f"background-color: {color}26; color: {color}; font-weight: 600;" if color else ""

            table_df = quad_df[["company", "lead_id", "owner", "status", "value", "days_stale", "quadrant"]].sort_values("value", ascending=False)
            st.dataframe(
                table_df.style.map(_quadrant_cell_style, subset=["quadrant"]).format({"value": "${:,.0f}"}),
                use_container_width=True,
            )

if selected_tab == "portfolio_insights":
    # --------------------------------------------------------------------------
    # Portfolio Insights (Phase 1) — six charts giving a cross-lead view of the
    # pipeline, replacing the old bullet-point Executive Summary on the
    # Overview tab (moved here as a short 1-2 line version instead - see
    # below). Every chart on this page reads ONLY data already sitting in
    # memory: case_files/crm_df (from load_everything(), cached), analyses
    # (from st.session_state, populated by the last Refresh), and the same
    # _compute_quadrant_rows() helper Effort vs. Payoff uses - NOT a single
    # new Claude API call anywhere on this page. Charts are scoped by
    # visible_lead_ids (the owner-filter row above), same as every other
    # tab - the one exception is the condensed summary blurb, which stays
    # full-pipeline like the original Executive Summary always was, since
    # it's one cached AI paragraph that isn't regenerated per owner-click.
    # --------------------------------------------------------------------------
    st.header("Portfolio Insights")

    _exec_summary = st.session_state.get("exec_summary")
    _exec_summary_error = st.session_state.get("exec_summary_error")
    if _exec_summary:
        _exec_bullets = _exec_summary if isinstance(_exec_summary, list) else [_exec_summary]
        # Condensed to the first 2 bullets only - same cached data as the
        # old full Executive Summary, no new AI call to shorten it.
        _short_bullets = _exec_bullets[:2]
        _short_html = " ".join(_short_bullets)
        st.markdown(f'<div class="atliq-card" style="font-size:0.95rem;">{_short_html}</div>', unsafe_allow_html=True)
        st.caption("Reflects the full pipeline as of the last Refresh, independent of the owner filter above.")
    elif _exec_summary_error:
        st.warning(f"Executive summary could not be generated: {_exec_summary_error}")
    else:
        st.info("Click **Refresh** at the top of the page to generate a summary and populate these charts.")

    st.divider()

    # ---- Shared chart look --------------------------------------------------
    # This app renders every chart via Altair/Vega-Lite (st.altair_chart), not
    # Plotly - there is no Plotly anywhere in this codebase. The options below
    # are the Vega-Lite equivalents of what was asked for (transparent
    # background, no action/export menu, app font, subtle gridlines matching
    # the card border color, formatted tooltips).
    #
    # Layout: charts are laid out in a 2-column grid (st.columns), and every
    # chart gets an explicit width instead of use_container_width=True - a
    # bar chart's width scales with its own category count (_bar_width) so a
    # 1-4 bar chart doesn't stretch across a whole column, and donuts get a
    # fixed compact size. This is what fixes the excess empty space / bloated
    # scrolling from the previous full-width version.
    _pi_is_dark = st.get_option("theme.base") == "dark"
    _pi_grid_color = "rgba(255,255,255,0.08)" if _pi_is_dark else "rgba(15,23,42,0.08)"
    _pi_label_color = "#CBD5E1" if _pi_is_dark else "#334155"
    _pi_font = "Inter"
    _pi_donut_width = 460
    _pi_donut_height = 260
    # Vega expression (not Python) - formats an axis tick value as "$440K" /
    # "$1.2M" instead of Vega-Lite's default lowercase SI suffix ("$440k").
    _pi_usd_axis_expr = (
        "'$' + (datum.value >= 1000000 ? format(datum.value/1000000,'.1f')+'M' : "
        "datum.value >= 1000 ? format(datum.value/1000,'.0f')+'K' : format(datum.value,',.0f'))"
    )

    def _fmt_usd_short(v: float) -> str:
        if v >= 1_000_000:
            return f"${v / 1_000_000:.1f}M"
        if v >= 1_000:
            return f"${v / 1_000:.0f}K"
        return f"${v:,.0f}"

    def _lead_preview(names: list[str], limit: int = 3) -> str:
        """One-line 'top N leads, +M more' summary for a hover tooltip -
        Vega-Lite tooltips are declarative fields (no Plotly-style
        hovertemplate/customdata in this stack), so the detail text is
        pre-composed in Python and passed as a plain tooltip field."""
        if not names:
            return "—"
        shown = ", ".join(names[:limit])
        extra = len(names) - limit
        return f"{shown} +{extra} more" if extra > 0 else shown

    def _truncate(text: str, limit: int = 100) -> str:
        text = (text or "").strip()
        return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"

    def _bar_width(n_categories: int) -> int:
        """Scales a bar chart's own width to its category count (~100px/bar,
        floor 170 so even a single bar doesn't look like a sliver, cap 480 so
        it never dominates its half-width column)."""
        return min(480, max(170, n_categories * 100))

    def _int_y(field: str, title: str, max_val: float):
        """Integer-only y-axis with explicit tick values and a matching
        domain - avoids Vega-Lite's auto tick selection producing duplicate
        rounded labels (e.g. "1 1 0") when the value range is tiny."""
        top = max(int(max_val), 1)
        return alt.Y(
            f"{field}:Q", title=title,
            scale=alt.Scale(domain=[0, top]),
            axis=alt.Axis(values=list(range(0, top + 1)), format="d"),
        )

    def _style_chart(chart):
        """Applies the shared look to a finished (possibly layered) chart:
        transparent background so it blends into its card, no vega-embed
        action menu, app font, and subtle theme-matched gridlines with the
        axis domain line removed."""
        return (
            chart.properties(background="transparent", usermeta={"embedOptions": {"actions": False}})
            .configure_view(fill=None, stroke=None)
            .configure_axis(
                gridColor=_pi_grid_color, domain=False, tickColor=_pi_grid_color,
                labelColor=_pi_label_color, titleColor=_pi_label_color,
                labelFont=_pi_font, titleFont=_pi_font,
            )
            .configure_legend(labelColor=_pi_label_color, titleColor=_pi_label_color, labelFont=_pi_font, titleFont=_pi_font)
            .configure_text(font=_pi_font)
        )

    _row1_left, _row1_right = st.columns(2, gap="large")

    # ---- Chart 1: Pipeline Health (Effort vs. Payoff quadrant counts) -----
    with _row1_left:
        st.markdown("##### Pipeline Health")
        st.caption("Lead count per Effort vs. Payoff quadrant — no AI calls, same computation as the Effort vs. Payoff tab.")
        _pi_quad_rows, _ = _compute_quadrant_rows()
        if not _pi_quad_rows:
            st.caption("No open leads with both a deal value and a last-contact date to plot.")
        else:
            _quad_counts = pd.DataFrame(_pi_quad_rows)["quadrant"].value_counts().reindex(QUADRANT_ORDER, fill_value=0).reset_index()
            _quad_counts.columns = ["quadrant", "count"]
            _quad_leads: dict[str, list[str]] = {}
            for _r in _pi_quad_rows:
                _quad_leads.setdefault(_r["quadrant"], []).append(_r["company"])
            _quad_counts["leads_preview"] = _quad_counts["quadrant"].map(lambda q: _lead_preview(_quad_leads.get(q, [])))
            _quad_color_scale = alt.Scale(domain=QUADRANT_ORDER, range=[QUADRANT_COLORS[q] for q in QUADRANT_ORDER])
            _bars1 = alt.Chart(_quad_counts).mark_bar(size=48).encode(
                x=alt.X("quadrant:N", title=None, sort=QUADRANT_ORDER, axis=alt.Axis(labelAngle=-30, labelAlign="right", labelOverlap=False)),
                y=_int_y("count", "Leads", _quad_counts["count"].max()),
                color=alt.Color("quadrant:N", scale=_quad_color_scale, legend=None),
                tooltip=[alt.Tooltip("quadrant:N", title="Quadrant"), alt.Tooltip("count:Q", title="Leads", format="d"), alt.Tooltip("leads_preview:N", title="Leads")],
            )
            _labels1 = alt.Chart(_quad_counts).mark_text(dy=-10, fontWeight="bold", color=_pi_label_color).encode(
                x=alt.X("quadrant:N", sort=QUADRANT_ORDER), y=alt.Y("count:Q"), text=alt.Text("count:Q", format="d"),
            )
            _chart1 = (_bars1 + _labels1).properties(width=_bar_width(len(_quad_counts)), height=260, padding={"top": 24, "bottom": 5, "left": 5, "right": 5})
            st.altair_chart(_style_chart(_chart1), use_container_width=False, theme="streamlit")

    # ---- Chart 2: What Clients Are Asking For (service_interest) ----------
    with _row1_right:
        st.markdown("##### What Clients Are Asking For")
        st.caption("Leads grouped by SERVICE_INTEREST from the CRM export — a raw CRM field, not AI-generated.")
        _service_rows = []
        _service_leads: dict[str, list[str]] = {}
        for cf in case_files:
            if cf.lead_id not in visible_lead_ids:
                continue
            val = (cf.crm_row.get("service_interest") or "").strip() or "Not specified"
            _service_rows.append(val)
            _service_leads.setdefault(val, []).append(cf.crm_row.get("company", cf.lead_id))
        if not _service_rows:
            st.caption("No visible leads to summarize.")
        else:
            _service_counts = pd.Series(_service_rows).value_counts().reset_index()
            _service_counts.columns = ["service_interest", "count"]
            _service_counts["pct"] = _service_counts["count"] / _service_counts["count"].sum()
            _service_counts["leads_preview"] = _service_counts["service_interest"].map(lambda s: _lead_preview(_service_leads.get(s, [])))
            _service_palette = ["#8B5CF6", "#EC4899", "#34D399", "#F59E0B", "#60A5FA", "#7C6A9C"]
            _service_domain = list(_service_counts["service_interest"])
            _service_colors = [_service_palette[i % len(_service_palette)] for i in range(len(_service_domain))]
            _donut2 = alt.Chart(_service_counts).mark_arc(innerRadius=65, outerRadius=115).encode(
                theta=alt.Theta("count:Q", stack=True),
                color=alt.Color(
                    "service_interest:N", title="Service interest",
                    scale=alt.Scale(domain=_service_domain, range=_service_colors),
                    legend=alt.Legend(orient="right", labelLimit=160, titleLimit=160),
                ),
                tooltip=[
                    alt.Tooltip("service_interest:N", title="Service interest"), alt.Tooltip("count:Q", title="Leads", format="d"),
                    alt.Tooltip("pct:Q", title="Share", format=".0%"), alt.Tooltip("leads_preview:N", title="Leads"),
                ],
            )
            _pct_labels2 = alt.Chart(_service_counts).mark_text(radius=88, fontWeight="bold", color="white").encode(
                theta=alt.Theta("count:Q", stack=True), text=alt.Text("pct:Q", format=".0%"),
            )
            _chart2 = (_donut2 + _pct_labels2).properties(width=_pi_donut_width, height=_pi_donut_height, padding={"top": 30, "bottom": 5, "left": 5, "right": 5})
            st.altair_chart(_style_chart(_chart2), use_container_width=False, theme="streamlit")

    st.divider()
    _row2_left, _row2_right = st.columns(2, gap="large")

    # ---- Chart 3: Revenue at Risk from Pricing Pushback --------------------
    with _row2_left:
        st.markdown("##### Revenue at Risk from Pricing Pushback")
        st.caption("Share of total pipeline value sitting on leads with a 'pricing pushback' deprioritize signal.")
        if not analyses:
            st.info("Click **Refresh** at the top of the page to populate this chart.")
        else:
            _total_pipeline_value = 0.0
            _at_risk_value = 0.0
            _at_risk_leads: list[tuple[str, float]] = []
            for cf in case_files:
                if cf.lead_id not in visible_lead_ids:
                    continue
                val = cf.crm_row.get("est_value_usd")
                if val is None or val != val:
                    continue
                val = float(val)
                _total_pipeline_value += val
                a = analyses.get(cf.lead_id)
                if a and any(d["signal_type"] == "pricing_pushback" for d in a.deprioritize_signals):
                    _at_risk_value += val
                    _at_risk_leads.append((cf.crm_row.get("company", cf.lead_id), val))
            if _total_pipeline_value <= 0:
                st.caption("No visible leads with a deal value to plot.")
            else:
                _pct_at_risk = _at_risk_value / _total_pipeline_value
                _rest_value = max(_total_pipeline_value - _at_risk_value, 0.0)
                _at_risk_leads.sort(key=lambda x: x[1], reverse=True)
                _at_risk_preview = _lead_preview([n for n, _ in _at_risk_leads])
                _risk_df = pd.DataFrame([
                    {"label": "At risk (pricing pushback)", "value": _at_risk_value, "leads_preview": _at_risk_preview},
                    {"label": "Rest of pipeline", "value": _rest_value, "leads_preview": "—"},
                ])
                _rest_color = "#3F3350" if _pi_is_dark else "#E5E0EE"
                _donut3 = alt.Chart(_risk_df).mark_arc(innerRadius=70, outerRadius=115).encode(
                    theta=alt.Theta("value:Q", stack=True),
                    color=alt.Color(
                        "label:N", title=None,
                        scale=alt.Scale(domain=["At risk (pricing pushback)", "Rest of pipeline"], range=["#EC4899", _rest_color]),
                        legend=alt.Legend(orient="right", labelLimit=180),
                    ),
                    tooltip=[alt.Tooltip("label:N", title=""), alt.Tooltip("value:Q", title="Deal value", format="$,.0f"), alt.Tooltip("leads_preview:N", title="Top leads")],
                )
                _center_text3 = alt.Chart(pd.DataFrame({"text": [f"{_pct_at_risk:.0%}\nat risk"]})).mark_text(
                    fontSize=20, fontWeight="bold", color=_pi_label_color, lineBreak="\n",
                ).encode(text="text:N")
                _chart3 = (_donut3 + _center_text3).properties(width=_pi_donut_width, height=_pi_donut_height, padding={"top": 30, "bottom": 5, "left": 5, "right": 5})
                st.altair_chart(_style_chart(_chart3), use_container_width=False, theme="streamlit")
                st.caption(f"{_pct_at_risk:.0%} of visible pipeline value is on leads showing unresolved pricing pushback.")

    # ---- Chart 4: Lead Source Effectiveness --------------------------------
    with _row2_right:
        st.markdown("##### Lead Source Effectiveness")
        st.caption("Total deal value by lead source — no AI calls, this uses only the CRM export.")
        _source_rows = []
        _source_leads: dict[str, list[tuple[str, float]]] = {}
        for cf in case_files:
            if cf.lead_id not in visible_lead_ids:
                continue
            val = cf.crm_row.get("est_value_usd")
            val = float(val) if val is not None and val == val else 0.0
            source = (cf.crm_row.get("source") or "").strip() or "Not specified"
            _source_rows.append({"source": source, "value": val})
            _source_leads.setdefault(source, []).append((cf.crm_row.get("company", cf.lead_id), val))
        if not _source_rows:
            st.caption("No visible leads to summarize.")
        else:
            _source_df = pd.DataFrame(_source_rows).groupby("source", as_index=False).agg(value=("value", "sum"), count=("value", "size"))
            _source_df = _source_df.sort_values("value", ascending=False)
            _source_df["label"] = _source_df["value"].apply(_fmt_usd_short)

            def _top_leads_preview(source: str) -> str:
                leads = sorted(_source_leads.get(source, []), key=lambda x: x[1], reverse=True)
                return _lead_preview([n for n, _ in leads])

            _source_df["leads_preview"] = _source_df["source"].map(_top_leads_preview)
            _max_source_val = max(_source_df["value"].max(), 1.0)
            _source_tick_vals = list(range(0, int(_max_source_val // 100_000 + 2) * 100_000, 100_000))
            _bars4 = alt.Chart(_source_df).mark_bar(color="#34D399", size=48).encode(
                x=alt.X("source:N", title=None, sort="-y", axis=alt.Axis(labelAngle=-30, labelAlign="right", labelOverlap=False)),
                y=alt.Y("value:Q", title="Total deal value (USD)", axis=alt.Axis(values=_source_tick_vals, labelExpr=_pi_usd_axis_expr)),
                tooltip=[
                    alt.Tooltip("source:N", title="Source"), alt.Tooltip("count:Q", title="Leads", format="d"),
                    alt.Tooltip("value:Q", title="Total value", format="$,.0f"), alt.Tooltip("leads_preview:N", title="Top leads"),
                ],
            )
            _labels4 = alt.Chart(_source_df).mark_text(dy=-10, fontWeight="bold", color=_pi_label_color).encode(
                x=alt.X("source:N", sort="-y"), y=alt.Y("value:Q"), text=alt.Text("label:N"),
            )
            _chart4 = (_bars4 + _labels4).properties(width=_bar_width(len(_source_df)), height=280, padding={"top": 24, "bottom": 5, "left": 5, "right": 5})
            st.altair_chart(_style_chart(_chart4), use_container_width=False, theme="streamlit")

    st.divider()
    _row3_left, _row3_right = st.columns(2, gap="large")

    # ---- Chart 5: Findings Mix by Owner (stacked columns) ------------------
    with _row3_left:
        st.markdown("##### Findings Mix by Owner")
        st.caption("Finding category counts per owner. Orphan leads (not in CRM, no owner) are excluded — see the 'Not in CRM' category elsewhere.")
        if not analyses:
            st.info("Click **Refresh** at the top of the page to populate this chart.")
        else:
            _FINDINGS_MIX_CATEGORIES = ["mismatches", "missing_info", "followup_open_questions", "deprioritize"]
            # Chart-local short labels so the legend never truncates - doesn't
            # touch CATEGORIES_BY_KEY (shared with the Categories tab elsewhere).
            _chart5_labels = {"mismatches": "Mismatches", "missing_info": "Missing info", "followup_open_questions": "Follow-ups", "deprioritize": "Deprioritize"}
            _mix_rows = []
            for cf in case_files:
                if cf.lead_id not in visible_lead_ids:
                    continue
                a = analyses.get(cf.lead_id)
                if not a:
                    continue
                owner = cf.crm_row.get("owner") or UNASSIGNED_OWNER_LABEL
                counts = {
                    "mismatches": len(a.mismatches),
                    "missing_info": len(a.missing_info_flags),
                    "followup_open_questions": len(a.timing_signals) + len(a.open_questions_or_dropped_commitments),
                    "deprioritize": len(a.deprioritize_signals),
                }
                for cat_key in _FINDINGS_MIX_CATEGORIES:
                    if counts[cat_key] > 0:
                        _mix_rows.append({
                            "owner": owner, "category": _chart5_labels[cat_key], "count": counts[cat_key],
                            "company": cf.crm_row.get("company", cf.lead_id),
                        })
            if not _mix_rows:
                st.caption("No findings among visible, analyzed leads.")
            else:
                _mix_rows_df = pd.DataFrame(_mix_rows)
                _mix_df = _mix_rows_df.groupby(["owner", "category"], as_index=False)["count"].sum()

                def _top_lead_in_segment(row) -> str:
                    seg = _mix_rows_df[(_mix_rows_df["owner"] == row["owner"]) & (_mix_rows_df["category"] == row["category"])]
                    top = seg.sort_values("count", ascending=False).iloc[0]
                    extra = len(seg) - 1
                    return f"{top['company']} ({int(top['count'])})" + (f" +{extra} more" if extra > 0 else "")

                _mix_df["top_lead"] = _mix_df.apply(_top_lead_in_segment, axis=1)
                _cat_labels = [_chart5_labels[k] for k in _FINDINGS_MIX_CATEGORIES]
                _cat_colors = [CATEGORIES_BY_KEY[k]["color"] for k in _FINDINGS_MIX_CATEGORIES]
                _owner_totals = _mix_df.groupby("owner")["count"].sum()
                _chart5 = alt.Chart(_mix_df).mark_bar(size=48).encode(
                    x=alt.X("owner:N", title=None, axis=alt.Axis(labelAngle=-30, labelAlign="right", labelOverlap=False)),
                    y=_int_y("count", "Findings", _owner_totals.max()),
                    color=alt.Color("category:N", title="Category", scale=alt.Scale(domain=_cat_labels, range=_cat_colors), legend=alt.Legend(labelLimit=130)),
                    order=alt.Order("category:N", sort="ascending"),
                    tooltip=[
                        alt.Tooltip("owner:N", title="Owner"), alt.Tooltip("category:N", title="Category"),
                        alt.Tooltip("count:Q", title="Findings", format="d"), alt.Tooltip("top_lead:N", title="Top lead"),
                    ],
                ).properties(width=_bar_width(_mix_df["owner"].nunique()), height=320, padding={"top": 10, "bottom": 5, "left": 5, "right": 5})
                st.altair_chart(_style_chart(_chart5), use_container_width=False, theme="streamlit")

    # ---- Chart 6: Competitor Mentions (Phase 2) -----------------------------
    with _row3_right:
        st.markdown("##### Competitor Mentions")
        st.caption("Competing vendors/consultancies explicitly named as an alternative to AtliQ — not just a named tool/tech stack preference (see Drill Into a Lead for those). Verbatim-quotable, same grounding rule as every other finding. Requires PROMPT_VERSION v5.")
        if not analyses:
            st.info("Click **Refresh** at the top of the page to populate this chart.")
        else:
            _competitor_rows = []
            _competitor_detail: dict[str, list[tuple[str, str]]] = {}
            for cf in case_files:
                if cf.lead_id not in visible_lead_ids:
                    continue
                a = analyses.get(cf.lead_id)
                if a and a.competitor_mentioned:
                    name = (a.competitor_mentioned.get("competitor_name") or "").strip()
                    if name:
                        _competitor_rows.append(name)
                        _competitor_detail.setdefault(name, []).append(
                            (cf.crm_row.get("company", cf.lead_id), a.competitor_mentioned.get("evidence_quote", ""))
                        )
            if not _competitor_rows:
                st.caption("No competitor mentions found among visible, analyzed leads.")
            else:
                _comp_counts = pd.Series(_competitor_rows).value_counts().reset_index()
                _comp_counts.columns = ["competitor", "count"]

                def _comp_hover_lead(name: str) -> str:
                    detail = _competitor_detail.get(name, [])
                    lead, _ = detail[0]
                    extra = len(detail) - 1
                    return f"{lead} +{extra} more" if extra > 0 else lead

                def _comp_hover_quote(name: str) -> str:
                    detail = _competitor_detail.get(name, [])
                    return _truncate(detail[0][1])

                _comp_counts["hover_lead"] = _comp_counts["competitor"].map(_comp_hover_lead)
                _comp_counts["hover_quote"] = _comp_counts["competitor"].map(_comp_hover_quote)
                _bars6 = alt.Chart(_comp_counts).mark_bar(color="#8B5CF6", size=48).encode(
                    x=alt.X("competitor:N", title=None, sort="-y", axis=alt.Axis(labelAngle=-30, labelAlign="right", labelOverlap=False)),
                    y=_int_y("count", "Leads", _comp_counts["count"].max()),
                    tooltip=[
                        alt.Tooltip("competitor:N", title="Competitor"), alt.Tooltip("count:Q", title="Leads", format="d"),
                        alt.Tooltip("hover_lead:N", title="Lead"), alt.Tooltip("hover_quote:N", title="Quote"),
                    ],
                )
                _labels6 = alt.Chart(_comp_counts).mark_text(dy=-10, fontWeight="bold", color=_pi_label_color).encode(
                    x=alt.X("competitor:N", sort="-y"), y=alt.Y("count:Q"), text=alt.Text("count:Q", format="d"),
                )
                _chart6 = (_bars6 + _labels6).properties(width=_bar_width(len(_comp_counts)), height=260, padding={"top": 24, "bottom": 5, "left": 5, "right": 5})
                st.altair_chart(_style_chart(_chart6), use_container_width=False, theme="streamlit")

if selected_tab == "drill_down":
    # ----------------------------------------------------------------------
    # Drill-down
    # ----------------------------------------------------------------------
    st.header("Drill into a lead")
    options = ["—"] + [f"{cf.crm_row['company']} ({cf.lead_id})" for cf in case_files] + [f"{o.company_guess} (not in CRM)" for o in orphans]
    choice = st.selectbox("Company / lead", options)

    if choice != "—":
        dup_note = None
        if "(not in CRM)" in choice:
            guess = choice.replace(" (not in CRM)", "")
            o = next(o for o in orphans if o.company_guess == guess)
            a = analyses.get("ORPHAN::" + o.company_guess)
            st.subheader(guess)
            st.write("**CRM record:** none — this company does not appear in the CRM.")
            docs = o.docs
            status_label = "Not in CRM"
        else:
            lead_id = choice.rsplit("(", 1)[1].rstrip(")")
            cf = case_files_by_id[lead_id]
            a = analyses.get(lead_id)
            st.subheader(f"{cf.crm_row['company']} ({lead_id})")
            # Fix 2: lead source, surfaced explicitly (it's also in the full CRM
            # dump below, but not prominently, since that table lists every field).
            if cf.crm_row.get("source"):
                st.markdown(
                    f'<span class="icon-badge" style="color:#4b5563;background:rgba(107,114,128,0.12);">'
                    f'<span class="msi">source</span>{cf.crm_row["source"]}</span>',
                    unsafe_allow_html=True,
                )
            # Compact 3-column label/value grid instead of a tall single-
            # column table - same fields/values, just denser, so the
            # Opportunity Timeline below starts much sooner on the page.
            _crm_fields = list(cf.crm_row.items())
            _n_cols = 3
            _detail_cols = st.columns(_n_cols)
            for _i, (_field_key, _field_val) in enumerate(_crm_fields):
                with _detail_cols[_i % _n_cols]:
                    _display_val = str(_field_val) if _field_val is not None and _field_val == _field_val else ""
                    st.markdown(
                        '<div style="margin-bottom:12px;">'
                        f'<div style="font-size:0.72rem;color:#A79FB8;text-transform:uppercase;letter-spacing:0.03em;">{_field_key}</div>'
                        f'<div style="font-size:0.95rem;font-weight:700;color:#F1EEF7;">{_display_val}</div>'
                        '</div>',
                        unsafe_allow_html=True,
                    )
            docs = cf.docs
            status_label = cf.crm_row.get("status") or "Unknown"
            # Fix 4 (consolidation): surface the duplicate relationship here too,
            # same as the shared lead card - this is the same information whether
            # reached by search or by browsing.
            dup_note = duplicate_note_for_lead(lead_id)
            if dup_note:
                st.markdown(category_tag_html("duplicates") + f" {dup_note['text']}", unsafe_allow_html=True)

        render_opportunity_timeline(docs, a, status_label)

        if a:
            # Findings no longer get their own standalone section here - that
            # duplicated the Opportunity Timeline above. Each timeline dot now
            # carries its own "Show evidence" expander with the same CRM-says/
            # quote/source detail this section used to show individually.
            if a.suggested_crm_update:
                st.markdown(f'<div class="atliq-suggestion">Suggested CRM update: {a.suggested_crm_update} (not applied automatically)</div>', unsafe_allow_html=True)
            if a.suggested_next_action:
                st.markdown(f'<div class="atliq-suggestion">Suggested action (draft only — nothing is sent automatically): {a.suggested_next_action}</div>', unsafe_allow_html=True)
            if a.tool_preference_mentioned:
                # Informational only, not a competitive signal - see
                # llm_client.py's competitor_mentioned/tool_preference_mentioned
                # split. Routed here instead of the Competitor Mentions chart.
                _tool_name = a.tool_preference_mentioned.get("tool_name", "")
                _tool_quote = a.tool_preference_mentioned.get("evidence_quote", "")
                st.markdown(f'<div class="atliq-suggestion">Tool/stack preference mentioned: <strong>{_tool_name}</strong> — “{_tool_quote}”</div>', unsafe_allow_html=True)
            if a.dropped_findings:
                with st.expander(f"{len(a.dropped_findings)} finding(s) dropped for failing the evidence-grounding check"):
                    st.json(a.dropped_findings)

            # One Gmail flow, everywhere - same shared component This Week's
            # Attention/category cards use, not a second implementation.
            render_followup_email_section(a, dup_note, key_suffix=f"drilldown_{a.key}")

        render_linked_documents(docs)


# --------------------------------------------------------------------------
# Dashboard Assistant: a template-driven help widget - "AI as an assistive
# layer," not a conversational agent. Zero Claude calls anywhere in this
# section: dashboard-logic questions are fixed hardcoded template text
# (see src/coach.py), and the two "Dashboard Insights" questions just
# reformat the already-computed AttentionItem ranking (same data This
# Week's Attention already uses) into a sentence - plain Python, no API call.
# --------------------------------------------------------------------------
st.session_state.setdefault("coach_open", False)
st.session_state.setdefault("coach_identity", None)  # None until Step 1 answered
st.session_state.setdefault("coach_messages", [])
st.session_state.setdefault("coach_input_nonce", 0)
st.session_state.setdefault("coach_current_prompts", None)  # (help_qs, insight_qs) for the current turn


def _coach_lead_rows(owner_crm_value: str | None) -> list[dict]:
    """One compact row per currently-visible lead, optionally scoped to one
    CRM owner value (or 'unassigned leads' when owner_crm_value is
    UNASSIGNED_OWNER_LABEL) - the Dashboard Assistant's own remembered
    identity from Step 1, independent of the dashboard's own owner buttons.
    Carries a few extra already-known fields (deal value, staleness, flag
    counts) purely so the 15 Insights prompts can filter/count without any
    new scoring logic - all values come straight from the existing
    LeadAnalysis/CRM row."""
    rows = []
    for a in va:
        item = attention_by_key.get(a.key)
        if item is None:
            continue
        lead_owner = (a.crm_row or {}).get("owner") or UNASSIGNED_OWNER_LABEL
        if owner_crm_value and lead_owner != owner_crm_value:
            continue
        findings = all_individual_findings(a)
        top_finding = findings[0]["text"] if findings else "no findings on record"
        crm_row = a.crm_row or {}
        deal_value = crm_row.get("est_value_usd")
        deal_value = float(deal_value) if deal_value is not None and deal_value == deal_value else None
        last_contact = pd.to_datetime(crm_row.get("last_contact_date") or None, errors="coerce")
        days_stale = (pd.Timestamp(TODAY) - last_contact).days if pd.notna(last_contact) else None
        unanswered = sum(1 for q_ in a.open_questions_or_dropped_commitments if q_.get("kind") == "unanswered_question")
        rows.append({
            "company": a.company,
            "tag": a.lead_id if a.lead_id else "not in CRM",
            "owner": lead_owner,
            "urgency_level": {"red": "Urgent", "yellow": "Check", "green": "Watch"}.get(item.urgency_level, item.urgency_level),
            "score": item.score,
            "top_finding": top_finding,
            "missing_info_count": len(a.missing_info_flags),
            "deprioritize_count": len(a.deprioritize_signals),
            "unanswered_questions_count": unanswered,
            "deal_value": deal_value,
            "days_stale": days_stale,
            "status": (crm_row.get("status") or "").strip().lower(),
            "has_contact_email": bool((crm_row.get("contact_email") or "").strip()),
            "has_action": bool(a.suggested_next_action),
        })
    return rows


ALL_COMPANY_NAMES = [a.company for a in va]


def _add_message(role: str, content: str, show_contact_button: bool = False) -> None:
    st.session_state["coach_messages"].append({"role": role, "content": content, "show_contact_button": show_contact_button})


def _build_insight_dispatch(rows: list[dict], owner_label: str) -> dict:
    """Maps each of the 15 exact Insights prompts to a formatter call over
    data already computed elsewhere in this file (duplicate_pairs_visible,
    orphans_visible, the memory trail, the cached Executive Summary,
    VALUE_THRESHOLD/STALENESS_THRESHOLD) - no new AI call, no new scoring."""
    dup_pair_labels = [f"{p.company_a} ({p.lead_id_a}) ↔ {p.company_b} ({p.lead_id_b})" for p in duplicate_pairs_visible]
    orphan_names = [o.company_guess for o in orphans_visible]
    persisting_clients = _trail["persisting_clients"] if _trail else []
    n_new = len(_trail["new_by_client"]) if _trail else 0
    n_resolved = len(_trail["resolved_by_client"]) if _trail else 0
    n_persisting = len(persisting_clients)
    exec_summary = st.session_state.get("exec_summary")

    return {
        "What are my Top 3 priorities today?": lambda: format_top_priorities(rows, owner_label),
        "Which leads need immediate attention?": lambda: format_urgent_leads(rows, owner_label),
        "How many leads are currently flagged Urgent?": lambda: format_urgent_count(rows, owner_label),
        "Which leads have been persisting the longest?": lambda: format_persisting_longest(persisting_clients),
        "What's changed since my last check?": lambda: format_since_last_check(n_new, n_resolved, n_persisting),
        "Which leads are missing key information?": lambda: format_missing_info_leads(rows),
        "Which leads look like duplicates right now?": lambda: format_duplicate_pairs(dup_pair_labels),
        "Which companies aren't in the CRM yet?": lambda: format_orphans(orphan_names),
        "What are the Hidden Opportunities right now?": lambda: format_hidden_opportunities(rows),
        "Which leads should I consider deprioritizing?": lambda: format_deprioritize_candidates(rows),
        "What's my highest-value deal that's currently stale?": lambda: format_highest_value_stale(rows),
        "How many leads are in the Quick Wins quadrant?": lambda: format_quick_wins_count(rows, VALUE_THRESHOLD, STALENESS_THRESHOLD),
        "Which leads have unanswered open questions?": lambda: format_unanswered_questions(rows),
        "What does today's Executive Summary say?": lambda: format_exec_summary_echo(exec_summary),
        "Which leads have a suggested action ready to send?": lambda: format_ready_to_send(rows),
    }


def _sample_fresh_prompts() -> tuple[list[str], list[str]]:
    """Rotation: 3 Dashboard Help + 2 Insights, sampled fresh each time,
    avoiding an identical repeat of the immediately-previous set where
    possible - pure random.sample over the fixed 30-question pool, no data
    lookup and no AI call involved in the sampling itself."""
    previous = st.session_state.get("coach_current_prompts")
    for _ in range(5):
        help_qs = random.sample(HELP_QUESTIONS, 3)
        insight_qs = random.sample(INSIGHT_QUESTIONS, 2)
        if previous is None or (help_qs, insight_qs) != previous:
            break
    return help_qs, insight_qs


def _handle_question(question: str) -> None:
    _add_message("user", question)
    identity = st.session_state["coach_identity"]
    owner_crm_value = OWNER_DISPLAY_TO_CRM.get(identity, UNASSIGNED_OWNER_LABEL if identity == "Unassigned" else None)
    owner_label = "you" if identity in (None, "Unassigned") else identity

    # 1) Exact match against the 15 Dashboard Help templates (guaranteed
    # correct routing for button clicks, since these are fixed strings).
    if question in HELP_TEMPLATES:
        _add_message("assistant", HELP_TEMPLATES[question])
    # 2) Exact match against the 15 Dashboard Insights prompts.
    elif question in INSIGHT_QUESTIONS:
        rows = _coach_lead_rows(owner_crm_value)
        dispatch = _build_insight_dispatch(rows, owner_label)
        _add_message("assistant", dispatch[question]())
    else:
        # 3) Free-typed question: fuzzy keyword match against the same
        # Dashboard Help content, then a client-name check, then fallback.
        template = match_template(question)
        if template:
            _add_message("assistant", template)
        elif mentions_known_company(question, ALL_COMPANY_NAMES):
            _add_message("assistant", CLIENT_SPECIFIC_MESSAGE)
        else:
            _add_message("assistant", FALLBACK_MESSAGE, show_contact_button=True)

    # Rotation: a fresh 3+2 sample is ready for the NEXT turn, whether this
    # question came from a button or free text.
    st.session_state["coach_current_prompts"] = _sample_fresh_prompts()


# --------------------------------------------------------------------------
# Feature 1's floating launcher: an invisible real button (for the actual
# click) with a decorative animated SVG hourglass layered on top via
# pointer-events:none - same pattern the chat launcher already uses for its
# icon, just with a live CSS animation instead of a static image. Idle:
# looping, subtle. Just-opened: one-shot "pour/flip", then settles.
# --------------------------------------------------------------------------
st.session_state.setdefault("workplan_open", False)
st.session_state.setdefault("workplan_just_opened", False)

# Both floating launchers (hourglass + Dashboard Assistant) are only shown on
# the This Week's Attention and Overview tabs - cleanly absent elsewhere,
# rather than rendered-but-inert.
if selected_tab in ("attention", "overview"):
    _poured_class = "poured" if st.session_state["workplan_just_opened"] else ""
    st.markdown(
        f'<div class="hourglass-icon-wrap {_poured_class}">'
        f'<img class="hourglass-frame" src="data:image/png;base64,{WORKPLAN_HOURGLASS_ICON_B64}" width="34" height="34" alt="30-Minute Work Plan"/>'
        '</div>',
        unsafe_allow_html=True,
    )
    # The pour animation is only for the render that just followed the click -
    # consumed immediately after so later, unrelated reruns don't replay it.
    st.session_state["workplan_just_opened"] = False

    if st.button(" ", key="workplan_toggle_btn", help="30-Minute Work Plan"):
        opening = not st.session_state["workplan_open"]
        st.session_state["workplan_open"] = opening
        st.session_state["workplan_just_opened"] = opening
        st.rerun()

    if st.session_state["workplan_open"]:
        with st.container(key="workplan_panel_container"):
            st.markdown(
                '<div class="coach-header"><span class="msi" style="font-size:22px;color:#8b5cf6;">hourglass_top</span>'
                '<div style="margin-left:8px;"><b>If you only have 30 minutes today...</b></div></div>',
                unsafe_allow_html=True,
            )
            if st.button("Close", key="workplan_close_btn", use_container_width=True):
                st.session_state["workplan_open"] = False
                st.rerun()
            render_thirty_minute_plan()

    # Dot + button share one positioned wrapper now (was two independently
    # fixed-positioned siblings with hand-tuned offsets that drifted out of
    # sync whenever the icon's own effective size changed) - see CSS: the
    # wrapper is the single position:fixed anchor, the button fills it via
    # inset:0, and the dot is position:absolute relative to that SAME
    # wrapper, anchored to the visible icon's actual top-right corner.
    with st.container(key="coach_launcher_wrap"):
        st.markdown('<span class="coach-pulse-dot"></span>', unsafe_allow_html=True)
        if st.button(" ", key="coach_toggle_btn", help="Dashboard Assistant"):
            st.session_state["coach_open"] = not st.session_state["coach_open"]
            st.rerun()

if selected_tab in ("attention", "overview") and st.session_state["coach_open"]:
    with st.container(key="coach_panel_container"):
        st.markdown(
            '<div class="coach-header"><div class="coach-avatar">'
            f'<img src="data:image/png;base64,{COACH_AVATAR_ICON_B64}" alt="Dashboard Assistant" /></div>'
            '<div><b>Dashboard Assistant</b><div style="font-size:0.75rem;opacity:0.7;">'
            'Helps you use this dashboard - not a chatbot</div></div></div>',
            unsafe_allow_html=True,
        )
        if st.button("Close", key="coach_close_btn", use_container_width=True):
            st.session_state["coach_open"] = False
            st.rerun()

        # Step 1 - user identification: remembered for the rest of the
        # session, used to scope the two "Dashboard Insights" questions.
        if st.session_state["coach_identity"] is None:
            st.markdown("**Who are you?**")
            id_cols = st.columns(2)
            id_options = ["Karan", "Jay", "Dhaval", "Bhavin"]
            for i, name in enumerate(id_options):
                with id_cols[i % 2]:
                    if st.button(name, key=f"coach_identity_{name}", use_container_width=True):
                        st.session_state["coach_identity"] = name
                        st.rerun()
            if st.button("View Unassigned Leads", key="coach_identity_unassigned", use_container_width=True):
                st.session_state["coach_identity"] = "Unassigned"
                st.rerun()
        else:
            st.caption(f"Signed in as **{st.session_state['coach_identity']}**")

            for m in st.session_state["coach_messages"]:
                role_class = "user" if m["role"] == "user" else "assistant"
                label = "You" if m["role"] == "user" else "Assistant"
                st.markdown(
                    f'<div class="coach-bubble {role_class}"><b>{label}:</b> {m["content"]}</div>',
                    unsafe_allow_html=True,
                )
                if m.get("show_contact_button"):
                    st.link_button("Contact Human", f"mailto:{CONTACT_EMAIL}", icon=":material/support_agent:")

            # Step 2 - "How can I help you today?": first 3 are primary
            # (Dashboard Help), last 2 secondary (Dashboard Insights) - same
            # consistent response shape either way (see src/coach.py:
            # templates open with a direct one-line answer, then specifics).
            # Persistent + rotating: a fresh 3+2 sample is shown after EVERY
            # answer (not just once at the start) - _handle_question already
            # refreshes coach_current_prompts each time it runs.
            if st.session_state["coach_current_prompts"] is None:
                st.session_state["coach_current_prompts"] = _sample_fresh_prompts()
            help_qs, insight_qs = st.session_state["coach_current_prompts"]

            st.markdown("**How can I help you today?**")
            st.caption("Dashboard Help")
            for i, hq in enumerate(help_qs):
                if st.button(hq, key=f"coach_help_prompt_{i}", use_container_width=True, type="primary"):
                    _handle_question(hq)
                    st.rerun()
            st.caption("Dashboard Insights")
            for i, iq in enumerate(insight_qs):
                if st.button(iq, key=f"coach_insight_prompt_{i}", use_container_width=True):
                    _handle_question(iq)
                    st.rerun()

            coach_input = st.text_input(
                "Ask a question", key=f"coach_text_input_{st.session_state['coach_input_nonce']}",
                label_visibility="collapsed", placeholder="Or type your own question...",
            )
            if st.button("Send", key="coach_send_btn", use_container_width=True) and coach_input.strip():
                _handle_question(coach_input.strip())
                st.session_state["coach_input_nonce"] += 1
                st.rerun()
