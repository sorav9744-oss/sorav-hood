"""Fast mode (`joshiclip run --fast`): same picks + same look in ~10-15 min instead of ~2.5 h.

1. download only the 360p stream + YouTube Hindi auto-captions (json3, word timings)       ~1 min
2. analyze the 360p copy (captions as transcript; batched Whisper turbo only if no captions)  ~3 min
3. download ONLY the winning spans in 4K (yt-dlp --download-sections, parallel)              ~1 min
4. align each 4K clip to the analysis timeline by audio cross-correlation (sample-accurate),
   check the clip's own A/V against the 360p video, verify CFR                              seconds
5. voice (Demucs/DFN/...) for all Shorts in a worker pool while frames render               ~6-8 min
"""
from __future__ import annotations

import copy
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

from . import ingest, media, record, tools
from .config import Config

FAST_VERSION = 1


BOT_CHECK = ("confirm you", "not a bot", "HTTP Error 429", "rate-limit")


def _routes() -> list[bool]:
    """[True] = through the proxy (Cloudflare WARP on GitHub runners), [False] = direct. With a proxy set, try it
    first and fall back to direct when its exit IP is blocked (and the other way round on the next round)."""
    import os
    return [True, False] if (os.environ.get("JOSHICLIP_YTDLP_PROXY") or tools._dotenv().get("JOSHICLIP_YTDLP_PROXY")) else [True]


def _ytdlp(cfg: Config, args: list[str], waits=(90, 300)) -> None:
    """YouTube's "Sign in to confirm you're not a bot" depends on the IP: try every route (proxy, direct), then wait
    and retry (90 s, then 5 min) before giving up with instructions."""
    for k in range(len(waits) + 1):
        last = None
        for via_proxy in _routes():
            try:
                media.run(ingest.ytdlp_base(cfg, via_proxy) + args)
                return
            except RuntimeError as e:
                if not any(x in str(e) for x in BOT_CHECK):
                    raise
                last = e
                print(f"    YouTube bot check {'via proxy' if via_proxy and len(_routes()) > 1 else 'direct'}", flush=True)
        if k == len(waits):
            raise RuntimeError(
                "YouTube is bot-checking this connection. Wait 1-2 hours (the daily job retries by itself later), or "
                "give yt-dlp cookies: cookies.txt in the project folder / YTDLP_COOKIES secret, or "
                "JOSHICLIP_YTDLP_BROWSER=firefox in .env (see docs/DAILY_SETUP.md).\n" + str(last)[-600:]) from None
        print(f"    YouTube bot check; waiting {waits[k]} s and retrying ({k + 1}/{len(waits)})", flush=True)
        time.sleep(waits[k])


def _fetch_360(cfg: Config, url: str, vid: str, wd: Path, src: Path, log=print) -> None:
    """360p analysis copy + Hindi captions: the phone's copy (media store) first, else yt-dlp, else recorded in
    Chrome (JOSHICLIP_FETCH=record/auto)."""
    from . import mediastore
    if mediastore.enabled(cfg):
        try:
            if mediastore.fetch_360(cfg, vid, wd, src, log):
                return
            log("  no 360p copy from the phone for this vlog; fetching it here")
        except Exception as e:  # noqa: BLE001
            log(f"  media store unavailable ({str(e)[:120]}); fetching here")
    m = record.mode()
    if m != "record":
        f = cfg["fast"]
        try:
            _ytdlp(cfg, ["-f", f["proxy_format"], "--merge-output-format", "mkv", "--continue", "-o", str(src),
                         "--write-auto-subs", "--sub-langs", f["sub_langs"], "--sub-format", "json3",
                         "--write-info-json", url])
            return
        except RuntimeError as e:
            if m != "auto":
                raise
            log(f"  yt-dlp failed ({str(e).splitlines()[0][:80]}); recording in Chrome instead")
    try:
        record.record_full(cfg, vid, wd, log)
    except Exception as e:  # noqa: BLE001 - YouTube changed its player etc.: one plain download try before giving up
        if m != "record":
            raise
        log(f"  Chrome recording failed ({type(e).__name__}: {str(e)[:120]}); trying yt-dlp once")
        f = cfg["fast"]
        try:
            _ytdlp(cfg, ["-f", f["proxy_format"], "--merge-output-format", "mkv", "--continue", "-o", str(src),
                         "--write-auto-subs", "--sub-langs", f["sub_langs"], "--sub-format", "json3",
                         "--write-info-json", url], waits=())
        except Exception:  # noqa: BLE001
            raise e from None


