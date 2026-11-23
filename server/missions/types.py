"""Mission type registry (PR #5, §3.7): the "fourth type is
configuration" mechanism.

Adding a mission type = one new entry in MISSION_TYPES + its
`discover` function. No router, DB, UI-shell or runner change.
The API validates `config` against the type's schema (unknown
keys rejected — 422); the UI renders its intake form from the
same schema, served by GET /missions/types (W1).

Schema form: field -> dict with JSON-safe values
{"type", "required", "default", ...} — the dict form W1's
_missions_api._schema_to_json passes through to the UI. The
"validator" lives in the field type name, handled by
_validate_field below (never a callable in the schema — the
schema is served as JSON).

Bounds live in validate_bounds: the §3.5 bounds model, with
per-type defaults from the spec. Q1 OVERRIDE (coordinator
decision, overrides the design doc's §3.5 gate): standing
approval IS part of PR #5 — `auto_approve: true` is accepted
here, NOT rejected with a 422. The runner auto-satisfies a
pending approval only when auto_approve is true AND the action
kind is not in require_approval_for AND the action is within
bounds/caps (server/missions/bounds.py::auto_approvable); every
consequential action still goes through the pending-approval
mechanism — nothing bypasses it.
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urlparse

# The discover callables live in discovery.py (W2). Imported at
# module level: discovery imports nothing from server.missions,
# so there is no cycle. The `act` callables live in runner.py
# (runner imports this module, so this module cannot import
# runner) — act dispatch is runner._act_for_type, and the spec's
# `act` field stays None. That is a deliberate deviation from
# the §3.7 sketch: a fourth type still needs one runner branch
# for its actions, but no router/DB/UI change.
from .discovery import (  # noqa: E402
    discover_cold_mail,
    discover_email_monitor,
    discover_job_hunt,
    discover_price_watch,
)

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

APPROVAL_KINDS = ("apply", "send")

# Fields every mission type carries: the run cadence (§3.6:
# default 60 min, floor 15 — the same floor as watches) and the
# fact-store budget (W5: the runner reads max_facts from the
# mission config; 0 disables fact storage for the mission).
COMMON_FIELDS: dict[str, dict] = {
    "interval_min": {
        "type": "integer", "required": False, "default": 60,
        "min": 15, "max": 1440,
        "description": "Minutes between runs (minimum 15).",
    },
    "max_facts": {
        "type": "integer", "required": False, "default": 1000,
        "min": 0,
        "description": "Fact-store budget for this mission (0 disables fact storage).",
    },
}

# Role shape, shared with POST /jobs/match (RoleIn,
# server/api/jobs_api.py). A role needs an id (the dedup key)
# and a title (the matcher needs it); everything else is
# optional. Unknown keys are rejected, like the rest of config.
ROLE_FIELDS: dict[str, str] = {
    "id": "string", "title": "string", "company": "string",
    "site": "string", "url": "string", "stipend": "integer_or_null",
    "location": "string", "deadline": "string_or_null",
    "skills": "string_list", "min_year": "integer_or_null",
    "degree": "string_or_null", "min_cgpa": "number_or_null",
    "requires_experience": "boolean",
}

# The §3.5 bounds model. Defaults per type come from the spec's
# default_bounds; this table is the shape (field -> kind).
BOUNDS_FIELDS: dict[str, str] = {
    "max_actions_per_day": "int_ge_0",
    "max_applies_per_day": "int_ge_0",
    "max_sends_per_day": "int_ge_0",
    "max_spend_inr": "int_ge_0",
    "allowed_sites": "string_list",
    "require_approval_for": "approval_list",
    "auto_approve": "boolean",
    "quiet_hours_ist": "quiet_hours",
    "min_score": "int_0_100",
}


@dataclass
class MissionTypeSpec:
    type: str
    label: str
    config_schema: dict
    default_bounds: dict
    default_interval_min: int = 60
    # Discovery for this type: (conn, mission, gmail=None) ->
    # candidate items. Bound in the registry below.
    discover: Callable | None = None
    # Action dispatch lives in runner._act_for_type (see the
    # module docstring) — this field is None by design.
    act: Callable | None = field(default=None, repr=False)


def _is_int(value) -> bool:
    """An int, not a bool (isinstance(True, int) is True)."""
    return isinstance(value, int) and not isinstance(value, bool)


def _is_num(value) -> bool:
    return (_is_int(value) or isinstance(value, float)) and value == value


def _check_url(value, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    parsed = urlparse(value.strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(
            f"{field_name} must be an http(s) URL with a host,"
            f" got {value!r}"
        )
    return value.strip()


def _check_time(value, field_name: str) -> str:
    if not isinstance(value, str) or not _TIME_RE.match(value):
        raise ValueError(
            f"{field_name} must be HH:MM (24-hour), got {value!r}"
        )
    return value


def _check_role(role, where: str) -> dict:
    if not isinstance(role, dict):
        raise ValueError(f"{where} must be an object")
    unknown = sorted(set(role) - set(ROLE_FIELDS))
    if unknown:
        raise ValueError(f"{where} has unknown fields: {', '.join(unknown)}")
    role_id = role.get("id")
    title = role.get("title")
    if not isinstance(role_id, str) or not role_id.strip() or len(role_id) > 200:
        raise ValueError(f"{where} needs an id (1-200 characters)")
    if not isinstance(title, str) or not title.strip() or len(title) > 200:
        raise ValueError(f"{where} needs a title (1-200 characters)")
    out = {"id": role_id.strip(), "title": title.strip()}
    for key, kind in ROLE_FIELDS.items():
        if key in ("id", "title") or key not in role:
            continue
        value = role[key]
        if kind == "string":
            if not isinstance(value, str):
                raise ValueError(f"{where}.{key} must be a string")
            out[key] = value
        elif kind == "string_or_null":
            if value is not None and not isinstance(value, str):
                raise ValueError(f"{where}.{key} must be a string or null")
            out[key] = value
        elif kind == "integer_or_null":
            if value is not None and not _is_int(value):
                raise ValueError(f"{where}.{key} must be an integer or null")
            out[key] = value
        elif kind == "number_or_null":
            if value is not None and not _is_num(value):
                raise ValueError(f"{where}.{key} must be a number or null")
            out[key] = value
        elif kind == "string_list":
            if (not isinstance(value, list)
                    or not all(isinstance(s, str) for s in value)):
                raise ValueError(f"{where}.{key} must be a list of strings")
            out[key] = list(value)
        elif kind == "boolean":
            if not isinstance(value, bool):
                raise ValueError(f"{where}.{key} must be a boolean")
            out[key] = value
    return out


def _validate_field(field_name: str, spec: dict, value):
    """Validate one config value against its schema entry.
    Returns the normalised value. Raises ValueError."""
    ftype = spec.get("type")
    if ftype == "string":
        if not isinstance(value, str):
            raise ValueError(f"{field_name} must be a string")
        if spec.get("max_length") and len(value) > spec["max_length"]:
            raise ValueError(
                f"{field_name} must be at most {spec['max_length']} characters"
            )
        return value
    if ftype == "url":
        return _check_url(value, field_name)
    if ftype == "integer":
        if not _is_int(value):
            raise ValueError(f"{field_name} must be an integer")
        if spec.get("min") is not None and value < spec["min"]:
            raise ValueError(f"{field_name} must be at least {spec['min']}")
        if spec.get("max") is not None and value > spec["max"]:
            raise ValueError(f"{field_name} must be at most {spec['max']}")
        return value
    if ftype == "number":
        if not _is_num(value):
            raise ValueError(f"{field_name} must be a number")
        if spec.get("min") is not None and value < spec["min"]:
            raise ValueError(f"{field_name} must be at least {spec['min']}")
        return value
    if ftype == "boolean":
        if not isinstance(value, bool):
            raise ValueError(f"{field_name} must be a boolean")
        return value
    if ftype == "string_list":
        if not isinstance(value, list) or not all(
            isinstance(s, str) for s in value
        ):
            raise ValueError(f"{field_name} must be a list of strings")
        choices = spec.get("choices")
        if choices:
            bad = [s for s in value if s not in choices]
            if bad:
                raise ValueError(
                    f"{field_name} must be chosen from"
                    f" {', '.join(choices)}; got {', '.join(bad)}"
                )
        return list(value)
    if ftype == "email_list":
        if not isinstance(value, list) or not all(
            isinstance(s, str) for s in value
        ):
            raise ValueError(f"{field_name} must be a list of email addresses")
        bad = [s for s in value if not _EMAIL_RE.match(s.strip())]
        if bad:
            raise ValueError(
                f"{field_name} has invalid email addresses:"
                f" {', '.join(bad)}"
            )
        return [s.strip() for s in value]
    if ftype == "role_list":
        if not isinstance(value, list):
            raise ValueError(f"{field_name} must be a list of roles")
        if len(value) > 200:
            raise ValueError(f"{field_name} must hold at most 200 roles")
        return [_check_role(r, f"{field_name}[{i}]")
                for i, r in enumerate(value)]
    if ftype == "condition":
        # The watch vocabulary (§3.7, §6 D9): changed / appeared /
        # disappeared / contains:<text> — NOT the broken
        # WatchesView list (bare "contains" is rejected by the
        # server).
        if not isinstance(value, str):
            raise ValueError(f"{field_name} must be a string")
        value = value.strip()
        if value.startswith("contains:"):
            if not value[len("contains:"):].strip():
                raise ValueError(f"{field_name} 'contains:' needs the text to look for")
            return value
        if value not in ("changed", "appeared", "disappeared"):
            raise ValueError(
                f"{field_name} must be changed, appeared, disappeared,"
                " or contains:<text>"
            )
        return value
    if ftype == "string_or_null":
        if value is not None and not isinstance(value, str):
            raise ValueError(f"{field_name} must be a string or null")
        return value
    if ftype == "integer_or_null":
        if value is not None and not _is_int(value):
            raise ValueError(f"{field_name} must be an integer or null")
        if value is not None and spec.get("min") is not None and value < spec["min"]:
            raise ValueError(f"{field_name} must be at least {spec['min']}")
        return value
    if ftype == "number_or_null":
        if value is not None and not _is_num(value):
            raise ValueError(f"{field_name} must be a number or null")
        if value is not None and spec.get("min") is not None and value < spec["min"]:
            raise ValueError(f"{field_name} must be at least {spec['min']}")
        return value
    raise ValueError(f"unknown schema type {ftype!r} for {field_name}")


def _base_bounds() -> dict:
    return {
        "max_actions_per_day": 5,
        "max_applies_per_day": 0,
        "max_sends_per_day": 0,
        "max_spend_inr": 0,
        "allowed_sites": [],
        "require_approval_for": ["apply", "send"],
        "auto_approve": False,
        "quiet_hours_ist": None,
        "min_score": 60,
    }


def _job_hunt_bounds() -> dict:
    bounds = _base_bounds()
    bounds["max_applies_per_day"] = 3
    return bounds


def _cold_mail_bounds() -> dict:
    bounds = _base_bounds()
    bounds["max_sends_per_day"] = 5
    return bounds


def _schema(fields: dict) -> dict:
    """One type's fields plus the common fields."""
    merged = dict(COMMON_FIELDS)
    merged.update(fields)
    return merged


