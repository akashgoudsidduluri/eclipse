"""Mission discovery (PR #5, §3.8): find candidate items for a
mission run. Discovery is honest about its limits — there is no
fake search engine and no canned roles here.

Sources, with their limits (say them plainly; do not pretend):

- Gmail offers (job_hunt): GmailConnector.search_offers with the
  existing keyword set (offer, interview, shortlisted, selected,
  application status). Keyword match is broad (it includes
  newsletters). Mail-body role extraction is best-effort regex
  + heuristics: a low-confidence extraction becomes a
  `discovered` candidate for the user to confirm, never an
  auto-apply (the matcher still has to fit it, and a role with
  no usable SDE title never fits). Requires the Gmail connector
  enabled; when off, this source is skipped and the run notes
  it — never a failure. When the connector itself errors, the
  error propagates: the runner treats it as a transient run
  failure (retried next tick, auto-pause after 3).
- Pasted roles (job_hunt, cold_mail): roles the user supplied in
  the mission config. This is the reliable job source in PR #5:
  Eclipse cannot *find* jobs, only score roles it is given or
  that arrive by mail. New roles only — dedup by role id
  against the mission's `discovered` activity rows.
- Watch events (price_watch): the tick reads watch_events since
  the last run and turns firings into price candidates. Only
  works when the page is open in a tab — a closed tab means no
  check, no failure, just silence. The selector reads the
  FIRST matching element's text only: no lists, no structured
  extraction. "Scan a listings page for new roles" does NOT
  work with current primitives and is out of scope for PR #5.

A candidate is a dict:
  {"kind": "role" | "price_event",
   "role": {...} | None,            # RoleInput-shaped, for kind=role
   "match": {...} | None,           # matcher.match_role result
   "source": "gmail" | "pasted" | "watch",
   "needs_confirmation": bool,      # low-confidence extraction
   "detail": {...}}                 # redacted ids/counts, never PII
"""
from __future__ import annotations

import json
import re
import time

from ..connectors import registry as conn_registry
from ..jobs import matcher, profile
from ..monitor import watches as watch_mod
from ..planner import scan

# Subject prefixes stripped when guessing a role title from a
# mail subject. Best-effort only — the result is a candidate,
# never a fact.
_SUBJECT_PREFIXES = (
    "re:", "fw:", "fwd:", "interview:", "offer:", "update:",
    "application status:", "regarding:", "about:",
)

# A money amount in mail text: ₹15,000 / Rs. 15000 / INR 15000 /
# 15k (with "stipend"/"salary"/"pay" nearby, or a ₹/Rs prefix).
_MONEY_RE = re.compile(
    r"(?:₹|Rs\.?|INR)\s*(\d[\d,]*(?:\.\d+)?)\s*(k)?",
    re.IGNORECASE,
)
_STIPEND_WORD_RE = re.compile(
    r"(?:stipend|salary|pay)[^0-9]{0,20}₹?\s*(\d[\d,]*)\s*(k)?",
    re.IGNORECASE,
)
# The number can come first: "10k stipend", "15000 stipend".
_STIPEND_AFTER_RE = re.compile(
    r"(\d[\d,]*)\s*(k)?\s*(?:stipend|salary)", re.IGNORECASE
)
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

_KNOWN_LOCATIONS = (
    "Remote", "Hyderabad", "Bangalore", "Bengaluru", "Chennai",
    "Pune", "Mumbai", "Delhi", "Noida", "Gurugram", "Kolkata",
    "Ahmedabad", "Jaipur", "Indore", "Coimbatore", "Mysore",
)


def gmail_available(conn) -> bool:
    """Is the Gmail connector on? A missing registry row is not
    "on" (the same fail-closed rule as mail/send.py)."""
    reg = conn_registry.get(conn, "gmail")
    return reg is not None and bool(reg["enabled"])


