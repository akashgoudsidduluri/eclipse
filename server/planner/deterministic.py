"""Deterministic no-key navigation (PR #7, W3).

Maps a task goal ("open google", "go to youtube", "open
https://github.com/...") to a navigate step with ZERO model
calls — the live-test failure was "open google" creating a task
that then failed "Couldn't plan this task" because every planner
endpoint was dead on the laptop (no keys, no Ollama). A pure
navigation goal never needed a model in the first place.

Resolution order for the object of "open / go to / visit":
  1. a full http(s) URL (used as-is, after the allow-list check);
  2. a bare domain ("google.com", "www.youtube.com") — https is
     assumed, exactly as a browser's address bar does;
  3. a known site name ("google", "youtube", "github", ...).

Anything else — "open the settings", "open google and search
for cats", a goal that is not a navigation at all — returns
None, and the caller falls through to the planner path. The
resolver is deliberately strict: it only fires when the WHOLE
goal is one navigation, so it can never hijack a multi-part
task.

The allow-list check is the EXISTING one: the resolved URL is
run through validate.validate_action (navigate) with the task's
allowed sites, so a task constrained to other sites never opens
a deterministic URL (resolve returns None and the planner path
— which enforces the same list — takes over). With no
allow-list and no current page, validate accepts any http(s)
URL (its documented "neither is known" behaviour); the URL
here comes from the user's own words, not from page text, so
there is no injection surface for a page to exploit.

The plan a resolved goal produces is one step that already
carries its action ({"action": "navigate", "url": ...}):
loop.execute_step runs such a step WITHOUT a planner call (see
the deterministic branch there), so "open google" works end to
end with no keys configured and no Ollama running. Planner-made
plans never carry "action"/"url" keys (validate.parse_plan
strips them), so only deterministic plans take that branch.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

from . import validate

# Known sites: name (and common aliases) -> home URL. A starter
# list — the sites the laptop live tests and the college demos
# actually name. Bare domains resolve without this map, so an
# unlisted site still works when the user names its domain.
KNOWN_SITES: dict[str, str] = {
    "google": "https://www.google.com",
    "youtube": "https://www.youtube.com",
    "github": "https://github.com",
    "linkedin": "https://www.linkedin.com",
    "internshala": "https://internshala.com",
    "twitter": "https://x.com",
    "x": "https://x.com",
    "wikipedia": "https://www.wikipedia.org",
    "stackoverflow": "https://stackoverflow.com",
    "stack overflow": "https://stackoverflow.com",
    "reddit": "https://www.reddit.com",
    "gmail": "https://mail.google.com",
    "outlook": "https://outlook.live.com",
    "leetcode": "https://leetcode.com",
    "codechef": "https://www.codechef.com",
    "codeforces": "https://codeforces.com",
    "hackerrank": "https://www.hackerrank.com",
    "naukri": "https://www.naukri.com",
    "indeed": "https://www.indeed.com",
    "amazon": "https://www.amazon.in",
    "flipkart": "https://www.flipkart.com",
    "notion": "https://www.notion.so",
    "figma": "https://www.figma.com",
    "chatgpt": "https://chatgpt.com",
    "claude": "https://claude.ai",
    "mdn": "https://developer.mozilla.org",
}

# Verbs that start a navigation goal. "show me" only counts with
# a site object ("show me youtube" — "show me the jobs" is not
# a navigation and resolves to None below).
_NAV_VERBS = (
    "open", "go to", "goto", "visit", "navigate to", "load",
    "show me", "take me to",
)

# Polite prefixes stripped before matching ("please open
# google", "hey, can you open youtube").
_PREFIXES = (
    "please", "hey", "hi", "hello", "eclipse",
    "can you", "could you", "would you", "will you",
)

# Trailing words stripped from the object ("open google website").
_OBJECT_SUFFIXES = ("website", "site", "web site", "app", "homepage", "home page", "page")

# A bare domain host: labels separated by dots, at least one dot.
# Deliberately ASCII-only and strict — "open the settings page"
# must not look like a domain. The path/query (if any) is split
# off before matching, so its case is preserved (URL paths are
# case-sensitive; domains are not).
_BARE_HOST_RE = re.compile(
    r"^(?:www\.)?[a-z0-9](?:[a-z0-9-]*[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+$"
)


def _normalize_ws(text: str) -> str:
    """Strip punctuation/whitespace, collapse spaces. Case is
    preserved — URL paths are case-sensitive."""
    cleaned = (text or "").strip().strip(" .!?,;:'\"()[]")
    return " ".join(cleaned.split())


def _clean(text: str) -> str:
    """_normalize_ws + lowercase (for matching verbs, prefixes
    and known-site names — never for a URL itself)."""
    return _normalize_ws(text).lower()


def _strip_prefixes(text: str) -> str:
    """Strip polite prefixes repeatedly ("hey, please open ...").
    Matching is case-insensitive, but the remainder keeps its
    original case (a URL object must not be lowercased)."""
    cleaned = _normalize_ws(text)
    changed = True
    while changed:
        changed = False
        lowered = cleaned.lower()
        for prefix in _PREFIXES:
            if lowered.startswith(prefix + " ") or lowered == prefix:
                cleaned = _normalize_ws(cleaned[len(prefix):])
                changed = True
                break
            if lowered.startswith(prefix + ","):
                cleaned = _normalize_ws(cleaned[len(prefix) + 1:])
                changed = True
                break
    return cleaned


def _split_navigation(text: str) -> str | None:
    """The object of a navigation goal ("google" from "open
    google"), or None when the goal is not a bare navigation.

    The verb must be at the start (after polite prefixes) and
    the object must be the rest of the goal — "open google and
    search for cats" has no bare object and returns None. The
    object keeps its original case (URL paths are
    case-sensitive); matching is case-insensitive.
    """
    cleaned = _strip_prefixes(text)
    lowered = cleaned.lower()
    for verb in sorted(_NAV_VERBS, key=len, reverse=True):
        if lowered.startswith(verb + " "):
            obj = _normalize_ws(cleaned[len(verb):])
            if obj:
                return obj
    return None


def _strip_object_suffix(obj: str) -> str:
    """Strip a trailing "website"/"site"/"app" from the object
    (matched case-insensitively, sliced from the original)."""
    lowered = obj.lower()
    for suffix in _OBJECT_SUFFIXES:
        if lowered.endswith(" " + suffix):
            return obj[: -len(suffix)].strip()
    return obj


def _object_to_url(obj: str) -> str | None:
    """Resolve a navigation object to a URL, or None.

    Known-site names match case-insensitively; a raw URL or a
    bare domain keeps its path/query case (only the host is
    lowercased — domains are case-insensitive, paths are not).
    """
    lowered = _strip_object_suffix(_clean(obj))
    if not lowered:
        return None
    if lowered in KNOWN_SITES:
        return KNOWN_SITES[lowered]
    raw = _strip_object_suffix(_normalize_ws(obj))
    if not raw or " " in raw:
        # A multi-word object that is not a known site ("the
        # settings page", "google and search") is a phrase, not
        # a destination.
        return None
    if raw.lower().startswith(("http://", "https://")):
        parsed = urlparse(raw)
        if parsed.hostname:
            return raw
        return None
    # "google.com" / "www.youtube.com/watch?v=..." — a bare
    # domain, https assumed (the address-bar rule). Split the
    # host from the path/query first: the host matches
    # lowercased, the rest keeps its case.
    cut = len(raw)
    for sep in ("/", "?"):
        idx = raw.find(sep)
        if idx != -1:
            cut = min(cut, idx)
    host_part, suffix = raw[:cut], raw[cut:]
    if _BARE_HOST_RE.match(host_part.lower()):
        return "https://" + host_part.lower() + suffix
    return None


def _url_allowed(url: str, allowed_sites: list[str] | None) -> bool:
    """The existing navigate allow-list check (validate.py):
    with an allow-list the host must match it; with none, any
    http(s) URL passes (validate's "neither is known" rule —
    there is no current page at plan time)."""
    try:
        validate.validate_action(
            {"action": "navigate", "url": url},
            set(), set(), allowed_sites=allowed_sites,
        )
    except validate.ValidationError:
        return False
    return True


def resolve_navigate(
    text: str, allowed_sites: list[str] | None = None,
) -> dict | None:
    """Resolve a navigation goal to a navigate action, or None.

    Returns {"action": "navigate", "url": ...} only when the
    whole goal is one navigation AND the URL passes the
    allow-list check. Pure: no I/O, no model, no state.
    """
    obj = _split_navigation(text)
    if obj is None:
        return None
    url = _object_to_url(obj)
    if url is None:
        return None
    if not _url_allowed(url, allowed_sites):
        return None
    return {"action": "navigate", "url": url}


def deterministic_plan(
    goal: str, constraints: str = "",
    allowed_sites: list[str] | None = None,
) -> list[dict] | None:
    """A one-step plan for a pure navigation goal, or None.

    `allowed_sites` overrides the constraints parsing (the loop
    passes its own _allowed_sites result); when omitted, an
    "allowed sites:" line is parsed from `constraints` with the
    same rule loop._allowed_sites uses.
    """
    if allowed_sites is None:
        allowed_sites = _allowed_sites_from_constraints(constraints)
    action = resolve_navigate(goal, allowed_sites)
    if action is None:
        return None
    url = action["url"]
    host = urlparse(url).hostname or url
    return [{
        "step": 1,
        "description": f"Open {url}",
        "expected_end_state": f"The browser shows {host}",
        "action": "navigate",
        "url": url,
    }]


def _allowed_sites_from_constraints(constraints: str) -> list[str] | None:
    """The same "allowed sites:" line loop._allowed_sites parses
    (kept in sync by test, not by import — importing loop here
    would be circular: loop imports this module)."""
    for line in (constraints or "").splitlines():
        if line.strip().lower().startswith("allowed sites:"):
            sites = line.split(":", 1)[1]
            return [s.strip() for s in sites.split(",") if s.strip()]
    return None