def _get_clips(cfg: Config, url: str, vid: str, spans: list, log=print) -> list[Path]:
    """HD spans: the phone's 1080p clips (media store) first, else yt-dlp --download-sections, else Chrome."""
    from . import mediastore
    if mediastore.enabled(cfg):
        try:
            got = mediastore.wait_clips(cfg, vid, spans, log)
        except Exception as e:  # noqa: BLE001
            log(f"  media store unavailable ({str(e)[:120]}); fetching the clips here")
            got = [None] * len(spans)
        if all(g is not None for g in got):
            return got
        rest = [sp for sp, g in zip(spans, got) if g is None]
        fetched = iter(_get_clips_here(cfg, url, vid, rest, log))
        return [g if g is not None else next(fetched) for g in got]
    return _get_clips_here(cfg, url, vid, spans, log)


def _get_clips_here(cfg: Config, url: str, vid: str, spans: list, log=print) -> list[Path]:
    m = record.mode()
    if m != "record":
        try:
            info = fetch_info(cfg, url, spans[0][2].parent) if any(not o.exists() for _, _, o in spans) else None
            with ThreadPoolExecutor(cfg["fast"]["parallel_downloads"]) as ex:
                futs = [ex.submit(download_clip, cfg, url, a, b, o, info) for a, b, o in spans]
                return [fu.result() for fu in futs]
        except RuntimeError as e:
            if m != "auto":
                raise
            log(f"  yt-dlp failed ({str(e).splitlines()[0][:80]}); recording the clips in Chrome instead")
    try:
        return record.record_clips(cfg, vid, spans, log)
    except Exception as e:  # noqa: BLE001 - same fallback as the analysis copy
        if m != "record":
            raise
        log(f"  Chrome recording failed ({type(e).__name__}: {str(e)[:120]}); trying yt-dlp once")
        try:
            info = fetch_info(cfg, url, spans[0][2].parent)
            return [download_clip(cfg, url, a, b, o, info, waits=()) for a, b, o in spans]
        except Exception:  # noqa: BLE001
            raise e from None