def _discovered_ids(conn, mission_id: str) -> set[str]:
    """Role ids / event ids this mission recorded as
    `discovered` in the last 24 hours (the dedup set — read
    straight from the activity table, so discovery never
    re-offers a candidate the user already saw today).

    The window is 24 hours, not forever: the runner spawns one
    task per run (§3.1), so a run that discovers five roles acts
    on one — the other four must re-surface tomorrow, or
    one-task-per-run would lose them for good. Already-applied
    roles are still protected by the already-applied gate, and
    mail-derived roles re-surface with a fresh `discovered` row
    (the log shows the re-offer; it is not silent)."""
    since = int(time.time()) - 24 * 3600
    seen: set[str] = set()
    rows = conn.execute(
        "SELECT detail FROM mission_activity"
        " WHERE mission_id = ? AND kind = 'discovered'"
        " AND created_at >= ?",
        (mission_id, since),
    ).fetchall()
    for row in rows:
        try:
            detail = json.loads(row["detail"] or "{}")
        except (ValueError, TypeError):
            continue
        for key in ("role_id", "event_id"):
            if detail.get(key):
                seen.add(str(detail[key]))
    return seen


def _profile_for_mission(conn, mission: dict) -> dict | None:
    """The stored profile, with the mission's own overrides
    applied (a mission stipend_min / locations override the
    profile's when set — the mission is the narrower scope)."""
    stored = profile.get_profile(conn)
    if stored is None:
        return None
    merged = dict(stored)
    config = mission.get("config") or {}
    if config.get("stipend_min") is not None:
        merged["stipend_min"] = config["stipend_min"]
    if config.get("locations"):
        merged["locations"] = list(config["locations"])
    return merged


def _role_id(role: dict) -> str:
    return str(role.get("id") or role.get("url") or role.get("title") or "")


def _match_candidate(role: dict, stored_profile: dict | None) -> dict | None:
    """Match one role, or None when there is no profile to match
    against (a mission with no stored profile discovers roles
    but never scores them — the runner notes it)."""
    if stored_profile is None:
        return None
    return matcher.match_role(role, stored_profile)


def discover_pasted_roles(conn, mission: dict) -> list[dict]:
    """Roles from the mission config, matched, new ones only."""
    config = mission.get("config") or {}
    stored = _profile_for_mission(conn, mission)
    seen = _discovered_ids(conn, mission["id"])
    out = []
    for role in config.get("roles") or []:
        role_id = _role_id(role)
        if role_id and role_id in seen:
            continue
        match = _match_candidate(role, stored)
        out.append({
            "kind": "role", "role": role, "match": match,
            "source": "pasted", "needs_confirmation": False,
            "detail": {"role_id": role_id},
        })
    return out


def _guess_title(subject: str, body: str) -> str:
    """Best-effort role title from a mail subject. The subject
    is already redacted by search_offers; this only reshapes it.
    Returns "" when nothing looks like a title."""
    text = (subject or "").strip()
    lowered = text.lower()
    for prefix in _SUBJECT_PREFIXES:
        if lowered.startswith(prefix):
            text = text[len(prefix):].strip()
            lowered = text.lower()
    # "Software Engineer Intern — Example Labs" / "… at Example Labs"
    for sep in (" — ", " – ", " - ", " | ", " at "):
        if sep in text:
            text = text.split(sep, 1)[0].strip()
            break
    # A title is short; a full sentence is not a title.
    if len(text) > 80 or len(text.split()) > 8:
        return ""
    return text


def _guess_company(subject: str, body: str, sender: str) -> str:
    """Best-effort company from "at <Company>" in the subject,
    else the sender's domain (example.com -> Example)."""
    match = re.search(r"\bat\s+([A-Z][A-Za-z0-9&.\- ]{1,40})",
                      subject or "")
    if match:
        return match.group(1).strip().rstrip(".,")
    sender = sender or ""
    domain_match = re.search(r"@([A-Za-z0-9.-]+)", sender)
    if domain_match:
        domain = domain_match.group(1)
        name = domain.split(".")[0]
        return name[:1].upper() + name[1:]
    return ""


def _guess_money(text: str) -> int | None:
    """Best-effort stipend from mail text. A bare number with no
    currency marker and no stipend word is not a stipend."""
    match = _MONEY_RE.search(text or "")
    if match:
        value = float(match.group(1).replace(",", ""))
        if match.group(2):
            value *= 1000
        return int(value)
    match = _STIPEND_WORD_RE.search(text or "")
    if match:
        value = int(match.group(1).replace(",", ""))
        if match.group(2):
            value *= 1000
        return value
    match = _STIPEND_AFTER_RE.search(text or "")
    if match:
        value = int(match.group(1).replace(",", ""))
        if match.group(2):
            value *= 1000
        return value
    return None


