"""state/: what was planned and posted, by every channel, forever. Committed to the repo by the daily workflow.

state/posted_<channel>.jsonl   one line per uploaded Short (per-channel files -> two parallel jobs never conflict)
state/plans/<date>.json        the day's plan; its scenes stay RESERVED for `reserve_days` even if a post failed
state/exhausted.json           vlogs with no unused good moment left (skipped by later plans)

A scene is "used" if it overlaps ANY posted or reserved span of the same vlog, in any channel, by even 1 frame.
"""
from __future__ import annotations

import datetime as dt
import json
import subprocess
from pathlib import Path

from .config import ROOT

STATE = ROOT / "state"
PARTNERS = STATE / "_partners"     # other repos' state/ (another channel run from its own repo), read-only, git-ignored


def _state_dirs() -> list[Path]:
    """Own state/ + every partner repo's state/: a scene used by ANY channel in any repo is used."""
    return [STATE] + (sorted(p / "state" for p in PARTNERS.iterdir() if (p / "state").is_dir()) if PARTNERS.is_dir() else [])


def _read_jsonl(p: Path) -> list[dict]:
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


def posted() -> list[dict]:
    return [r for d in _state_dirs() for p in sorted(d.glob("posted_*.jsonl")) for r in _read_jsonl(p)]


def plan_path(date: str) -> Path:
    return STATE / "plans" / f"{date}.json"


def recent_plans(today: str, reserve_days: int) -> list[dict]:
    t = dt.date.fromisoformat(today)
    out = []
    for p in [q for d in _state_dirs() for q in sorted((d / "plans").glob("*.json"))]:
        try:
            d = dt.date.fromisoformat(p.stem)
        except ValueError:
            continue
        if (t - d).days <= reserve_days:                 # recent days and every FUTURE day (planned ahead)
            out.append(json.loads(p.read_text(encoding="utf-8")))
    return out


def used_spans(today: str, reserve_days: int = 2) -> dict[str, list[tuple[float, float]]]:
    u: dict[str, list[tuple[float, float]]] = {}
    for r in posted():
        u.setdefault(r["vid"], []).append((r["start"], r["end"]))
    for pl in recent_plans(today, reserve_days):
        for ch in pl.get("channels", {}).values():
            for it in ch:
                u.setdefault(it["vid"], []).append((it["start"], it["end"]))
    return u


def partner_spans(today: str, reserve_days: int = 2) -> dict[str, list[tuple[float, float]]]:
    """Only the OTHER repos' posted + recently planned scenes (state/_partners)."""
    u: dict[str, list[tuple[float, float]]] = {}
    for d in _state_dirs()[1:]:
        for p in sorted(d.glob("posted_*.jsonl")):
            for r in _read_jsonl(p):
                u.setdefault(r["vid"], []).append((r["start"], r["end"]))
        t = dt.date.fromisoformat(today)
        for p in sorted((d / "plans").glob("*.json")):
            try:
                age = (t - dt.date.fromisoformat(p.stem)).days
            except ValueError:
                continue
            if age <= reserve_days:                          # incl. days planned ahead
                for ch in json.loads(p.read_text(encoding="utf-8")).get("channels", {}).values():
                    for it in ch:
                        u.setdefault(it["vid"], []).append((it["start"], it["end"]))
    return u


def partner_plan_exists(date: str) -> bool:
    return any((d / "plans" / f"{date}.json").exists() for d in _state_dirs()[1:])


def overlaps(spans: dict, vid: str, a: float, b: float, pad: float = 0.0) -> bool:
    return any(min(b, e) - max(a, s) > -pad for s, e in spans.get(vid, []))


def used_titles() -> set[str]:
    return {r["title"] for r in posted() if r.get("title")}


def record_post(channel: str, rec: dict) -> Path:
    STATE.mkdir(exist_ok=True)
    p = STATE / f"posted_{channel}.jsonl"
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
    return p


def exhausted() -> dict:
    p = STATE / "exhausted.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def mark_exhausted(vid: str, date: str) -> Path:
    e = exhausted()
    e[vid] = date
    p = STATE / "exhausted.json"
    STATE.mkdir(exist_ok=True)
    p.write_text(json.dumps(e, indent=1, sort_keys=True), encoding="utf-8")
    return p


def save_plan(plan: dict) -> Path:
    p = plan_path(plan["date"])
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(plan, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    return p


# ---------------------------------------------------------------- git (only when enabled: CI or daily.git_push)
def git_commit(paths: list[Path], msg: str, push: bool, log=print) -> None:
    if not push:
        return
    rel = [str(Path(p).resolve().relative_to(ROOT)) for p in paths]

    def g(*a, check=True):
        return subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True, check=check)
    g("add", "--", *rel)
    if g("diff", "--cached", "--quiet", check=False).returncode == 0:
        return
    g("commit", "-m", msg)
    for k in range(6):
        if g("push", check=False).returncode == 0:
            return
        g("pull", "--rebase", "--autostash", check=False)
    log("    WARNING: could not push state/ (it will be pushed by the workflow's final step)")
