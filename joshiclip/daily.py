"""Daily posting: N Shorts per channel per day, 2-3 from the newest vlog, the rest from older vlogs, and no scene is
ever posted twice (any channel, any day).

  joshiclip daily plan                    pick today's scenes for every channel -> state/plans/<date>.json
  joshiclip daily post --channel NAME     render that channel's scenes from the plan, upload, record in state/
  joshiclip daily run                     plan + post every channel (one machine)
  add --dry-run to `plan` / `run` to only print the plan; --no-upload to render without uploading
"""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import os
import random
import time
from pathlib import Path

from . import ledger
from .config import Config, ROOT


def _today(d: dict) -> dt.datetime:
    return dt.datetime.now(dt.timezone(dt.timedelta(minutes=int(d.get("utc_offset_min", 330)))))


def _push(cfg: Config) -> bool:
    return bool(cfg["daily"].get("git_push")) or os.environ.get("GITHUB_ACTIONS") == "true"


def my_channels(d: dict) -> list[str]:
    """Channels this run handles. On GitHub: those whose `repo` is this repository (one channel per repo) and,
    on a scheduled run, whose `crons` include the cron that fired. Locally: all."""
    repo = os.environ.get("GITHUB_REPOSITORY")
    sched = (os.environ.get("JOSHICLIP_SCHEDULE") or "").strip()
    out = []
    for c in d["channels"]:
        if repo and c.get("repo") and c["repo"].lower() != repo.lower():
            continue
        if sched and c.get("crons") and sched not in c["crons"]:
            continue
        out.append(c["name"])
    return out


def _fetch_partner_raw(cfg: Config, repo: str, chans: list[str], dst: Path) -> int:
    """The few small files that matter, straight from raw.githubusercontent.com (no git, no API rate limit):
    state/posted_<channel>.jsonl + state/plans/<date>.json for the last reserve_days days. 404 = not there yet."""
    import requests
    d = cfg["daily"]
    today = _today(d).date()
    names = [f"posted_{ch}.jsonl" for ch in chans]
    names += [f"plans/{(today - dt.timedelta(days=k)).isoformat()}.json" for k in range(int(d.get("reserve_days", 2)) + 1)]
    n = 0
    for name in names:
        url = f"https://raw.githubusercontent.com/{repo}/main/state/{name}"
        for attempt in range(3):
            try:
                r = requests.get(url, timeout=30, headers={"Cache-Control": "no-cache"})
            except requests.RequestException:
                if attempt == 2:
                    raise
                time.sleep(5 * (attempt + 1))
                continue
            if r.status_code == 404:
                break
            r.raise_for_status()
            f = dst / "state" / name
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(r.content)
            n += 1
            break
    return n


def sync_partners(cfg: Config, log=print) -> list[str]:
    """Fetch the OTHER channels' repos' state (posted + recent plans) into state/_partners/ so this repo never
    picks a scene another channel posted or planned. raw files first, a shallow git clone as the fallback.
    A repo that can't be reached is skipped with a warning (post() re-checks before uploading)."""
    import shutil
    import subprocess
    repo = (os.environ.get("GITHUB_REPOSITORY") or "").lower()
    allc = cfg["daily"]["channels"]
    mine = {c["name"] for c in allc if (c.get("repo") or "").lower() == repo} if repo else {c["name"] for c in allc}
    repos = sorted({c["repo"] for c in allc if c.get("repo") and c["name"] not in mine})
    got = []
    for r in repos:
        dst = ledger.PARTNERS / r.replace("/", "__")
        shutil.rmtree(dst, ignore_errors=True)
        dst.mkdir(parents=True, exist_ok=True)
        try:
            n = _fetch_partner_raw(cfg, r, [c["name"] for c in allc if c.get("repo") == r], dst)
            log(f"  partner {r}: state fetched ({n} files)")
            got.append(r)
            continue
        except Exception as e:  # noqa: BLE001
            log(f"  partner {r}: raw fetch failed ({type(e).__name__}: {str(e)[:200]}); trying git")
        shutil.rmtree(dst, ignore_errors=True)
        try:
            for args in (["clone", "--depth", "1", "--filter=blob:none", "--no-checkout", "-q",
                          f"https://github.com/{r}.git", str(dst)],
                         ["-C", str(dst), "sparse-checkout", "set", "--no-cone", "/state/"],
                         ["-C", str(dst), "checkout", "-q"]):
                ssl = ["-c", "http.sslBackend=schannel"] if os.name == "nt" else []   # Windows certificate store
                p = subprocess.run(["git", *ssl, *args], capture_output=True, text=True, timeout=180)
                if p.returncode:
                    raise RuntimeError(f"git {args[0]}: {(p.stderr or p.stdout).strip()[-300:]}")
            log(f"  partner {r}: state fetched with git")
            got.append(r)
        except Exception as e:  # noqa: BLE001
            log(f"  WARNING partner {r}: could not fetch its state ({str(e)[:400]}); its scenes are not excluded today")
    return got