def ingest_lite(cfg: Config, target, latest, vid, log=print):
    f = cfg["fast"]
    if latest or not target:
        ingest.ytdlp_update(cfg, log)
        target = ingest.latest_long_video(cfg)
        log(f"  newest long video: {target}")
    vid = vid or ingest.video_id_for(target)
    url = target if str(target).startswith("http") else f"https://www.youtube.com/watch?v={vid}"
    wd = cfg.path("work") / f"{vid}-fast"
    wd.mkdir(parents=True, exist_ok=True)
    meta_p = wd / "norm.json"
    if meta_p.exists() and (wd / "proxy_360p.mp4").exists() and (wd / "audio_16k_mono.wav").exists():
        log("  [cache] 360p ingest")
        return vid, wd, json.loads(meta_p.read_text(encoding="utf-8"))
    if f.get("source", "auto") != "local" and record.mode() != "record":
        ingest.ytdlp_update(cfg, log)
    t = time.perf_counter()
    src = wd / "src360.mkv"
    local = cfg.path("work") / vid
    want_local = f.get("source", "auto") == "local"
    if not src.exists():
        try:
            if want_local:
                raise RuntimeError("fast.source = local")
            _fetch_360(cfg, url, vid, wd, src, log)
        except Exception as e:  # noqa: BLE001
            if not ((local / "norm.mp4").exists() and (local / "proxy_360p.mp4").exists()):
                raise
            log(f"  YouTube unavailable ({str(e).splitlines()[0][:80]}); using the local normalized copy in {local}")
            import shutil
            for fn in ("proxy_360p.mp4", "audio_16k_mono.wav"):
                shutil.copy2(local / fn, wd / fn)
            nm = json.loads((local / "norm.json").read_text(encoding="utf-8"))
            meta = {"key": {"vid": vid, "local": nm["key"], "v": FAST_VERSION}, "source": url, "fps": nm["proxy_fps"],
                    "proxy_fps": nm["proxy_fps"], "width": 640, "height": 360, "duration": nm["duration"],
                    "timings_s": {"local_copy": round(time.perf_counter() - t, 1)}, "fast": True,
                    "local_norm": str(local / "norm.mp4"), "norm_fps": nm["fps"], "norm_w": nm["width"], "norm_h": nm["height"]}
            meta_p.write_text(json.dumps(meta, indent=1), encoding="utf-8")
            return vid, wd, meta
    for p in wd.glob("src360.*.json3"):
        p.rename(wd / p.name.replace("src360", vid))
    t_dl = time.perf_counter() - t
    t = time.perf_counter()
    info = media.probe(src)
    fps = ingest.nominal_fps(info, [30, 60])
    pts = media.frame_pts(src)
    d = np.diff(pts)
    cfr = bool(len(d) and abs(d.max() - 1 / fps) < 1e-3 and abs(d.min() - 1 / fps) < 1e-3)
    vcodec = ["-c:v", "copy"] if cfr else ["-vf", f"fps={fps}", "-fps_mode", "cfr", "-c:v", "libx264", "-preset",
                                            "ultrafast", "-crf", "20", "-g", str(2 * fps)]
    media.run([media.ff(), "-v", "error", "-y", "-i", str(src), "-map", "0:v:0", "-map", "0:a:0", *vcodec,
               "-af", "aresample=48000:async=1:first_pts=0", "-c:a", "aac", "-b:a", "128k", str(wd / "proxy_360p.mp4")])
    media.run([media.ff(), "-v", "error", "-y", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000",
               "-af", "aresample=16000:async=1:first_pts=0", "-c:a", "pcm_s16le", "-fflags", "+bitexact",
               str(wd / "audio_16k_mono.wav")])
    info_json = next(iter(sorted(wd.glob("*.info.json"))), None)
    yt = json.loads(info_json.read_text(encoding="utf-8")) if info_json else {}
    meta = {"key": {"vid": vid, "src360": ingest.file_fingerprint(src), "v": FAST_VERSION, "cfr_copy": cfr},
            "source": url, "fps": fps, "proxy_fps": fps, "width": info["video"]["width"], "height": info["video"]["height"],
            "duration": media.probe(wd / "proxy_360p.mp4")["duration"], "timings_s": {"download_360p": round(t_dl, 1),
            "proxy_audio": round(time.perf_counter() - t, 1)}, "fast": True, "title": yt.get("title"),
            "source_fps": yt.get("fps"), "source_height": yt.get("height")}
    meta_p.write_text(json.dumps(meta, indent=1, ensure_ascii=False), encoding="utf-8")
    src.unlink(missing_ok=True)      # ~100 MB; everything later reads proxy_360p.mp4 / the wav (smaller CI cache)
    return vid, wd, meta


# ---------------------------------------------------------------- 4K clips
def fetch_info(cfg: Config, url: str, out_dir: Path) -> Path:
    """ONE YouTube extraction per vlog; every clip then downloads from this file (--load-info-json).
    Before: each of the 8 clips did its own full extraction, 3 at a time -> bursts that trip the bot check.
    Stream URLs inside stay valid ~6 h, so the file is refreshed when older than 2 h."""
    p = out_dir / "source.info.json"
    if p.exists() and time.time() - p.stat().st_mtime < 2 * 3600:
        return p
    stem = out_dir / "source"
    _ytdlp(cfg, ["--skip-download", "--write-info-json", "-f", cfg["fast"]["clip_format"], "-o", str(stem), url])
    if not p.exists():
        raise RuntimeError("yt-dlp wrote no info json")
    return p


