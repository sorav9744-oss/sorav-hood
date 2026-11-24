"""One click on the PC = Shorts for the next N days on both channels (YouTube + Facebook), scheduled.

  python -m joshiclip.pcbatch run --days 3        what the Upload button does
  python -m joshiclip.pcbatch job a 2026-11-25    one channel, one day (run inside that channel's checkout)

Why the PC: YouTube blocks GitHub's servers from downloading, your home internet isn't blocked. GitHub still
uploads anything YouTube's daily limit didn't allow (the queue, .github/workflows/queue.yml), so the days you
don't switch the PC on are covered too.

Each channel has its own checkout of its own repo under .channels/ (its own records), so the records always go to
the right repo and channel_b's plan always sees channel_a's plan for the same day (no scene twice).
"""
from __future__ import annotations

import datetime as dt
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT.parents[1] if ROOT.parent.name == ".channels" else ROOT   # the project folder (also from a checkout)
CH = MAIN / ".channels"
SIDES = {"a": ("channel_a", "prajmulik49-stack", "prajmulik49-stack/s-clips"),
         "b": ("channel_b", "sorav9744-oss", "sorav9744-oss/sorav-hood")}


def log(msg: str) -> None:
    print(f"{dt.datetime.now():%H:%M:%S} {msg}", flush=True)


def _git(args: list[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    ssl = ["-c", "http.sslBackend=schannel"] if os.name == "nt" else []
    r = subprocess.run(["git", *ssl, *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=600)
    if check and r.returncode:
        raise RuntimeError(f"git {args[0]}: {(r.stderr or r.stdout).strip()[-300:]}")
    return r


def sync(side: str) -> Path:
    """Fresh checkout of the channel's repo (its code + its records); local changes are kept on top."""
    _, user, repo = SIDES[side]
    d = CH / side
    url = f"https://{user}@github.com/{repo}.git"
    if not (d / ".git").exists():
        CH.mkdir(exist_ok=True)
        _git(["clone", "-q", url, str(d)])
    else:
        _git(["remote", "set-url", "origin", url], d)
        _git(["pull", "-q", "--rebase", "--autostash", "origin", "main"], d)
    shutil.copy2(MAIN / ".env", d / ".env")        # same keys as the project (YouTube, Facebook, MEDIA_TOKEN)
    return d


def push(side: str) -> None:
    d = CH / side
    _git(["add", "state"], d)
    if _git(["diff", "--cached", "--quiet"], d, check=False).returncode == 0:
        return
    _git(["commit", "-q", "-m", f"{SIDES[side][0]}: Shorts made on the PC ({dt.date.today()})"], d)
    for k in range(5):
        _git(["pull", "-q", "--rebase", "--autostash", "origin", "main"], d, check=False)
        if _git(["push", "-q", "origin", "HEAD:main"], d, check=False).returncode == 0:
            log(f"records of {SIDES[side][0]} saved to {SIDES[side][2]}")
            return
        time.sleep(10)
    log(f"WARNING could not push {SIDES[side][2]}; run the batch again later")


def keep_awake(on: bool) -> None:
    """Windows must not sleep while the batch runs."""
    if os.name != "nt":
        return
    import ctypes
    ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
    ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0))


def job(side: str, date: str) -> int:
    """Inside a channel checkout: plan this day (channel_b after channel_a, against its local plan) and post it."""
    os.environ.setdefault("JOSHICLIP_FETCH", "auto")
    channel, _, repo = SIDES[side]
    os.environ["GITHUB_REPOSITORY"] = repo
    from joshiclip import config, daily, ledger
    cfg = config.load()
    cfg["paths"]["work"] = str(MAIN / "work")      # one shared analysis cache for both channels
    if side == "b":
        def from_local_a(cfg_, log=print):
            dst = ledger.PARTNERS / "prajmulik49-stack__s-clips" / "state"
            shutil.rmtree(dst.parent, ignore_errors=True)
            shutil.copytree(CH / "a" / "state", dst, ignore=shutil.ignore_patterns("_partners"))
            return ["prajmulik49-stack/s-clips"]
        daily.sync_partners = from_local_a
    plan = daily.plan_day(cfg, False, print, date=date)
    n = len(plan["channels"].get(channel, []))
    print(f"[{channel}] {date}: {n} Shorts planned", flush=True)
    return daily.post(cfg, channel, plan, True, print, date=date)


IST = dt.timezone(dt.timedelta(minutes=330))


def inventory(side_dirs: dict | None = None, now: dt.datetime | None = None) -> dict[str, int]:
    """Shorts per channel that are made but not published yet (scheduled on YouTube or waiting in the queue), from
    each channel's own records. A Short stops counting the moment its publish time passes; a queued Short that
    GitHub later uploaded is one Short, not two (same vlog + start)."""
    import json
    now = now or dt.datetime.now(dt.timezone.utc)
    out: dict[str, int] = {}
    for side, (channel, _, _) in SIDES.items():
        base = (side_dirs or {}).get(side) or (CH / side)
        f = Path(base) / "state" / f"posted_{channel}.jsonl"
        seen: dict = {}
        if f.exists():
            for line in f.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                r = json.loads(line)
                t = r.get("publish_at")
                if t:
                    seen[(r["vid"], round(r["start"], 3))] = dt.datetime.strptime(t, "%Y-%m-%dT%H:%M:%SZ").replace(
                        tzinfo=dt.timezone.utc)
        out[channel] = sum(1 for w in seen.values() if w > now)
    return out


def first_day(now_ist: dt.datetime, cutoff_hour: int = 17) -> dt.date:
    """Today if its slots are still ahead, otherwise tomorrow (a late click must not squeeze 6 Shorts into the evening)."""
    return now_ist.date() if now_ist.hour < cutoff_hour else now_ist.date() + dt.timedelta(days=1)


def run(days: int = 4) -> int:
    """Fill the buffer: keep `days` days of Shorts scheduled ahead on both channels. Days whose Shorts exist are
    skipped in seconds; a half-finished day continues; only the missing days are analysed, rendered and uploaded."""
    keep_awake(True)
    try:
        start = first_day(dt.datetime.now(IST))
        dates = [(start + dt.timedelta(days=k)).isoformat() for k in range(max(1, min(days, 10)))]
        log(f"===== batch: buffer of {len(dates)} day(s) {dates[0]} .. {dates[-1]}, both channels")
        for s in SIDES:
            sync(s)
            log(f"{SIDES[s][2]}: up to date")
        rc = 0
        for date in dates:
            for s in SIDES:
                log(f"----- {SIDES[s][0]} {date}")
                p = subprocess.run([sys.executable, "-m", "joshiclip.pcbatch", "job", s, date], cwd=CH / s,
                                   env=dict(os.environ, PYTHONPATH=str(CH / s), PYTHONUTF8="1", PYTHONUNBUFFERED="1"))
                rc |= p.returncode
                push(s)                            # after every day: the records are safe even if the PC goes off
        inv = inventory()
        log(f"===== batch done (rc={rc}); scheduled ahead: " + ", ".join(f"{c} {n}" for c, n in inv.items()))
        return rc
    finally:
        keep_awake(False)


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[:1] == ["run"]:
        n = int(a[a.index("--days") + 1]) if "--days" in a else 4
        raise SystemExit(run(n))
    if a[:1] == ["job"] and len(a) == 3:
        raise SystemExit(job(a[1], a[2]))
    print(__doc__)
