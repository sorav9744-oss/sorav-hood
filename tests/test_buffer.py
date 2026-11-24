import datetime as dt
import json

from joshiclip import daily, ledger, pcbatch
from joshiclip.config import Config

UTC = dt.timezone.utc


def _t(delta_h: float) -> str:
    return (dt.datetime.now(UTC) + dt.timedelta(hours=delta_h)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_inventory_counts_only_unpublished_and_dedupes_queue(tmp_path):
    for side, ch in (("a", "channel_a"), ("b", "channel_b")):
        (tmp_path / side / "state").mkdir(parents=True)
    rows = [
        {"vid": "v1", "start": 10.0, "publish_at": _t(5)},                     # scheduled
        {"vid": "v1", "start": 70.0, "publish_at": _t(-5)},                    # already public: not counted
        {"vid": "v2", "start": 5.0, "publish_at": _t(30), "queued": True},     # waiting in the queue ...
        {"vid": "v2", "start": 5.0, "publish_at": _t(31), "from_queue": True},  # ... uploaded later: still ONE Short
        {"vid": "v3", "start": 9.0},                                           # no publish time (published at once)
    ]
    (tmp_path / "a" / "state" / "posted_channel_a.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    inv = pcbatch.inventory({"a": tmp_path / "a", "b": tmp_path / "b"})
    assert inv == {"channel_a": 2, "channel_b": 0}


def test_first_day_skips_to_tomorrow_late_in_the_day():
    ist = dt.timezone(dt.timedelta(minutes=330))
    assert pcbatch.first_day(dt.datetime(2026, 11, 24, 9, 0, tzinfo=ist)) == dt.date(2026, 11, 24)
    assert pcbatch.first_day(dt.datetime(2026, 11, 24, 20, 30, tzinfo=ist)) == dt.date(2026, 11, 25)


def test_queue_uploads_earliest_first_stops_at_the_daily_limit_and_retimes_late_ones(tmp_path, monkeypatch):
    from joshiclip import mediastore, youtube
    metas = {
        "q_channel_a_d3_01_vC": {"publish_at": _t(60), "vid": "vC", "start": 3.0},
        "q_channel_a_d1_01_vA": {"publish_at": _t(-2), "vid": "vA", "start": 1.0},     # slot already passed
        "q_channel_a_d2_01_vB": {"publish_at": _t(30), "vid": "vB", "start": 2.0},
    }
    for k, m in metas.items():
        m.update(channel="channel_a", end=m["start"] + 40, title=k, plan_date="2026-11-25", latest=False, file="x.mp4",
                 yt_title="T " + k, description="d", tags=["a"], category_id="24")
    have = {}
    for k in metas:
        have[k + ".json"] = {"id": len(have) + 1, "state": "uploaded", "size": 1}
        have[k + ".mp4"] = {"id": len(have) + 1, "state": "uploaded", "size": 1}
    monkeypatch.setattr(mediastore, "assets", lambda cfg: have)
    monkeypatch.setattr(mediastore, "_cfg", lambda cfg: {"repo": "o/r"})

    def download(cfg, name, dest, h=None):
        dest.parent.mkdir(parents=True, exist_ok=True)
        if name.endswith(".json"):
            dest.write_text(json.dumps(metas[name[:-5]]), encoding="utf-8")
        else:
            dest.write_bytes(b"x")
        return True
    monkeypatch.setattr(mediastore, "download", download)
    deleted = []
    monkeypatch.setattr(mediastore, "_req", lambda m, url, **kw: deleted.append((m, url.rsplit("/", 1)[-1])))
    uploads = []

    def fake_upload(svc, path, title, desc, tags, cat, when, privacy, log=print):
        if len(uploads) == 2:
            raise youtube.QuotaExceeded("limit")
        uploads.append((title, when))
        return "id%d" % len(uploads)
    monkeypatch.setattr(youtube, "service", lambda prefix: object())
    monkeypatch.setattr(youtube, "channel_name", lambda svc: "chan")
    monkeypatch.setattr(youtube, "upload", fake_upload)
    saved = []
    monkeypatch.setattr(ledger, "record_post", lambda ch, rec: saved.append(rec) or tmp_path / "p.jsonl")
    monkeypatch.setattr(ledger, "git_commit", lambda *a, **k: None)
    cfg = Config({"daily": {"channels": [{"name": "channel_a", "env": "YT_A"}], "media": {"repo": "o/r"}, "privacy": "public"}})
    assert daily.upload_queue(cfg, "channel_a", log=lambda *a: None) == 0
    assert [u[0] for u in uploads] == ["T q_channel_a_d1_01_vA", "T q_channel_a_d2_01_vB"]   # earliest first, 2 then the limit
    assert uploads[0][1] > _t(0)                                    # the passed slot goes out soon, not in the past
    assert uploads[1][1] == metas["q_channel_a_d2_01_vB"]["publish_at"]
    assert [r["vid"] for r in saved] == ["vA", "vB"] and all(r["from_queue"] for r in saved)
    assert len(deleted) == 4                                       # mp4 + json of each uploaded Short; vC stays queued


def test_a_short_plan_is_filled_not_reused(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "STATE", tmp_path / "state")
    monkeypatch.setattr(ledger, "PARTNERS", tmp_path / "state" / "_partners")
    ledger.save_plan({"date": "2026-11-25", "used_titles": [], "max_moments_per_vlog": 20, "vlog_titles": {"v0": "T0"},
                      "channels": {"channel_a": [{"vid": "v0", "cand": 1, "start": 10.0, "end": 50.0, "final": 2.0,
                                                  "title": "kept", "latest": True}]}})
    asked = []

    def fake_moments(cfg, vid, used, log=print):
        asked.append(vid)
        return [{"vid": vid, "cand": i, "start": 100.0 * i, "end": 100.0 * i + 40, "final": 1.5 - i / 10,
                 "title": f"{vid}-{i}"} for i in range(1, 5)]
    monkeypatch.setattr(daily, "moments", fake_moments)
    monkeypatch.setattr(daily, "list_vlogs", lambda cfg, n: [{"vid": "v1", "title": "T1"}, {"vid": "v2", "title": "T2"}])
    monkeypatch.setattr(daily, "prune_cache", lambda *a, **k: [])
    monkeypatch.setattr(ledger, "git_commit", lambda *a, **k: None)
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    cfg = Config({"paths": {"work": str(tmp_path / "w")}, "engine": {"max_shorts": 20, "min_shorts": 1}, "download": {},
                  "daily": {"channels": [{"name": "channel_a", "env": "YT_A"}], "per_channel": 3,
                            "latest_per_channel": [1, 1], "vlog_pool": 5, "utc_offset_min": 330}})
    plan = daily.plan_day(cfg, False, lambda *a: None, date="2026-11-25")
    titles = [x["title"] for x in plan["channels"]["channel_a"]]
    assert len(titles) == 3 and titles[0] == "kept"                  # the Short already planned stays, 2 are added
    saved = json.loads(ledger.plan_path("2026-11-25").read_text(encoding="utf-8"))
    assert len(saved["channels"]["channel_a"]) == 3
    asked.clear()
    daily.plan_day(cfg, False, lambda *a: None, date="2026-11-25")   # full now: reused, nothing analysed
    assert asked == []


def test_auto_fill_starts_only_when_it_is_safe_and_needed(tmp_path, monkeypatch):
    from joshiclip import pcapp
    monkeypatch.setattr(pcapp, "ROOT", tmp_path)
    monkeypatch.setattr(pcapp, "lan_ip", lambda: "192.168.1.5")
    monkeypatch.setattr(pcapp, "_plugged_in", lambda: True)
    monkeypatch.setattr(pcapp, "_other_jobs", lambda: False)
    d = {"auto_fill": True}
    assert pcapp._auto_reason(d) is None                                   # all clear
    monkeypatch.setattr(pcapp, "lan_ip", lambda: "172.20.10.4")
    assert "hotspot" in pcapp._auto_reason(d)                              # mobile data: wait
    assert pcapp._auto_reason({"auto_fill_on_hotspot": True}) is None
    monkeypatch.setattr(pcapp, "lan_ip", lambda: "192.168.1.5")
    monkeypatch.setattr(pcapp, "_plugged_in", lambda: False)
    assert "battery" in pcapp._auto_reason(d)
    monkeypatch.setattr(pcapp, "_plugged_in", lambda: True)
    monkeypatch.setattr(pcapp, "_other_jobs", lambda: True)
    assert "another" in pcapp._auto_reason(d)                              # never next to a manual run
    monkeypatch.setattr(pcapp, "_other_jobs", lambda: False)
    (tmp_path / ".pause_auto").write_text("")
    assert "paused" in pcapp._auto_reason(d)
    (tmp_path / ".pause_auto").unlink()
    assert "switched off" in pcapp._auto_reason({"auto_fill": False})

    inv = {"channel_a": 24, "channel_b": 22}
    monkeypatch.setattr(pcbatch, "inventory", lambda *a, **k: inv)
    cfg = {"buffer_days": 4, "per_channel": 6, "auto_fill_slack": 3}
    assert not pcapp._needs_fill(cfg)                                      # 24 and 22 of 24: within half a day
    inv["channel_b"] = 20
    assert pcapp._needs_fill(cfg)                                          # 4 missing: top up