def download_clip(cfg: Config, url: str, a: float, b: float, out: Path, info: Path | None = None,
                  waits=(90, 300)) -> Path:
    if out.exists():
        return out
    tmp = out.with_name(out.stem + ".part" + out.suffix)
    src = ["--load-info-json", str(info)] if info else [url]
    _ytdlp(cfg, ["-f", cfg["fast"]["clip_format"], "--download-sections", f"*{a:.3f}-{b:.3f}",
                 "--merge-output-format", "mkv", "-o", str(tmp), *src], waits=waits)
    got = sorted(tmp.parent.glob(tmp.stem + "*"))
    got = [g for g in got if g.suffix in (".mkv", ".mp4", ".webm")]
    if not got:
        raise RuntimeError(f"clip download failed for {a}-{b}")
    got[0].replace(out)
    return out


def _env(x: np.ndarray, hop: int) -> np.ndarray:
    n = len(x) // hop
    return np.sqrt((x[: n * hop].reshape(n, hop) ** 2).mean(1) + 1e-10)


def audio_offset(clip: Path, ref_wav: Path, approx_start: float, search: float) -> tuple[float, float]:
    """off such that analysis_time = clip_time + off. 5 ms envelope FFT xcorr, then raw-sample refinement."""
    import soundfile as sf
    from scipy.signal import fftconvolve
    sr = 16000
    c = media.read_audio(clip, sr)
    ref, _ = sf.read(str(ref_wav), dtype="float32")
    r0 = max(0.0, approx_start - search)
    r = ref[int(r0 * sr): int((approx_start + len(c) / sr + search) * sr)]
    hop = 80
    ce, re_ = np.log(_env(c, hop)), np.log(_env(r, hop))
    ce = (ce - ce.mean()) / (ce.std() + 1e-9); re_ = (re_ - re_.mean()) / (re_.std() + 1e-9)
    xc = fftconvolve(re_, ce[::-1], mode="valid")
    k = int(np.argmax(xc))
    coarse = r0 + k * hop / sr
    # refine on raw samples (+-10 ms) over a 6 s window in the middle of the clip
    m0 = int(len(c) / 2 - 3 * sr)
    seg = c[max(0, m0): max(0, m0) + 6 * sr]
    best, bl = -1e9, 0
    base = int((coarse - r0) * sr) + max(0, m0)
    for L in range(-160, 161):
        rr = r[base + L: base + L + len(seg)]
        if len(rr) < len(seg):
            continue
        v = float(np.dot(seg, rr) / (np.linalg.norm(seg) * np.linalg.norm(rr) + 1e-9))
        if v > best:
            best, bl = v, L
    return coarse + bl / sr, best


def video_offset(clip: Path, proxy: Path, off_audio: float, fps: int) -> tuple[float, float]:
    """Clip's video offset vs the 360p video around the audio offset (to detect A/V-desynced section cuts)."""
    from .sync import xcorr_lag
    dur = media.probe(clip)["duration"]
    t0, L = dur / 2 - 2.0, 4.0
    a = media.read_gray(clip, t0, L, fps, 160, 90)
    b = media.read_gray(proxy, t0 + off_audio - 0.5, L + 1.0, fps, 160, 90)
    aa = np.abs(np.diff(a, axis=0)).mean((1, 2)); bb = np.abs(np.diff(b, axis=0)).mean((1, 2))
    lag, c = xcorr_lag(aa, bb, int(0.5 * fps))
    return off_audio + lag / fps, c