MISSION_TYPES: dict[str, MissionTypeSpec] = {
    "job_hunt": MissionTypeSpec(
        type="job_hunt",
        label="Job hunt",
        config_schema=_schema({
            "sources": {
                "type": "string_list", "required": False,
                "default": ["gmail", "pasted"],
                "choices": ["gmail", "pasted"],
                "description": "Discovery sources: Gmail offer mail and/or roles pasted in config.",
            },
            "min_score": {
                "type": "integer", "required": False, "default": 60,
                "min": 0, "max": 100,
                "description": "Roles below this matcher score are never acted on.",
            },
            "stipend_min": {
                "type": "integer_or_null", "required": False,
                "default": None, "min": 0,
                "description": "Mission stipend floor (₹/month); overrides the profile floor when set.",
            },
            "locations": {
                "type": "string_list", "required": False, "default": [],
                "description": "Mission locations; override the profile locations when non-empty.",
            },
            "roles": {
                "type": "role_list", "required": False, "default": [],
                "description": "User-pasted roles to match (the reliable job source — Eclipse cannot search job sites).",
            },
            "watch_new_only": {
                "type": "boolean", "required": False, "default": True,
                "description": "Only act on roles not seen in an earlier run.",
            },
        }),
        default_bounds=_job_hunt_bounds(),
        discover=discover_job_hunt,
    ),
    "cold_mail": MissionTypeSpec(
        type="cold_mail",
        label="Cold mail",
        config_schema=_schema({
            "role_ids": {
                "type": "string_list", "required": False, "default": [],
                "description": "Role ids to mail about, resolved against job-hunt missions' pasted roles.",
            },
            "roles": {
                "type": "role_list", "required": False, "default": [],
                "description": "Roles to mail about, given directly (same shape as job-hunt roles).",
            },
            "recipients": {
                "type": "email_list", "required": False, "default": [],
                "description": "Explicit recipient addresses — the mission never invents addresses.",
            },
            "attach_resume": {
                "type": "boolean", "required": False, "default": False,
                "description": "Attach the stored resume (POST /missions/resume) to each mail.",
            },
            "max_drafts_per_run": {
                "type": "integer", "required": False, "default": 5,
                "min": 1, "max": 20,
                "description": "New drafts created per run at most.",
            },
        }),
        default_bounds=_cold_mail_bounds(),
        discover=discover_cold_mail,
    ),
    # PR #7: email monitoring — "watch my inbox for interview
    # mails". Read-only: discovery finds matching offer /
    # interview mail via the Gmail connector and records it
    # (activity + facts + briefing); the runner takes NO action
    # for this type (no task, no apply, no send).
    "email_monitor": MissionTypeSpec(
        type="email_monitor",
        label="Inbox watch",
        config_schema=_schema({
            "keywords": {
                "type": "string_list", "required": False,
                "default": ["interview", "offer", "shortlisted",
                            "selected"],
                "description": "Mail matches when its subject or body contains one of these words.",
            },
        }),
        default_bounds=_base_bounds(),
        discover=discover_email_monitor,
    ),
    "price_watch": MissionTypeSpec(
        type="price_watch",
        label="Price watch",
        config_schema=_schema({
            "url": {
                "type": "url", "required": True, "default": None,
                "description": "The product page to watch (must be open in a tab for checks to run).",
            },
            "selector": {
                "type": "string", "required": True, "default": None,
                "max_length": 500,
                "description": "CSS selector (or text:<phrase>) for the price element.",
            },
            "condition": {
                "type": "condition", "required": False, "default": "changed",
                "description": "changed, appeared, disappeared, or contains:<text>.",
            },
            "target_price": {
                "type": "number_or_null", "required": False,
                "default": None, "min": 0,
                "description": "Target price — hit it and the mission completes (when one_shot).",
            },
            "currency": {
                "type": "string", "required": False, "default": "INR",
                "max_length": 10,
            },
            "one_shot": {
                "type": "boolean", "required": False, "default": True,
                "description": "When the target is hit, the mission completes.",
            },
            "watch_id": {
                "type": "string_or_null", "required": False, "default": None,
                "description": "Set by the runner when it creates the watch; do not set by hand.",
            },
        }),
        default_bounds=_base_bounds(),
        discover=discover_price_watch,
    ),
}


