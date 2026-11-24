"""The phone's nightly job (Android + Termux, home Wi-Fi): download what the GitHub runners can't, start their runs.

  python -m joshiclip.phone run      once: today's whole handover (safe to repeat - finished steps are skipped)
  python -m joshiclip.phone loop     forever: run every night at daily.media.phone_time (IST), retry on errors
  python -m joshiclip.phone check    test the token, the media repo and yt-dlp (no downloads)

Needs (phone .env, never committed): MEDIA_TOKEN (GitHub classic token: repo + workflow), YOUTUBE_API_KEY.
Light on purpose: only requests, PyYAML and yt-dlp (+ node for YouTube's JS challenge, ffmpeg to cut clips).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import mediastore as ms
from .config import Config

API = "https://api.github.com"
LOG = Path.home() / "joshiclip-phone.log"


def log(msg: str) -> None:
    line = f"{dt.datetime.now().strftime('%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _ist_now(cfg) -> dt.datetime:
    off = int(cfg["daily"].get("utc_offset_min", 330))
    return dt.datetime.now(dt.timezone(dt.timedelta(minutes=off)))


def _ytdlp(args: list[str], tries: int = 3) -> None:
    base = [sys.executable, "-m", "yt_dlp", "--no-warnings", "--retries", "10", "--fragment-retries", "10",
            "--socket-timeout", "30"]
    if shutil.which("node"):
        base += ["--js-runtimes", "node"]          # YouTube's JS challenge (Termux: pkg install nodejs)
    last = ""
    for k in range(tries):
        r = subprocess.run(base + args, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.returncode == 0:
            return
        last = (r.stderr or r.stdout)[-400:]
        time.sleep(30 * (k + 1))
    raise RuntimeError("yt-dlp: " + last)


def _gh(method: str, url: str, **kw):
    return ms._req(method, url, headers=ms._h(), **kw)


# ---------------------------------------------------------------- which vlogs
def _raw_state(repo: str, path: str):
    r = _gh("GET", f"{API}/repos/{repo}/contents/state/{path}",)
    if r.status_code != 200:
        return None
    import base64
    return base64.b64decode(r.json()["content"]).decode("utf-8")


def pick_vlogs(cfg: Config, date: str) -> list[dict]:
    """Newest vlog + `old_vlogs` older ones not exhausted in any repo (same for every channel: their plans pick
    different scenes). Same date -> same choice."""
    from . import ytapi
    d, m = cfg["daily"], cfg["daily"].get("media") or {}
    cid = cfg["channel"]["official_id"]
    items = ytapi.playlist(f"UULF{cid[2:]}", int(d.get("vlog_pool", 200))) or []
    if not items:
        raise RuntimeError("YouTube API returned no vlogs (YOUTUBE_API_KEY?)")
    exhausted = set()
    for c in d["channels"]:
        txt = _raw_state(c["repo"], "exhausted.json") if c.get("repo") else None
        if txt:
            exhausted |= set(json.loads(txt))
    old = [v for v in items[1:int(m.get("old_pool", 80))] if v["vid"] not in exhausted]
    rng = random.Random(int(hashlib.sha256(("old" + date).encode()).hexdigest()[:12], 16))
    rng.shuffle(old)
    picked = [items[0]] + old[:int(m.get("old_vlogs", 4))]
    return [{"vid": v["vid"], "title": v["title"]} for v in picked]


# ---------------------------------------------------------------- downloads
def ensure_360(cfg: Config, v: dict, have: dict) -> None:
    vid = v["vid"]
    if f"v360_{ms.safe(vid)}.mkv" in have:
        return
    f = cfg["fast"]
    tmp = Path(tempfile.mkdtemp(prefix="jc360_"))
    try:
        log(f"  360p {vid} {v['title'][:40]}")
        _ytdlp(["-f", f["proxy_format"], "--merge-output-format", "mkv", "-o", str(tmp / "src360.%(ext)s"),
                "--write-auto-subs", "--sub-langs", f["sub_langs"], "--sub-format", "json3", "--write-info-json",
                f"https://www.youtube.com/watch?v={vid}"])
        mkv = next(tmp.glob("src360.mkv"), None) or next(iter(sorted(tmp.glob("src360.*[!n]"))), None)
        if not mkv:
            raise RuntimeError("no video file")
        subs = sorted(tmp.glob("src360.*.json3"), key=lambda p: ("orig" not in p.name, p.name))
        info = tmp / "src360.info.json"
        for p, name in ((subs[0] if subs else None, f"v360_{vid}.json3"), (info if info.exists() else None,
                                                                          f"v360_{vid}.info.json")):
            if p:
                ms.upload(cfg, p, name, have)
        ms.upload(cfg, mkv, f"v360_{vid}.mkv", have)      # last: its presence means "complete"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def ensure_clip(cfg: Config, vid: str, start: float, end: float, have: dict) -> None:
    pad = float(cfg["fast"].get("clip_pad_s", 3))
    a, b = max(0.0, start - pad), end + pad
    name = ms.clip_name(vid, a, b)
    if name in have:
        return
    tmp = Path(tempfile.mkdtemp(prefix="jcclip_"))
    try:
        log(f"  1080p {vid} {a:.1f}-{b:.1f}s")
        _ytdlp(["-f", cfg["fast"]["clip_format"], "--download-sections", f"*{a:.3f}-{b:.3f}",
                "--merge-output-format", "mkv", "-o", str(tmp / "clip.%(ext)s"),
                f"https://www.youtube.com/watch?v={vid}"])
        out = next(tmp.glob("clip.mkv"), None) or next(iter(tmp.glob("clip.*")), None)
        if not out:
            raise RuntimeError("no clip file")
        ms.upload(cfg, out, name, have)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- GitHub runs
def start_run(repo: str) -> None:
    r = _gh("POST", f"{API}/repos/{repo}/actions/workflows/daily.yml/dispatches",
            json={"ref": "main", "inputs": {"dry_run": "false", "test_one": "false"}})
    log(f"  started {repo}'s daily run (HTTP {r.status_code})")


def wait_plan(repo: str, date: str, minutes: float) -> dict | None:
    end = time.time() + 60 * minutes
    while time.time() < end:
        txt = _raw_state(repo, f"plans/{date}.json")
        if txt:
            return json.loads(txt)
        time.sleep(60)
    return None


def run(cfg: Config) -> int:
    m = cfg["daily"].get("media") or {}
    if not m.get("phone_enabled", True):
        log("phone job is switched off (config daily.media.phone_enabled); nothing to do")
        return 0
    date = _ist_now(cfg).date().isoformat()
    log(f"===== phone run {date}")
    try:
        ms.delete_older(cfg, log=log)
    except Exception as e:  # noqa: BLE001
        log(f"  cleanup skipped: {e}")
    have = ms.assets(cfg)
    man_name = f"manifest_{date}.json"
    man = ms.manifest(cfg, date)
    if not man:
        man = {"date": date, "vlogs": pick_vlogs(cfg, date)}
    ok = []
    for v in man["vlogs"]:
        try:
            ensure_360(cfg, v, have)
            ok.append(v)
        except Exception as e:  # noqa: BLE001
            log(f"  360p {v['vid']} FAILED: {str(e)[:200]}")
    if not ok:
        log("  no vlog could be downloaded; stopping")
        return 1
    if ok[0]["vid"] != man["vlogs"][0]["vid"]:
        log("  WARNING the newest vlog failed; today has no 'latest' Shorts")
    man["vlogs"] = ok
    if man_name not in have:
        p = Path(tempfile.mkdtemp()) / man_name
        p.write_text(json.dumps(man, ensure_ascii=False, indent=1), encoding="utf-8")
        ms.upload(cfg, p, man_name, have)
    rc = 0
    for c in cfg["daily"]["channels"]:
        repo, ch = c.get("repo"), c["name"]
        if not repo:
            continue
        plan = json.loads(_raw_state(repo, f"plans/{date}.json") or "null")
        if not plan:
            start_run(repo)
            plan = wait_plan(repo, date, float(m.get("plan_wait_min", 120)))
        if not plan:
            log(f"  {ch}: no plan from GitHub in time; its clips can't be prepared")
            rc = 1
            continue
        items = plan["channels"].get(ch, [])
        log(f"  {ch}: plan with {len(items)} Shorts -> 1080p clips")
        have = ms.assets(cfg)
        for it in items:
            try:
                ensure_clip(cfg, it["vid"], it["start"], it["end"], have)
            except Exception as e:  # noqa: BLE001
                log(f"  clip {it['vid']} {it['start']:.0f}s FAILED: {str(e)[:200]}")
                rc = 1
    log(f"===== phone run done (rc={rc})")
    return rc


def check(cfg: Config) -> int:
    ok = True
    try:
        rel = ms.release(cfg)
        log(f"OK media repo {cfg['daily']['media']['repo']} (release '{rel['tag_name']}', {len(ms.assets(cfg))} files)")
    except Exception as e:  # noqa: BLE001
        log(f"FAIL media repo: {e}"); ok = False
    for c in cfg["daily"]["channels"]:
        try:
            code = _gh("GET", f"{API}/repos/{c['repo']}/actions/workflows/daily.yml").status_code
        except Exception as e:  # noqa: BLE001
            code = str(e)[:80]
        log(f"{'OK' if code == 200 else 'FAIL'} can start {c['repo']}'s runs ({code})")
        ok &= code == 200
    if not ok:
        log("HINT: 401 / Bad credentials = MEDIA_TOKEN wrong or empty. Fix: rm ~/s-clips/.env && bash ~/setup.sh")
    try:
        _ytdlp(["--simulate", "-f", cfg["fast"]["clip_format"], "https://www.youtube.com/watch?v=PPWTwGU-cqQ"], tries=1)
        log("OK yt-dlp can read YouTube from here")
    except Exception as e:  # noqa: BLE001
        log(f"FAIL yt-dlp: {str(e)[:200]}"); ok = False
    log(f"OK node: {shutil.which('node') or 'MISSING (pkg install nodejs)'}")
    return 0 if ok else 1


def loop(cfg_path: str | None = None) -> None:
    """Every night at daily.media.phone_time (IST). Pulls the newest code first, so fixes reach the phone too."""
    from .config import load
    root = Path(__file__).resolve().parents[1]
    while True:
        cfg = load(cfg_path)
        hh, mm = map(int, str((cfg["daily"].get("media") or {}).get("phone_time", "00:30")).split(":"))
        now = _ist_now(cfg)
        nxt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if nxt <= now:
            nxt += dt.timedelta(days=1)
        log(f"next run {nxt:%Y-%m-%d %H:%M} IST")
        time.sleep(max(30, (nxt - now).total_seconds()))
        subprocess.run(["git", "-C", str(root), "pull", "-q", "--ff-only"], capture_output=True)
        for attempt in range(3):              # a failed handover is retried 3 times, 20 min apart
            try:
                if subprocess.run([sys.executable, "-m", "joshiclip.phone", "run"], cwd=root).returncode == 0:
                    break
            except Exception as e:  # noqa: BLE001
                log(f"run crashed: {e}")
            time.sleep(1200)


if __name__ == "__main__":
    from .config import load
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "loop":
        loop()
    elif cmd in ("run", "check"):
        c = load()
        raise SystemExit(run(c) if cmd == "run" else check(c))
    else:
        print(__doc__)