def run_fast(cfg: Config, target, latest=False, vid=None, mode="final", max_shorts=None, log=print,
             pick: list[int] | None = None, out_dir: Path | None = None,
             pick_spans: list[tuple[float, float]] | None = None) -> int:
    """pick: render only these candidate ids (daily scheduler), pick_spans: the (start, end) each had when it was
    planned (an id whose span moved is looked up by span); out_dir: where the Shorts + report go."""
    from . import pipeline, report, sync, vertical, voice
    T0 = time.perf_counter()
    cfg = copy.deepcopy(cfg)
    cfg["render"][mode] = dict(cfg["render"][mode], preset=cfg["fast"]["render_preset"])
    cfg["render"]["frame_workers"] = cfg["fast"]["frame_workers"]
    cfg["render"]["x264_threads"] = cfg["fast"]["x264_threads"]
    cfg["voice"] = dict(cfg["voice"], separator=cfg["fast"]["separator"])
    A = pipeline.analyze(cfg, target, latest, vid, log, fast=True)
    C = A["cache"]
    t_an = time.perf_counter() - T0
    cands, sel = pipeline.plan(cfg, A, max_shorts)
    if pick is not None:
        sel = dict(sel, winners=_match_pick(cands, pick, pick_spans, log))
    vid = A["vid"]
    url = A["meta"]["source"]
    out_dir = Path(out_dir) if out_dir else cfg.path("out") / f"{vid}-fast"
    out_dir.mkdir(parents=True, exist_ok=True)
    _acquire_lock(out_dir)
    wins = [cands[i] for i in sel["winners"]]
    log(f"  plan: {len(wins)} Shorts after {t_an:.0f}s -> downloading 4K spans")
    pad = cfg["fast"]["clip_pad_s"]

    # 3. 4K clips in parallel (or the local normalized 4K file when YouTube was unavailable)
    t = time.perf_counter()
    clips = {}
    local_norm = A["meta"].get("local_norm") if record.mode() != "record" else None   # record mode: always record
    if not local_norm and wins:
        # the span is in the name: a scene that moved never reuses an old clip of the same rank
        spans = [(max(0.0, c["start"] - pad), c["end"] + pad,
                  out_dir / f"clip_{k:02d}_{max(0.0, c['start'] - pad):.0f}-{c['end'] + pad:.0f}.mkv")
                 for k, c in enumerate(wins, 1)]
        for c, p in zip(wins, _get_clips(cfg, url, vid, spans, log)):
            clips[c["id"]] = p
    C.timings["download_4k_clips"] = round(time.perf_counter() - t, 1)

    # 4. offsets + CFR check
    t = time.perf_counter()
    srcs = {}
    for k, c in enumerate(wins, 1):
        if local_norm:
            srcs[c["id"]] = {"path": local_norm, "off": 0.0, "fps": A["meta"]["norm_fps"], "width": A["meta"]["norm_w"],
                             "height": A["meta"]["norm_h"], "local": True}
            continue
        clip = clips[c["id"]]
        info = media.probe(clip)
        fps = ingest.nominal_fps(info, [30, 60])
        offa, ca = audio_offset(clip, A["workdir"] / "audio_16k_mono.wav", max(0.0, c["start"] - pad), 12.0)
        offv, cv = video_offset(clip, A["workdir"] / "proxy_360p.mp4", offa, A["fps"])
        pts = media.frame_pts(clip)
        d = np.diff(pts)
        cfr = bool(abs(np.median(d) - 1 / fps) < 1e-3 and (np.abs(d - 1 / fps) > 0.5 / fps).mean() < 0.002)
        srcs[c["id"]] = {"path": str(clip), "off": offa, "fps": fps, "width": info["video"]["width"],
                         "height": info["video"]["height"], "audio_corr": round(ca, 3), "video_off": round(offv, 4),
                         "video_corr": round(cv, 3), "av_internal_ms": round((offv - offa) * 1000, 1), "cfr": cfr}
        log(f"  clip_{k:02d}: {info['video']['width']}x{info['video']['height']}@{fps}  off {offa:.4f}s (corr {ca:.2f})  "
            f"clip A/V {srcs[c['id']]['av_internal_ms']} ms  CFR {cfr}")
        # Backstop for A/V drift inside a clip (record.py already removes Chrome's measured lag): only a clear,
        # confidently measured offset of 2+ frames is corrected, so measurement noise never makes sync worse.
        dav = offa - offv
        fix = abs(dav) >= 2.0 / fps and cv >= 0.8
        if fix or not cfr:
            vin = ["-itsoffset", f"{-dav:.4f}"] if fix and dav < 0 else []
            ain = ["-itsoffset", f"{dav:.4f}"] if fix and dav > 0 else []
            venc = ["-c:v", "copy"] if cfr else ["-vf", f"fps={fps}", "-fps_mode", "cfr", "-c:v", "libx264",
                                                 "-preset", "veryfast", "-crf", "15"]
            nclip = clip.with_name(clip.stem + "_fixed.mkv")
            media.run([media.ff(), "-v", "error", "-y", *vin, "-i", str(clip), *ain, "-i", str(clip), "-map", "0:v:0",
                       "-map", "1:a:0", *venc, "-af", "aresample=48000:async=1:first_pts=0", "-c:a", "flac", str(nclip)])
            srcs[c["id"]]["path"] = str(nclip)
            if fix:
                srcs[c["id"]].update(off=offv if dav > 0 else offa, av_fixed_ms=round(dav * 1000, 1))
                log(f"    clip_{k:02d}: A/V corrected by {dav * 1000:+.0f} ms")
    C.timings["align_clips"] = round(time.perf_counter() - t, 1)

    renders = {}
    # 5. every Short renders in its OWN process (no shared GIL): voice -> frames -> sync check -> result.json
    t = time.perf_counter()
    from .config import stable_hash
    cfg_key = stable_hash({k: cfg.get(k) for k in ("voice", "music", "jumpcuts", "layout", "look", "look_tune", "camera", "render")})
    jobs = []
    for k, c in enumerate(wins, 1):
        jd = out_dir / f"short_{k:02d}"
        jd.mkdir(exist_ok=True)
        job = {"cfg": dict(cfg), "vid": vid, "k": k, "cand": c["id"], "start": c["start"], "src": srcs[c["id"]],
               "out_dir": str(out_dir), "mode": mode, "cfg_key": cfg_key, "max_shorts": max_shorts}
        (jd / "job.json").write_text(json.dumps(job, ensure_ascii=False, default=str), encoding="utf-8")
        jobs.append(jd)
    running, pending, failed = [], list(jobs), []
    par = cfg["fast"]["parallel_renders"]
    while pending or running:
        while pending and len(running) < par:
            jd = pending.pop(0)
            lf = open(jd / "worker.log", "w", encoding="utf-8")
            pr = subprocess.Popen([sys.executable, "-m", "joshiclip.fast", str(jd / "job.json")], stdout=lf, stderr=subprocess.STDOUT,
                                  env=dict(tools.child_env(), PYTHONUNBUFFERED="1"))
            running.append((jd, pr, lf))
        time.sleep(1.0)
        for item in list(running):
            jd, pr, lf = item
            if pr.poll() is not None:
                lf.close(); running.remove(item)
                res = jd / "result.json"
                if pr.returncode != 0 or not res.exists():
                    # one broken Short must not cost the others: it is reported without checks -> not uploaded
                    tail = (jd / "worker.log").read_text(encoding="utf-8", errors="ignore")[-1500:]
                    log(f"  {jd.name} FAILED (rc={pr.returncode}), the other Shorts continue:\n{tail}")
                    failed.append(jd.name)
                    continue
                r = json.loads(res.read_text(encoding="utf-8"))
                ch, vr = r["result"]["checks"], r["result"]["vertical"]
                c = next(x for x in wins if x["id"] == r["cand"])
                renders[r["cand"]] = r["result"]
                C.timings[f"render_{jd.name}"] = r.get("seconds")
                log(f"  {jd.name} {c['start']:.1f}-{c['end']:.1f}s  {c['title']}  sync {'OK' if ch['ok'] else 'FAIL'} "
                    f"(A/V {ch['max_abs_av_offset_ms']} ms, {vr['render_fps']} fps, {len(vr.get('jump_cuts') or [])} silence cuts)"
                    f"  [{time.perf_counter() - T0:.0f}s total]")
    C.timings["voice_and_render"] = round(time.perf_counter() - t, 1)
    C.timings["TOTAL"] = round(time.perf_counter() - T0, 1)
    for k, c in enumerate(wins, 1):          # the worker names the mp4; the parent's report must use the same name
        c["file"] = pipeline.clip_filename(k, c["title"])
    rep = report.build(A, cands, sel, cfg, renders)
    rep["mode"] = "fast"
    report.write(rep, out_dir, C.timings)
    from . import upload, viewers
    log(f"  upload pack: {upload.write(rep, cfg, out_dir, A['meta'].get('title') or viewers.vlog_title(A['workdir']))}")
    log(f"  done in {C.timings['TOTAL'] / 60:.1f} min -> {out_dir}" + (f"  ({len(failed)} failed: {', '.join(failed)})" if failed else ""))
    return 0 if not failed and all(r["checks"]["ok"] for r in renders.values()) else 1