def _spec(mission_type: str) -> MissionTypeSpec:
    try:
        return MISSION_TYPES[mission_type]
    except KeyError:
        raise ValueError(
            f"unknown mission type {mission_type!r} — use one of"
            f" {', '.join(MISSION_TYPES)}"
        ) from None


def validate_config(mission_type: str, config: dict) -> dict:
    """Validate a mission config against its type schema.
    Returns the normalised config (defaults filled). Raises
    ValueError on an unknown type, an unknown key, a missing
    required field, or a field that fails its schema."""
    return _validate_config(mission_type, config, require_required=True)


def validate_config_draft(mission_type: str, config: dict) -> dict:
    """Validate a config that may be incomplete (the intake
    draft path): required fields may be absent — they come back
    as None and the caller lists them in `missing_fields`.
    Unknown keys and malformed values are still rejected."""
    return _validate_config(mission_type, config, require_required=False)


def _validate_config(mission_type: str, config: dict,
                     require_required: bool) -> dict:
    spec = _spec(mission_type)
    if not isinstance(config, dict):
        raise ValueError("config must be an object")
    unknown = sorted(set(config) - set(spec.config_schema))
    if unknown:
        raise ValueError(
            f"unknown config fields for {mission_type}: {', '.join(unknown)}"
        )
    out: dict = {}
    for field_name, field_spec in spec.config_schema.items():
        if field_name in config and config[field_name] is not None:
            out[field_name] = _validate_field(
                field_name, field_spec, config[field_name]
            )
        elif field_spec.get("required") and require_required:
            raise ValueError(
                f"{field_name} is required for a {mission_type} mission"
            )
        else:
            out[field_name] = copy.deepcopy(field_spec.get("default"))
    return out


