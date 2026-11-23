"""Media handed over by the phone (home Wi-Fi: YouTube doesn't bot-check it) -> GitHub runners (YouTube does).

Storage = the assets of one release ("media") in a PRIVATE repo (config daily.media.repo). Token: MEDIA_TOKEN
(classic GitHub token with `repo` + `workflow`; on the phone the same token also starts the daily runs).

  v360_<vid>.mkv / .json3 / .info.json   360p analysis copy + Hindi captions + metadata   (phone, before planning)
  clip_<vid>_<a>_<b>.mkv                 1080p span a..b seconds (pad included)           (phone, after the plan)
  manifest_<date>.json                   today's vlogs, newest first                       (phone)

Asset names use only [A-Za-z0-9._-]; assets older than keep_days are deleted by the phone.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

API = "https://api.github.com"


def _cfg(cfg) -> dict:
    return (cfg.get("daily") or {}).get("media") or {}


def token() -> str:
    from .tools import _dotenv
    return (os.environ.get("MEDIA_TOKEN") or _dotenv().get("MEDIA_TOKEN") or "").strip()


def enabled(cfg) -> bool:
    return bool(_cfg(cfg).get("repo")) and bool(token())


def _h(extra: dict | None = None) -> dict:
    return dict({"Authorization": f"Bearer {token()}", "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28"}, **(extra or {}))


def _req(method: str, url: str, ok=(200, 201, 204), tries: int = 4, **kw):
    import requests
    last = None
    for k in range(tries):
        try:
            r = requests.request(method, url, timeout=kw.pop("timeout", 120), **kw)
            if r.status_code in ok or r.status_code == 404 or (r.status_code == 422 and method == "POST"):
                return r
            last = RuntimeError(f"GitHub {method} {url.split('?')[0][-80:]}: {r.status_code} {r.text[:200]}")
            if r.status_code in (401, 403) and "rate limit" not in r.text.lower():
                raise last
        except requests.RequestException as e:
            last = e
        time.sleep(5 * (k + 1))
    raise RuntimeError(str(last))


_REL: dict = {}


def release(cfg) -> dict:
    """The 'media' release of the private repo (created on first use)."""
    m = _cfg(cfg)
    repo, tag = m["repo"], m.get("tag", "media")
    if repo in _REL:
        return _REL[repo]
    r = _req("GET", f"{API}/repos/{repo}/releases/tags/{tag}", headers=_h())
    if r.status_code == 404:
        r = _req("POST", f"{API}/repos/{repo}/releases", headers=_h(), json={
            "tag_name": tag, "name": "media (phone -> GitHub handover)", "prerelease": True,
            "body": "Temporary files for the daily Shorts. Old assets are deleted automatically."})
        if r.status_code not in (200, 201):
            raise RuntimeError(f"cannot create the media release in {repo}: {r.status_code} {r.text[:200]}")
    _REL[repo] = r.json()
    return _REL[repo]


def assets(cfg) -> dict[str, dict]:
    rel, out, page = release(cfg), {}, 1
    repo = _cfg(cfg)["repo"]
    while True:
        r = _req("GET", f"{API}/repos/{repo}/releases/{rel['id']}/assets?per_page=100&page={page}", headers=_h())
        items = r.json() if r.status_code == 200 else []
        for a in items:
            out[a["name"]] = a
        if len(items) < 100:
            return out
        page += 1


def safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)


def upload(cfg, path: Path, name: str, have: dict | None = None) -> bool:
    """False when an asset of that name is already there (nothing to do)."""
    name = safe(name)
    have = assets(cfg) if have is None else have
    if name in have and have[name].get("state") == "uploaded" and have[name].get("size"):
        return False
    if name in have:                                  # a broken/partial one: replace it
        _req("DELETE", f"{API}/repos/{_cfg(cfg)['repo']}/releases/assets/{have[name]['id']}", headers=_h())
    rel = release(cfg)
    up = rel["upload_url"].split("{")[0]
    with open(path, "rb") as f:
        r = _req("POST", f"{up}?name={name}", headers=_h({"Content-Type": "application/octet-stream"}), data=f,
                 timeout=1800, tries=1)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"upload {name}: {r.status_code} {r.text[:200]}")
    have[name] = r.json()
    return True


def download(cfg, name: str, dest: Path, have: dict | None = None) -> bool:
    import requests
    name = safe(name)
    have = assets(cfg) if have is None else have
    a = have.get(name)
    if not a:
        return False
    tmp = dest.with_name(dest.name + ".part")
    dest.parent.mkdir(parents=True, exist_ok=True)
    for k in range(4):
        try:
            with requests.get(f"{API}/repos/{_cfg(cfg)['repo']}/releases/assets/{a['id']}", stream=True, timeout=300,
                              headers=_h({"Accept": "application/octet-stream"})) as r:
                r.raise_for_status()
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(1 << 20):
                        f.write(chunk)
            if a.get("size") and tmp.stat().st_size != a["size"]:
                raise RuntimeError(f"size {tmp.stat().st_size} != {a['size']}")
            tmp.replace(dest)
            return True
        except Exception:  # noqa: BLE001
            time.sleep(10 * (k + 1))
    return False


def delete_older(cfg, days: float | None = None, log=print) -> int:
    import datetime as dt
    keep = float(days if days is not None else _cfg(cfg).get("keep_days", 4))
    cut = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=keep)
    n = 0
    for name, a in assets(cfg).items():
        t = dt.datetime.fromisoformat(a["created_at"].replace("Z", "+00:00"))
        if t < cut:
            _req("DELETE", f"{API}/repos/{_cfg(cfg)['repo']}/releases/assets/{a['id']}", headers=_h())
            n += 1
    if n:
        log(f"  media store: deleted {n} old files")
    return n


# ---------------------------------------------------------------- what the daily run reads
def clip_name(vid: str, a: float, b: float) -> str:
    return safe(f"clip_{vid}_{a:.2f}_{b:.2f}.mkv")


def manifest(cfg, date: str) -> dict | None:
    import tempfile
    have = assets(cfg)
    name = f"manifest_{date}.json"
    if name not in have:
        return None
    p = Path(tempfile.mkdtemp()) / name
    return json.loads(p.read_text(encoding="utf-8")) if download(cfg, name, p, have) else None


def fetch_360(cfg, vid: str, wd: Path, src: Path, log=print) -> bool:
    """The phone's 360p copy -> src (+ captions + info json named like yt-dlp's own)."""
    have = assets(cfg)
    if f"v360_{safe(vid)}.mkv" not in have:
        return False
    if not download(cfg, f"v360_{vid}.mkv", src, have):
        return False
    download(cfg, f"v360_{vid}.json3", wd / "src360.hi.json3", have)
    download(cfg, f"v360_{vid}.info.json", wd / "src360.info.json", have)
    log(f"  360p copy from the phone (media store): {src.stat().st_size // 1_000_000} MB")
    return True


def wait_clips(cfg, vid: str, spans: list, log=print) -> list:
    """Each span's 1080p clip from the phone; waits up to daily.media.clip_wait_min for the phone to finish.
    Returns the paths, None where a clip never arrived. A clip is matched to its span within 0.1 s."""
    deadline = time.time() + 60 * float(_cfg(cfg).get("clip_wait_min", 150))
    got: list = [None] * len(spans)
    said = False
    while True:
        have = assets(cfg)
        for i, (a, b, out) in enumerate(spans):
            if got[i] is not None:
                continue
            if Path(out).exists():
                got[i] = Path(out)
                continue
            pre = safe(f"clip_{vid}_")
            for name in have:
                m = re.match(re.escape(pre) + r"([0-9.]+)_([0-9.]+)\.mkv$", name)
                if m and abs(float(m.group(1)) - a) < 0.1 and abs(float(m.group(2)) - b) < 0.1:
                    if download(cfg, name, Path(out), have):
                        got[i] = Path(out)
                    break
        if all(g is not None for g in got) or time.time() > deadline:
            if not all(g is not None for g in got):
                log(f"  media store: {sum(g is None for g in got)} clip(s) of {vid} never arrived from the phone")
            return got
        if not said:
            log(f"  waiting for the phone's 1080p clips of {vid} ({sum(g is not None for g in got)}/{len(got)} here)")
            said = True
        time.sleep(60)
