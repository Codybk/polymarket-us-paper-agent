"""
48-hour evaluation window, persisted in state/evaluation.json.

WHY THIS FILE LIVES UNDER state/
--------------------------------
Every GitHub Actions run is a CLEAN CHECKOUT of the repository. Anything a run
writes is discarded unless it is committed and pushed. The workflow commits
`state/` and `docs/`, so the window's start timestamp must live in `state/`.

An earlier version stamped the start into config/risk_config.json, which the
workflow does not commit. The result was silent and total: every scheduled run
began with an empty start time, re-stamped "now", and the window could never
expire -- the scanner would have run forever. The split below is the structural
fix, not a patch:

    config/risk_config.json   IMMUTABLE deployment configuration.
                              Written by a human, committed by a human,
                              never modified by running code.

    state/evaluation.json     MUTABLE run state. Written by the scanner,
                              committed by the workflow, survives checkouts.

`assert_config_immutable()` enforces the split so the mistake cannot recur.
"""
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Tuple

# Keys that must NEVER appear in config/risk_config.json, because a run that
# writes them would lose them on the next clean checkout.
FORBIDDEN_CONFIG_KEYS = ("evaluation_start_utc", "evaluation_complete",
                         "evaluation_completed_at")


def _parse(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def assert_config_immutable(cfg: Dict) -> None:
    """Fail loudly if mutable state has crept back into deployment config."""
    bad = [k for k in FORBIDDEN_CONFIG_KEYS if k in cfg]
    if bad:
        raise RuntimeError(
            f"config/risk_config.json contains mutable evaluation state {bad}. "
            "These belong in state/evaluation.json -- config is not committed "
            "by the workflow, so anything written there is lost on the next "
            "clean checkout.")


def _default(hours: float) -> Dict:
    return {"started_at": None, "complete": False, "completed_at": None,
            "hours": hours, "runs_observed": 0}


def load(state_path: str, cfg: Dict) -> Dict:
    hours = float(cfg.get("evaluation_hours", 48))
    if not os.path.exists(state_path):
        return _default(hours)
    try:
        with open(state_path) as fh:
            st = json.load(fh)
    except (json.JSONDecodeError, OSError):
        # A corrupt state file must not silently restart the window.
        raise RuntimeError(
            f"{state_path} exists but could not be read. Refusing to run rather "
            "than restart the evaluation window from zero.")
    st.setdefault("started_at", None)
    st.setdefault("complete", False)
    st.setdefault("completed_at", None)
    st.setdefault("runs_observed", 0)
    # Duration is deployment config, not state -- config stays authoritative.
    st["hours"] = hours
    return st


def save(state_path: str, st: Dict) -> None:
    os.makedirs(os.path.dirname(state_path) or ".", exist_ok=True)
    tmp = state_path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(st, fh, indent=2)
    os.replace(tmp, state_path)          # atomic; never a half-written window


def stamp_start_if_needed(state_path: str, cfg: Dict) -> Tuple[Dict, bool]:
    """Stamp the start exactly once, ever. Returns (state, was_stamped)."""
    st = load(state_path, cfg)
    st["runs_observed"] = int(st.get("runs_observed", 0)) + 1
    if st.get("started_at"):
        save(state_path, st)
        return st, False
    st["started_at"] = datetime.now(timezone.utc).isoformat()
    save(state_path, st)
    return st, True


def window(st: Dict) -> Dict:
    start = _parse(st.get("started_at"))
    hours = float(st.get("hours", 48))
    if start is None:
        return {"started": False, "expired": False, "elapsed_hours": 0.0,
                "remaining_hours": hours, "start": None, "end": None,
                "complete": bool(st.get("complete")),
                "runs_observed": st.get("runs_observed", 0)}
    end = start + timedelta(hours=hours)
    now = datetime.now(timezone.utc)
    elapsed = (now - start).total_seconds() / 3600.0
    return {"started": True, "expired": now >= end,
            "elapsed_hours": round(elapsed, 4),
            "remaining_hours": round(max(0.0, hours - elapsed), 4),
            "start": start.isoformat(), "end": end.isoformat(),
            "complete": bool(st.get("complete")),
            "runs_observed": st.get("runs_observed", 0)}


def mark_complete(state_path: str, cfg: Dict) -> Dict:
    st = load(state_path, cfg)
    st["complete"] = True
    st["completed_at"] = datetime.now(timezone.utc).isoformat()
    save(state_path, st)
    return st


def is_complete(state_path: str, cfg: Dict) -> bool:
    return bool(load(state_path, cfg).get("complete"))