def _match_pick(cands: list[dict], pick: list[int], spans, log=print) -> list[int]:
    """Candidate ids to render. Without spans: the ids as given. With the planned spans: an id whose start/end still
    match is kept; otherwise the candidate with that exact span is used (ids can shift when the analysis is rebuilt);
    a scene that no longer exists is skipped instead of failing the whole vlog."""
    if not spans:
        return list(pick)
    out = []
    for i, (a, b) in zip(pick, spans):
        def same(c):
            return abs(c["start"] - a) < 0.05 and abs(c["end"] - b) < 0.05
        if 0 <= i < len(cands) and same(cands[i]):
            out.append(i)
            continue
        alt = next((c["id"] for c in cands if same(c)), None)
        if alt is None:
            log(f"  planned scene {a:.1f}-{b:.1f}s (cand {i}) is gone from the re-analysis; skipped")
        else:
            log(f"  planned scene {a:.1f}-{b:.1f}s moved from cand {i} to {alt}")
            out.append(alt)
    if not out:
        raise RuntimeError("plan changed since it was scheduled: none of the planned scenes exist any more")
    return out


# ---------------------------------------------------------------- one run per output folder
def _pid_alive(pid: int) -> bool:
    import os
    if os.name == "nt":
        r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True)
        return str(pid) in r.stdout
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _acquire_lock(out_dir: Path) -> None:
    """Two runs writing the same Shorts corrupt the files: refuse if another live run holds the folder."""
    import atexit
    import os
    lock = out_dir / ".lock"
    if lock.exists():
        try:
            pid = int(lock.read_text().strip())
        except ValueError:
            pid = -1
        if pid > 0 and pid != os.getpid() and _pid_alive(pid):
            raise RuntimeError(f"another joshiclip run (PID {pid}) is writing {out_dir}; wait for it to finish")
    lock.write_text(str(os.getpid()))
    atexit.register(lambda: lock.unlink(missing_ok=True))