def _yields(d: dict, channel: str) -> bool:
    return bool(next((c for c in d["channels"] if c["name"] == channel), {}).get("after"))


def _wait_for_partner_plan(cfg: Config, chans: list[str], date: str, log=print) -> None:
    """A channel with `after:` plans only once the earlier repo's plan of the day is visible (GitHub often starts
    scheduled runs 30-60 min late, so the earlier repo may still be planning). Gives up after
    daily.wait_partner_plan_min and plans anyway (post() still drops any scene the partner took)."""
    d = cfg["daily"]
    if not os.environ.get("GITHUB_ACTIONS") or not any(_yields(d, ch) for ch in chans):
        return
    limit = time.time() + 60 * float(d.get("wait_partner_plan_min", 40))
    while not ledger.partner_plan_exists(date):
        if time.time() > limit:
            log("  WARNING: the earlier channel's plan of today isn't there yet; planning anyway")
            return
        log("  waiting for the earlier channel's plan of today (5 min)")
        time.sleep(300)
        sync_partners(cfg, log)


def prepare_cfg(cfg: Config) -> Config:
    """Settings for daily runs: more moments per vlog, CI-only overrides on GitHub-hosted runners."""
    c = copy.deepcopy(cfg)
    d = c["daily"]
    c["engine"]["max_shorts"] = int(d.get("max_moments_per_vlog", 20))
    c["engine"]["min_shorts"] = 1
    c["download"]["update_first"] = True
    if os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted":
        for sec, vals in (d.get("ci_overrides") or {}).items():
            c[sec] = dict(c.get(sec, {}), **vals)
    return c