def _guess_location(text: str) -> str:
    for loc in _KNOWN_LOCATIONS:
        if re.search(rf"\b{re.escape(loc)}\b", text or "", re.IGNORECASE):
            return loc
    return ""


def _role_from_mail(mail: dict) -> tuple[dict, bool]:
    """Parse one offer mail into a candidate role. Returns
    (role, needs_confirmation). The parse is best-effort: a
    mail that yields no SDE-ish title is still returned (the
    matcher rejects it, and the user sees it as discovered) —
    discovery never invents a title or a company."""
    subject = mail.get("subject") or ""
    body = mail.get("body_redacted") or ""
    title = _guess_title(subject, body)
    company = _guess_company(subject, body, mail.get("from") or "")
    role = {
        "id": f"gmail:{mail.get('id')}",
        "title": title,
        "company": company,
        "site": "gmail",
        "url": "",
        "stipend": _guess_money(f"{subject}\n{body}"),
        "location": _guess_location(f"{subject}\n{body}"),
        "deadline": None,
        "skills": [],
        "min_year": None,
        "degree": None,
        "min_cgpa": None,
        "requires_experience": False,
    }
    # Low confidence when the title is missing or does not look
    # like an SDE role — the matcher is the second opinion, but
    # the candidate is flagged either way.
    needs_confirmation = (not title) or not matcher._sde_title(title)
    return role, needs_confirmation


def discover_gmail_offers(conn, mission: dict, gmail=None) -> list[dict]:
    """Offer/interview mails -> candidate roles. Skipped (empty
    list) when Gmail is off. A connector error propagates to
    the runner (a transient run failure, not a silent skip)."""
    if not gmail_available(conn):
        return []
    if gmail is None:
        from ..connectors.gmail import GmailConnector

        gmail = GmailConnector()
    stored = _profile_for_mission(conn, mission)
    seen = _discovered_ids(conn, mission["id"])
    out = []
    for mail in gmail.search_offers():
        role, needs_confirmation = _role_from_mail(mail)
        role_id = role["id"]
        if role_id in seen:
            continue
        match = _match_candidate(role, stored)
        out.append({
            "kind": "role", "role": role, "match": match,
            "source": "gmail",
            "needs_confirmation": needs_confirmation,
            "detail": {"role_id": role_id, "mail_id": mail.get("id")},
        })
    return out


def discover_job_hunt(conn, mission: dict, gmail=None) -> list[dict]:
    """job_hunt discovery: Gmail offers + pasted roles, per the
    mission's `sources` (default both)."""
    sources = (mission.get("config") or {}).get("sources") or ["gmail", "pasted"]
    out: list[dict] = []
    if "gmail" in sources:
        out.extend(discover_gmail_offers(conn, mission, gmail=gmail))
    if "pasted" in sources:
        out.extend(discover_pasted_roles(conn, mission))
    return out


def _roles_from_job_hunt_missions(conn, role_ids: set[str]) -> list[dict]:
    """Resolve role ids against every job-hunt mission's pasted
    roles (cold mail reuses the job-hunt shortlist — there is no
    separate role store in PR #5)."""
    found: list[dict] = []
    if not role_ids:
        return found
    rows = conn.execute(
        "SELECT config_json FROM missions WHERE type = 'job_hunt'"
    ).fetchall()
    for row in rows:
        try:
            config = json.loads(row["config_json"] or "{}")
        except (ValueError, TypeError):
            continue
        for role in config.get("roles") or []:
            if _role_id(role) in role_ids:
                found.append(role)
    return found


def discover_cold_mail(conn, mission: dict, gmail=None) -> list[dict]:
    """cold_mail discovery: the roles to mail about — given
    directly in config, or resolved by id from job-hunt
    missions. Gmail is not a source here: cold mail never
    invents recipients from the inbox (recipients are explicit
    in the config)."""
    config = mission.get("config") or {}
    stored = _profile_for_mission(conn, mission)
    seen = _discovered_ids(conn, mission["id"])
    roles: list[dict] = list(config.get("roles") or [])
    wanted = {str(r) for r in (config.get("role_ids") or [])}
    known = {_role_id(r) for r in roles}
    for role in _roles_from_job_hunt_missions(conn, wanted - known):
        roles.append(role)
    out = []
    for role in roles:
        role_id = _role_id(role)
        if role_id and role_id in seen:
            continue
        match = _match_candidate(role, stored)
        out.append({
            "kind": "role", "role": role, "match": match,
            "source": "pasted", "needs_confirmation": False,
            "detail": {"role_id": role_id},
        })
    return out