def validate_bounds(mission_type: str, bounds: dict) -> dict:
    """Validate mission bounds (§3.5). Returns the normalised
    bounds (type defaults filled). Raises ValueError on an
    unknown type, an unknown key, or a field that fails its
    schema. Q1 override: auto_approve=true is ACCEPTED here —
    the design doc's 422 gate is not implemented."""
    spec = _spec(mission_type)
    if not isinstance(bounds, dict):
        raise ValueError("bounds must be an object")
    unknown = sorted(set(bounds) - set(BOUNDS_FIELDS))
    if unknown:
        raise ValueError(f"unknown bounds fields: {', '.join(unknown)}")
    out = copy.deepcopy(spec.default_bounds)
    for field_name, kind in BOUNDS_FIELDS.items():
        if field_name not in bounds or bounds[field_name] is None:
            continue
        value = bounds[field_name]
        if kind == "int_ge_0":
            if not _is_int(value) or value < 0:
                raise ValueError(
                    f"{field_name} must be an integer >= 0"
                )
            out[field_name] = value
        elif kind == "int_0_100":
            if not _is_int(value) or not 0 <= value <= 100:
                raise ValueError(f"{field_name} must be an integer 0-100")
            out[field_name] = value
        elif kind == "boolean":
            if not isinstance(value, bool):
                raise ValueError(f"{field_name} must be a boolean")
            out[field_name] = value
        elif kind == "string_list":
            if (not isinstance(value, list)
                    or not all(isinstance(s, str) for s in value)):
                raise ValueError(f"{field_name} must be a list of strings")
            out[field_name] = list(value)
        elif kind == "approval_list":
            if (not isinstance(value, list)
                    or not all(isinstance(s, str) for s in value)):
                raise ValueError(f"{field_name} must be a list of strings")
            bad = [s for s in value if s not in APPROVAL_KINDS]
            if bad:
                raise ValueError(
                    f"{field_name} must be chosen from"
                    f" {', '.join(APPROVAL_KINDS)}; got {', '.join(bad)}"
                )
            out[field_name] = list(value)
        elif kind == "quiet_hours":
            if not isinstance(value, dict):
                raise ValueError(
                    f"{field_name} must be null or"
                    " {\"start\": \"HH:MM\", \"end\": \"HH:MM\"}"
                )
            if set(value) != {"start", "end"}:
                raise ValueError(
                    f"{field_name} must have exactly start and end"
                )
            out[field_name] = {
                "start": _check_time(value["start"], f"{field_name}.start"),
                "end": _check_time(value["end"], f"{field_name}.end"),
            }
    return out


def default_bounds(mission_type: str) -> dict:
    """The registry defaults for a mission type (§3.5). Raises
    ValueError for an unknown type."""
    return copy.deepcopy(_spec(mission_type).default_bounds)