def list_vlogs(cfg: Config, n: int) -> list[dict]:
    """Newest first: the official channel's long videos (UULF playlist = no Shorts, no lives)."""
    import subprocess
    from . import ingest, tools
    cid = cfg["channel"]["official_id"]
    from . import ytapi
    items = ytapi.playlist(f"UULF{cid[2:]}", n)      # API first (1 unit / 50 vlogs); yt-dlp only as a fallback
    if items:
        return [{"vid": x["vid"], "title": x["title"]} for x in items]
    r = subprocess.run(ingest.ytdlp_base(cfg) + ["--flat-playlist", "--playlist-end", str(n), "--print", "%(id)s\t%(title)s",
                                                 f"https://www.youtube.com/playlist?list=UULF{cid[2:]}"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", env=tools.child_env())
    if r.returncode:
        raise RuntimeError("listing vlogs failed: " + (r.stderr or "")[-600:])
    out = []
    for line in r.stdout.splitlines():
        if "\t" in line:
            vid, title = line.split("\t", 1)
            out.append({"vid": vid.strip(), "title": title.strip()})
    return out


def moments(cfg: Config, vid: str, used: dict, log=print) -> list[dict]:
    """This vlog's planned moments (best first) that overlap nothing already used."""
    from . import pipeline
    A = pipeline.analyze(cfg, vid, False, vid, log, fast=True)
    try:
        os.utime(Path(A["workdir"]) / "norm.json")       # "used today" for prune_cache
    except OSError:
        pass
    cards, sel = pipeline.plan(cfg, A, cfg["engine"]["max_shorts"])
    out = []
    for i in sel["winners"]:
        c = cards[i]
        if not ledger.overlaps(used, vid, c["start"], c["end"]):
            out.append({"vid": vid, "cand": c["id"], "start": c["start"], "end": c["end"], "final": c["final"],
                        "title": c["title"], "title_source": c.get("title_source"), "viewer_score": c.get("viewer_score")})
    out.sort(key=lambda m: (-m["final"], m["start"]))
    return out


def assign(ms: list[dict], need: dict[str, int], cap: int | None = None) -> dict[str, list[dict]]:
    """Deal moments best-first round-robin to the channels that still need some (fair quality split)."""
    got = {ch: [] for ch in need}
    order = [ch for ch in need]
    k = 0
    for m in ms:
        if cap is not None and sum(len(v) for v in got.values()) >= cap:
            break
        open_ = [ch for ch in order if len(got[ch]) < need[ch]]
        if not open_:
            break
        ch = open_[k % len(open_)]
        got[ch].append(m)
        k += 1
    return got


def plan_day(cfg: Config, dry_run: bool = False, log=print, replan: bool = False) -> dict:
    cfg = prepare_cfg(cfg)
    d = cfg["daily"]
    now = _today(d)
    date = now.date().isoformat()
    if ledger.plan_path(date).exists() and not (replan or dry_run):     # a re-run continues today's plan
        log(f"[daily plan {date}] reusing {ledger.plan_path(date)}")
        return json.loads(ledger.plan_path(date).read_text(encoding="utf-8"))
    rng = random.Random(int(hashlib.sha256(date.encode()).hexdigest()[:12], 16))
    chans = my_channels(d)
    if not chans:
        raise RuntimeError("no channel of config daily.channels belongs to this repo / schedule")
    sync_partners(cfg, log)
    if not dry_run:
        _wait_for_partner_plan(cfg, chans, date, log)
    lo, hi = d.get("latest_per_channel", [2, 3])
    want_latest = {ch: rng.randint(int(lo), int(hi)) for ch in chans}
    per = int(d.get("per_channel", 10))
    used = ledger.used_spans(date, int(d.get("reserve_days", 2)))
    titles = sorted(ledger.used_titles())
    cfg["_used_titles"] = titles
    exh = ledger.exhausted()
    from . import mediastore
    man = None
    if mediastore.enabled(cfg):          # the phone downloaded today's vlogs: plan exactly those (latest first)
        try:
            man = mediastore.manifest(cfg, date)
        except Exception as e:  # noqa: BLE001
            log(f"  media store unavailable ({str(e)[:120]}); planning from the vlog list")
    vlogs = man["vlogs"] if man else list_vlogs(cfg, int(d.get("vlog_pool", 200)))
    if man:
        log(f"  vlogs from the phone's manifest: {', '.join(v['vid'] for v in vlogs)}")
    if not vlogs:
        raise RuntimeError("could not list the channel's vlogs")
    log(f"[daily plan {date}] {len(vlogs)} vlogs in the pool; latest {vlogs[0]['vid']} {vlogs[0]['title']}")
    plan = {"date": date, "channels": {ch: [] for ch in chans}, "used_titles": titles,
            "max_moments_per_vlog": cfg["engine"]["max_shorts"], "vlog_titles": {}}
    deadline = time.time() + 60 * float(d.get("plan_max_minutes", 150))
    touched = []

    def take(v, need, cap=None, label=""):
        ms = moments(cfg, v["vid"], used, log)
        if not ms:
            if v is not vlogs[0]:
                touched.append(ledger.mark_exhausted(v["vid"], date))
            log(f"  {v['vid']}: no unused moments left")
            return
        got = assign(ms, need, cap)
        for ch, items in got.items():
            for it in items:
                it["latest"] = v is vlogs[0]
                plan["channels"][ch].append(it)
                used.setdefault(it["vid"], []).append((it["start"], it["end"]))
        plan["vlog_titles"][v["vid"]] = v["title"]
        log(f"  {v['vid']} {label}: " + ", ".join(f"{ch} +{len(x)}" for ch, x in got.items()))

    take(vlogs[0], want_latest, label="(latest)")
    # max_per_old_vlog counts all channels; a repo that plans only its own channel takes its share of it
    cap = max(1, round(int(d.get("max_per_old_vlog", 4)) * len(chans) / max(1, len(d["channels"]))))
    for v in ([v for v in vlogs[1:] if v["vid"] not in exh] if man else old_vlog_order(cfg, vlogs[1:], exh, rng)):
        need = {ch: per - len(plan["channels"][ch]) for ch in chans}
        if all(n <= 0 for n in need.values()):
            break
        if v["vid"] in exh:
            continue
        if time.time() > deadline:
            log("  plan time budget used up; posting what is planned")
            break
        try:
            take(v, {ch: n for ch, n in need.items() if n > 0}, cap)
        except Exception as e:  # noqa: BLE001 - one broken vlog (private, no audio, ...) must not stop the day
            log(f"  {v['vid']}: skipped ({type(e).__name__}: {str(e)[:160]})")
    for ch in chans:
        log(f"  {ch}: {len(plan['channels'][ch])} Shorts "
            f"({sum(1 for x in plan['channels'][ch] if x['latest'])} from the latest vlog)")
    if not dry_run:
        touched.append(ledger.save_plan(plan))
        ledger.git_commit(touched, f"daily plan {date}", _push(cfg), log)
        prune_cache(cfg, {it["vid"] for items in plan["channels"].values() for it in items}, log=log)
    return plan


def prune_cache(cfg: Config, keep: set[str], max_gb: float | None = None, log=print) -> list[str]:
    """Keep work/<vid>-fast analyses under daily.cache_max_gb (GitHub's cache holds 10 GB per repo; past that the
    save fails and the post job has to re-record the vlog). Oldest-used first; today's planned vlogs are never
    removed. Full (non-fast) analyses from local runs are not touched."""
    import shutil
    limit = float(max_gb if max_gb is not None else cfg["daily"].get("cache_max_gb", 6)) * 1024 ** 3
    root = cfg.path("work")
    dirs = []
    for d in root.glob("*-fast"):
        if not d.is_dir():
            continue
        size = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
        used = max((f.stat().st_mtime for f in d.glob("norm.json")), default=d.stat().st_mtime)
        dirs.append((used, size, d))
    total = sum(s for _, s, _ in dirs)
    gone = []
    for used, size, d in sorted(dirs):
        if total <= limit:
            break
        if d.name[: -len("-fast")] in keep:
            continue
        shutil.rmtree(d, ignore_errors=True)
        total -= size
        gone.append(d.name)
    if gone:
        log(f"  cache: removed {len(gone)} old analyses ({', '.join(gone)}); {total / 1024 ** 3:.1f} GB left")
    return gone


def old_vlog_order(cfg: Config, old: list[dict], exhausted, rng: random.Random) -> list[dict]:
    """Any old vlog, not always the next-newest: one already analysed (work/ cache: instant) first, then one random
    vlog from the whole pool (new variety; it has to be recorded, so only one per day), then the remaining cached
    ones, then the rest. Same date -> same order (re-runs make the same plan)."""
    old = [v for v in old if v["vid"] not in exhausted]
    work = cfg.path("work")
    cached = [v for v in old if (work / f"{v['vid']}-fast" / "norm.json").exists()]
    ids = {v["vid"] for v in cached}
    fresh = [v for v in old if v["vid"] not in ids]
    rng.shuffle(cached); rng.shuffle(fresh)
    return cached[:1] + fresh[:1] + cached[1:] + fresh[1:]


def _slot(d: dict, ch_index: int, k: int, now: dt.datetime, latest: bool | None = None,
          late: list | None = None) -> str | None:
    """k-th publish time of the day for this channel (ISO UTC). With `latest` given and publish_latest / publish_old
    set, the newest vlog's Shorts and the old vlogs' Shorts each use their own times. A slot that has already passed
    (late run, catch-up) or doesn't exist (more Shorts than times): with `late` (a one-item counter) it goes out
    soon - now + 30 min, then 30 min apart - instead of tomorrow, where it would collide with tomorrow's Shorts."""
    if not d.get("schedule", True):
        return None
    times = d.get("publish_times") or []
    if latest is not None and d.get("publish_latest") and d.get("publish_old"):
        times = d["publish_latest"] if latest else d["publish_old"]
    if not times:
        return None
    hh, mm = map(int, times[k % len(times)].split(":"))
    off = (d.get("channel_offset_min") or [0])[ch_index % len(d.get("channel_offset_min") or [0])]
    t = now.replace(hour=hh, minute=mm, second=0, microsecond=0) + dt.timedelta(minutes=off)
    if late is not None and (t < now + dt.timedelta(minutes=20) or k >= len(times)):
        t = now + dt.timedelta(minutes=30 + 30 * late[0])
        late[0] += 1
    elif t < now + dt.timedelta(minutes=20):
        t += dt.timedelta(days=1)
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def post(cfg: Config, channel: str, plan: dict | None = None, upload_videos: bool = True, log=print) -> int:
    from . import fast, upload, youtube
    cfg = prepare_cfg(cfg)
    d = cfg["daily"]
    now = _today(d)
    plan = plan or json.loads(ledger.plan_path(now.date().isoformat()).read_text(encoding="utf-8"))
    cfg["_used_titles"] = plan["used_titles"]                       # the exact settings the plan was made with
    cfg["engine"]["max_shorts"] = plan["max_moments_per_vlog"]
    # the plan's comments (cached in work/): fresh comments would re-score the moments and move the planned scenes
    cfg["viewers"] = dict(cfg.get("viewers", {}), allow_network=False)
    chans =[c["name"] for c in d["channels"]]
    ci = chans.index(channel)
    prefix = d["channels"][ci]["env"]
    items = plan["channels"].get(channel, [])
    done = {(r["vid"], round(r["start"], 3)) for r in ledger.posted()}
    items = [it for it in items if (it["vid"], round(it["start"], 3)) not in done]
    # never upload footage that overlaps anything already posted by ANY channel (not just the exact same scene)
    posted_spans: dict[str, list[tuple[float, float]]] = {}
    for r in ledger.posted():
        posted_spans.setdefault(r["vid"], []).append((r["start"], r["end"]))
    clash = [it for it in items if ledger.overlaps(posted_spans, it["vid"], it["start"], it["end"])]
    if clash:
        log(f"[{channel}] {len(clash)} planned scene(s) overlap footage already posted; skipped")
        items = [it for it in items if it not in clash]
    if os.environ.get("JOSHICLIP_TEST_ONE"):     # end-to-end test: one Short, published right away (no schedule)
        items = items[:1]
        d["schedule"] = False
        d["privacy"] = "public"
        log(f"[{channel}] TEST: only 1 Short, published immediately")
    if _yields(d, channel) and os.environ.get("GITHUB_ACTIONS"):
        # last guard: if both repos planned at the same time anyway, the later channel gives the scene up
        sync_partners(cfg, log)
        theirs = ledger.partner_spans(plan["date"], int(d.get("reserve_days", 2)))
        gone = [it for it in items if ledger.overlaps(theirs, it["vid"], it["start"], it["end"])]
        if gone:
            log(f"[{channel}] {len(gone)} planned scene(s) were taken by the other channel; skipped")
            items = [it for it in items if it not in gone]
    if not items:
        log(f"[{channel}] nothing left to post")
        return 0
    from . import record
    record.set_budget(60 * float(d.get("record_budget_minutes", 45)), len(items))   # 4K while it fits, else less
    svc = None
    if upload_videos:
        if not youtube.have_secrets(prefix):
            raise RuntimeError(f"{channel}: secrets {prefix}_CLIENT_ID / _CLIENT_SECRET / _REFRESH_TOKEN are not set")
        svc = youtube.service(prefix)
        log(f"[{channel}] uploading as YouTube channel: {youtube.channel_name(svc)}")
    from . import facebook
    try:
        fb_on = svc is not None and facebook.enabled(cfg)
    except Exception as e:  # noqa: BLE001 - never let Facebook stop YouTube
        fb_on = False
        log(f"[{channel}] facebook check failed ({str(e)[:120]}); YouTube only")
    if fb_on:
        log(f"[{channel}] also posting every Short to the Facebook Page (Reels)")
    deadline = time.time() + 60 * float(d.get("post_max_minutes", 300))
    today_posts = [r for r in ledger.posted() if r["channel"] == channel and r.get("plan_date") == plan["date"]]
    k_kind = {True: sum(1 for r in today_posts if r.get("latest")), False: sum(1 for r in today_posts if r.get("latest") is False)}
    n_ok, bad = 0, 0
    late = [0]                                    # passed slots of a late run go out 30 min apart, from now
    by_vid: dict[str, list[dict]] = {}
    for it in items:
        by_vid.setdefault(it["vid"], []).append(it)
    # latest vlog first (gets the first publish slots), then older ones in plan order
    for vid in sorted(by_vid, key=lambda v: (not by_vid[v][0].get("latest"), list(by_vid).index(v))):
        if time.time() > deadline:
            log(f"[{channel}] time budget used up; the rest stays for a later run")
            break
        group = by_vid[vid]
        od = cfg.path("out") / "daily" / plan["date"] / channel / vid
        try:
            fast.run_fast(cfg, vid, False, vid, "final", cfg["engine"]["max_shorts"], log,
                          pick=[it["cand"] for it in group], out_dir=od,
                          pick_spans=[(it["start"], it["end"]) for it in group])
        except Exception as e:  # noqa: BLE001
            log(f"[{channel}] {vid}: render failed ({type(e).__name__}: {str(e)[:300]})"); bad += len(group); continue
        rep = json.loads((od / "report.json").read_text(encoding="utf-8"))
        packs = {p["file"]: p for p in upload.build(rep, cfg, plan.get("vlog_titles", {}).get(vid))}
        for s in sorted(rep["shorts"], key=lambda s: (-s["final"], s["start"])):
            if not (s.get("checks") or {}).get("ok"):
                log(f"[{channel}] {s['file']}: sync check failed -> not uploaded"); bad += 1; continue
            p = packs[s["file"]]
            rec = {"channel": channel, "vid": vid, "start": s["start"], "end": s["end"], "title": s["title"],
                   "plan_date": plan["date"], "file": s["file"], "latest": bool(group[0].get("latest"))}
            if svc is None:
                log(f"[{channel}] rendered (not uploaded): {od / s['file']}"); n_ok += 1; continue
            is_latest = bool(group[0].get("latest"))
            when = _slot(d, ci, k_kind[is_latest], _today(d), is_latest, late)   # the time NOW (renders take a while)
            try:
                yid = youtube.upload(svc, od / s["file"], p["title"], p["description"], p["hashtags"] + p.get("keywords", []),
                                     str(d.get("category_id", "24")), when, d.get("privacy", "public"), log)
            except youtube.QuotaExceeded as e:
                log(f"[{channel}] YouTube quota reached ({e}); stopping for today"); return 1 if n_ok == 0 else 0
            rec.update(yt_id=yid, publish_at=when, uploaded=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))
            if fb_on:      # Facebook can never stop or undo YouTube: any error is only logged
                try:
                    rec["fb_id"] = facebook.post_reel(cfg, od / s["file"], facebook.caption(
                        p["title"], p["hashtags"], cfg.get("upload", {}).get("credit_line"), p.get("link")), when, log)
                except Exception as e:  # noqa: BLE001
                    log(f"[{channel}] facebook skipped for {s['file']}: {str(e)[:200]}")
            ledger.git_commit([ledger.record_post(channel, rec)], f"{channel}: posted {yid}", _push(cfg), log)
            log(f"[{channel}] uploaded https://youtu.be/{yid}  {p['title']}  (publish {when or 'now'})")
            k_kind[is_latest] += 1; n_ok += 1
            if d.get("delete_after_upload", True):
                _delete_uploaded(od, s["file"])
        if svc is not None and d.get("delete_after_upload", True):
            _delete_sources(od, log)
    log(f"[{channel}] done: {n_ok} posted, {bad} failed")
    return 0 if n_ok else 1


def _delete_uploaded(od: Path, file: str) -> None:
    """Free the disk once YouTube has the Short: its mp4 + its work folder (short_NN/, jump-cut pieces)."""
    import shutil
    mp4 = od / file
    mp4.unlink(missing_ok=True)
    shutil.rmtree(od / mp4.stem, ignore_errors=True)
    shutil.rmtree(od / f"short_{file[:2]}", ignore_errors=True)


def _delete_sources(od: Path, log=print) -> None:
    """After a vlog's uploads: the recorded / downloaded 4K clips (the largest files). report.md / upload.md stay."""
    n = 0
    for f in list(od.glob("clip_*")) + list(od.glob("source.info.json")):
        try:
            f.unlink(); n += 1
        except OSError:
            pass
    if n:
        log(f"    deleted {n} source clip files in {od}")


def run(cfg: Config, dry_run: bool = False, upload_videos: bool = True, log=print, replan: bool = False) -> int:
    plan = plan_day(cfg, dry_run, log, replan)
    if dry_run:
        for ch, items in plan["channels"].items():
            log(f"\n{ch}:")
            for it in items:
                log(f"  {'L' if it['latest'] else ' '} {it['vid']} {it['start']:8.2f}-{it['end']:8.2f}  {it['final']:.3f}  {it['title']}")
        return 0
    rc = 0
    for ch in plan["channels"]:
        rc |= post(cfg, ch, plan, upload_videos, log)
    return rc