def parse_price(value: str | None) -> float | None:
    """Best-effort price from a watch value string ("₹4,999",
    "Rs. 4999.00", "$49.99", "4999"). Returns None when no
    number is found. The first number wins — a value with two
    numbers ("was ₹6,999, now ₹4,999") parses the first, so a
    target hit is always re-checkable from the stored event."""
    if not value:
        return None
    match = re.search(r"(\d[\d,]*(?:\.\d{1,2})?)", value)
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", ""))
    except ValueError:
        return None


def discover_price_watch(conn, mission: dict, gmail=None) -> list[dict]:
    """price_watch discovery: watch firings since the last run.
    The watch must exist (the runner creates it on the first
    run); without one there is nothing to discover. Events are
    new-only: since=last_run_at, plus dedup against `discovered`
    activity (a failed run must not re-offer the same firing)."""
    config = mission.get("config") or {}
    watch_id = config.get("watch_id")
    if not watch_id:
        return []
    since = mission.get("last_run_at") or 0
    seen = _discovered_ids(conn, mission["id"])
    out = []
    for event in watch_mod.list_events(conn, watch_id, limit=50, since=since):
        if event["kind"] != "fired":
            continue
        if event["id"] in seen:
            continue
        observed = parse_price(event.get("new_value"))
        out.append({
            "kind": "price_event", "role": None, "match": None,
            "source": "watch", "needs_confirmation": False,
            "detail": {
                "event_id": event["id"], "watch_id": watch_id,
                "observed_price": observed,
                "new_value": (event.get("new_value") or "")[:200],
            },
        })
    return out


def discover_email_monitor(conn, mission: dict, gmail=None) -> list[dict]:
    """email_monitor discovery (PR #7): offer/interview mails
    matching the mission's keywords, via the Gmail connector.
    Read-only — candidates are recorded (activity + facts +
    briefing), never acted on. Skipped (empty list) when Gmail
    is off; a connector error propagates to the runner (a
    transient run failure, not a silent skip)."""
    if not gmail_available(conn):
        return []
    if gmail is None:
        from ..connectors.gmail import GmailConnector

        gmail = GmailConnector()
    keywords = [
        k.strip().lower()
        for k in (mission.get("config") or {}).get("keywords") or []
        if k and k.strip()
    ]
    stored = _profile_for_mission(conn, mission)
    seen = _discovered_ids(conn, mission["id"])
    out = []
    for mail in gmail.search_offers():
        haystack = f"{mail.get('subject') or ''}\n{mail.get('body_redacted') or ''}".lower()
        if keywords and not any(k in haystack for k in keywords):
            continue
        role, needs_confirmation = _role_from_mail(mail)
        role_id = role["id"]
        if role_id in seen:
            continue
        match = _match_candidate(role, stored)
        out.append({
            "kind": "role", "role": role, "match": match,
            "source": "gmail",
            "needs_confirmation": needs_confirmation,
            "detail": {"role_id": role_id, "mail_id": mail.get("id")},
        })
    return out


def discover(conn, mission: dict, gmail=None) -> list[dict]:
    """Discover candidate items for one mission run, by type.
    Unknown types discover nothing (the registry validates
    types at creation, so this is a safety net, not a path)."""
    mission_type = mission.get("type")
    if mission_type == "job_hunt":
        return discover_job_hunt(conn, mission, gmail=gmail)
    if mission_type == "cold_mail":
        return discover_cold_mail(conn, mission, gmail=gmail)
    if mission_type == "price_watch":
        return discover_price_watch(conn, mission, gmail=gmail)
    if mission_type == "email_monitor":
        return discover_email_monitor(conn, mission, gmail=gmail)
    return []


def redact_candidate_text(text: str) -> str:
    """Redact a candidate-derived line before it is stored
    anywhere (activity detail, fact text). Mail-derived text is
    already redacted by search_offers; this is the second pass
    for anything pasted or parsed locally."""
    clean, _hits = scan.redact(text or "")
    return clean[:500]