# ---------------------------------------------------------------- worker process (one Short)
def render_one(cfg: Config, A: dict, c: dict, k: int, s: dict, out_dir: Path, mode: str, cfg_key: str, log=print) -> dict:
    from . import cut, sync, vertical, voice
    from .config import stable_hash
    d = out_dir / f"short_{k:02d}"
    d.mkdir(exist_ok=True)
    from . import pipeline
    c["file"] = pipeline.clip_filename(k, c["title"])
    out = out_dir / c["file"]
    done = d / "result.json"
    if out.exists() and done.exists():
        r = json.loads(done.read_text(encoding="utf-8"))
        if r.get("cand") == c["id"] and r.get("start") == c["start"] and r.get("cfg") == cfg_key:
            log(f"  short_{k:02d} already rendered")
            return r
    t0 = time.perf_counter()
    segs, _ = vertical.segments_for(A, c, cfg, s["off"], s["fps"])
    vc = voice_cfg(cfg, A["vid"], c["start"])
    key = stable_hash({"clip": Path(s["path"]).name, "off": round(s["off"], 5), "segs": [[x.fa, x.fb] for x in segs],
                       "voice": vc, "jc": cfg["jumpcuts"]})
    vj = d / "voice.json"
    vrep_audio = None
    if vj.exists() and (d / "voice.wav").exists():
        meta = json.loads(vj.read_text(encoding="utf-8"))
        if meta.get("key") == key:
            vrep_audio = meta["rep"]
    if vrep_audio is None:
        voice.extract_segments(Path(s["path"]), segs, d / "src.wav", cfg["jumpcuts"].get("fade_ms", 0))
        vrep_audio = voice.process(d / "src.wav", d / "voice.wav", vc, log)
        vj.write_text(json.dumps({"key": key, "rep": vrep_audio}), encoding="utf-8")
    vrep = vertical.render(A, c, out, cfg, mode, log, src=s, voice_wav=d / "voice.wav", voice_rep=vrep_audio)
    vg = vrep.pop("_vgeom", None)
    span = cut.snap(c["start"] - s["off"], c["end"] - s["off"], s["fps"])
    ch = sync.check_render(out, Path(s["path"]), (span.start, span.end), s["fps"], cfg["sync"]["probe_len_s"],
                           cfg["sync"]["max_av_offset_ms"], vg)
    vrep["clip"] = {k2: v for k2, v in s.items() if k2 != "path"}
    r = {"cand": c["id"], "start": c["start"], "cfg": cfg_key, "seconds": round(time.perf_counter() - t0, 1),
         "result": {"checks": ch, "vertical": vrep}}
    done.write_text(json.dumps(r, default=float), encoding="utf-8")
    return r


def voice_cfg(cfg: Config, vid: str, start: float) -> dict:
    """Voice settings + the music bed config + a per-Short seed (same Short -> same track and start point)."""
    return dict(cfg["voice"], music=cfg.get("music"), music_seed=f"{vid}:{start:.3f}")


def _worker(job_path: str) -> int:
    from . import pipeline
    from .config import Config
    job = json.loads(Path(job_path).read_text(encoding="utf-8"))
    cfg = Config(job["cfg"])
    cfg["download"] = dict(cfg["download"], update_first=False)
    cfg["viewers"] = dict(cfg.get("viewers", {}), allow_network=False)   # reuse the parent's comments.json
    A = pipeline.analyze(cfg, job["vid"], False, job["vid"], lambda *a: None, fast=True)
    cands, sel = pipeline.plan(cfg, A, job["max_shorts"])
    c = cands[job["cand"]]
    assert abs(c["start"] - job["start"]) < 1e-6, "plan changed between parent and worker"
    render_one(cfg, A, c, job["k"], job["src"], Path(job["out_dir"]), job["mode"], job["cfg_key"], print)
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(_worker(sys.argv[1]))


# ---------------------------------------------------------------- audio-only update (no picture re-render)
def reaudio(cfg: Config, vid: str, log=print) -> int:
    """Re-process every rendered Short's saved source audio with the CURRENT voice settings and remux it into the
    finished video (video stream copied). Same sample count -> lip-sync unchanged. Takes seconds, not a re-render."""
    import os
    import soundfile as sf
    from . import voice
    out_dir = cfg.path("out") / f"{vid}-fast"
    _acquire_lock(out_dir)
    vc = dict(cfg["voice"], separator=cfg["fast"]["separator"])
    n_done = 0
    for d in sorted(out_dir.glob("short_*")):
        res = d / "result.json"
        if not (d / "src.wav").exists() or not res.exists():
            continue
        r = json.loads(res.read_text(encoding="utf-8"))
        mp4s = sorted(out_dir.glob(f"{d.name[-2:]}_*.mp4"))
        if not mp4s:
            continue
        mp4 = mp4s[0]
        new = d / "voice_new.wav"
        rep = voice.process(d / "src.wav", new, dict(voice_cfg(cfg, vid, r["start"]), separator=vc["separator"]), log)
        if sf.info(str(new)).frames != sf.info(str(d / "src.wav")).frames:
            raise RuntimeError(f"{d.name}: audio length changed - refusing to remux")
        tmp = mp4.with_suffix(".tmp.mp4")
        media.run([media.ff(), "-v", "error", "-y", "-i", str(mp4), "-i", str(new), "-map", "0:v", "-map", "1:a",
                   "-c:v", "copy", "-c:a", "aac", "-b:a", cfg["render"]["final"]["audio_bitrate"], "-ar", "48000",
                   "-movflags", "+faststart", "-fflags", "+bitexact", "-flags:a", "+bitexact", "-shortest", str(tmp)])
        os.replace(tmp, mp4)
        os.replace(new, d / "voice.wav")
        r["result"]["vertical"]["audio"] = rep
        res.write_text(json.dumps(r, default=float), encoding="utf-8")
        n_done += 1
        log(f"  {mp4.name}: new audio ({rep['lufs']} LUFS, TP {rep['true_peak_dbtp']} dBTP), video untouched")
    log(f"  re-audio done: {n_done} Shorts")
    return 0
