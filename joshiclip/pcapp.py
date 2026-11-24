"""The Upload page: http://localhost:8790 on the PC, or from the iPhone on the same Wi-Fi with the private link shown
in the page. It also fills the buffer BY ITSELF: it starts at Windows login (Startup shortcut), waits 2 minutes for
the network, then every daily.auto_fill_check_min minutes keeps `buffer_days` days of Shorts scheduled ahead
(joshiclip.pcbatch). It does nothing while the laptop is on battery or on an iPhone hotspot, while another Shorts job
is running, or while a file named .pause_auto exists in the project folder. A click on the page does the same, now.

  pythonw -m joshiclip.pcapp            what the Windows Startup shortcut runs (add --open to also open the browser)
"""
from __future__ import annotations

import collections
import datetime as dt
import html
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
PORT = 8790          # 8765 is used by another program on this PC (ClipPilot)
LOGF = ROOT / "out" / "pcapp.log"
STATE = {"running": False, "started": None, "days": 0, "rc": None, "lines": collections.deque(maxlen=400)}
LOCK = threading.Lock()


def key() -> str:
    """Private key for the iPhone link (kept in .env; never needed on the PC itself)."""
    env = ROOT / ".env"
    txt = env.read_text(encoding="utf-8") if env.exists() else ""
    m = re.search(r"^PCAPP_KEY=(\S+)", txt, re.M)
    if m:
        return m.group(1)
    k = secrets.token_urlsafe(12)
    with open(env, "a", encoding="utf-8") as f:
        f.write(f"\nPCAPP_KEY={k}\n")
    return k


def lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "this-pc"


BELOW_NORMAL = 0x00004000          # Windows priority class: the laptop stays responsive while Shorts are made
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def start(days: int, auto: bool = False) -> bool:
    with LOCK:
        if STATE["running"]:
            return False
        STATE.update(running=True, started=dt.datetime.now().strftime("%d %b %H:%M"), days=days, rc=None,
                     by="automatic" if auto else "your click")
        STATE["lines"].clear()
    threading.Thread(target=_work, args=(days,), daemon=True).start()
    return True


def _work(days: int) -> None:
    LOGF.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, PYTHONUTF8="1", PYTHONUNBUFFERED="1")
    noise = re.compile(r"XNNPACK|InitGoogle|inference_feedback|^W0000|^I0000|\[run\]|\[done\]|\[cache\]")
    with open(LOGF, "a", encoding="utf-8") as lf:
        lf.write(f"\n===== {dt.datetime.now():%Y-%m-%d %H:%M} upload {days} day(s)\n")
        p = subprocess.Popen([sys.executable.replace("pythonw", "python"), "-m", "joshiclip.pcbatch", "run", "--days",
                              str(days)], cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW | BELOW_NORMAL)
        for line in p.stdout:
            line = line.rstrip()
            lf.write(line + "\n"); lf.flush()
            if line and not noise.search(line):
                STATE["lines"].append(line)
        rc = p.wait()
    STATE.update(running=False, rc=rc, finished=time.time())


# ---------------------------------------------------------------- automatic fill
def _cfg() -> dict:
    import yaml
    return yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8")).get("daily", {})


def _sys_power() -> tuple[int, int]:
    """(AC line status, battery percent): AC 0 = on battery, 1 = plugged in, 255 = unknown; percent 255 = unknown."""
    if os.name != "nt":
        return 1, 100
    import ctypes

    class Power(ctypes.Structure):
        _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                    ("BatteryLifePercent", ctypes.c_ubyte), ("SystemStatusFlag", ctypes.c_ubyte),
                    ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong)]
    s = Power()
    ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(s))
    return s.ACLineStatus, s.BatteryLifePercent


def _plugged_in() -> bool:
    return _sys_power()[0] != 0          # unknown (255) counts as plugged in


def _battery_pct() -> int:
    p = _sys_power()[1]
    return 100 if p == 255 else p


def _other_jobs() -> bool:
    """Another Shorts job (a manual run, a render worker) is running: never start a second one next to it."""
    if os.name != "nt":
        return False
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command",
                            "Get-CimInstance Win32_Process -Filter \"Name='python.exe' OR Name='pythonw.exe'\" | "
                            "ForEach-Object { $_.CommandLine }"], capture_output=True, text=True, timeout=40,
                           creationflags=NO_WINDOW)
    except Exception:  # noqa: BLE001
        return False
    for line in r.stdout.splitlines():
        low = line.lower()
        if ("joshiclip" in low or "run_today" in low or "topup_" in low) and "pcapp" not in low:
            return True
    return False


def _auto_reason(d: dict) -> str | None:
    """Why the automatic fill must not start right now (None = go)."""
    if (ROOT / ".pause_auto").exists():
        return "paused (the .pause_auto file exists)"
    if not d.get("auto_fill", True):
        return "switched off (config daily.auto_fill)"
    if lan_ip().startswith("172.20.10.") and not d.get("auto_fill_on_hotspot", False):
        return "on an iPhone hotspot (mobile data); waiting for Wi-Fi"
    if not _plugged_in():
        if not d.get("auto_fill_on_battery", False):
            return "laptop is on battery; plug it in"
        floor = int(d.get("auto_fill_battery_min_pct", 20))
        if _battery_pct() < floor:         # a run started on an almost empty battery would just be cut off
            return f"battery is at {_battery_pct()}% (runs on battery only above {floor}%); plug it in"
    if _other_jobs():
        return "another Shorts job is running"
    return None


def _needs_fill(d: dict) -> bool:
    from .pcbatch import inventory
    target = int(d.get("buffer_days", 4)) * int(d.get("per_channel", 6))
    slack = int(d.get("auto_fill_slack", 3))            # half a day missing is worth a run
    return any(n < target - slack for n in inventory().values())


def auto_loop() -> None:
    time.sleep(120)                                      # the network needs a moment after Windows login
    while True:
        wait = 60
        try:
            d = _cfg()
            wait = max(10, int(d.get("auto_fill_check_min", 60)))
            nxt = f"next check {dt.datetime.now() + dt.timedelta(minutes=wait):%H:%M}"
            if STATE["running"]:
                STATE["auto"] = "working"
            elif STATE["rc"] not in (None, 0) and time.time() - STATE.get("finished", 0) < 3 * 3600:
                STATE["auto"] = "the last run was incomplete; trying again later"
            elif _auto_reason(d):
                STATE["auto"] = f"waiting: {_auto_reason(d)}; {nxt}"
            elif not _needs_fill(d):
                STATE["auto"] = f"the buffer is full; {nxt}"
            else:
                STATE["auto"] = "filling the buffer"
                start(int(d.get("buffer_days", 4)), auto=True)
        except Exception as e:  # noqa: BLE001 - never let the loop die
            STATE["auto"] = f"error ({type(e).__name__}: {str(e)[:80]})"
        time.sleep(wait * 60)


def status() -> dict:
    lines = list(STATE["lines"])
    ups = [l for l in lines if "uploaded https://youtu.be/" in l or "queued for GitHub" in l]
    try:
        from .pcbatch import inventory
        inv = inventory()
    except Exception:  # noqa: BLE001 - no checkout yet (first run) or a record being written
        inv = {}
    try:
        days = int(_cfg().get("buffer_days", 4))
    except Exception:  # noqa: BLE001
        days = 4
    return {"running": STATE["running"], "started": STATE["started"], "days": STATE["days"], "rc": STATE["rc"],
            "uploads": ups[-40:], "tail": lines[-14:], "inventory": inv, "auto": STATE.get("auto"),
            "by": STATE.get("by"), "buffer_days": days}


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Shorts uploader</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--fg:#1d2330;--mut:#667085;--acc:#e5322d;--ok:#12805c;--line:#e3e6eb}
@media (prefers-color-scheme:dark){:root{--bg:#121417;--card:#1b1e23;--fg:#e8eaee;--mut:#9aa3b2;--line:#2b3038}}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,Segoe UI,Roboto,sans-serif}
main{max-width:720px;margin:0 auto;padding:18px 16px 40px}
h1{font-size:22px;margin:4px 0 2px} p.sub{color:var(--mut);margin:0 0 16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px;margin-bottom:14px}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
button{font:600 16px system-ui;border:0;border-radius:12px;padding:14px 18px;cursor:pointer;background:var(--acc);color:#fff;flex:1;min-width:140px}
button.alt{background:transparent;color:var(--fg);border:1px solid var(--line)}
button:disabled{opacity:.45;cursor:default}
.pill{display:inline-block;padding:3px 10px;border-radius:99px;font-size:13px;background:var(--line)}
.run{background:#fff3cd;color:#7a5800}.done{background:#d8f3e6;color:var(--ok)}
ul{padding-left:18px;margin:6px 0} li{margin:3px 0;word-break:break-word} a{color:#2563eb}
pre{white-space:pre-wrap;word-break:break-word;font:12px/1.4 ui-monospace,Consolas,monospace;color:var(--mut);margin:0}
small{color:var(--mut)}
</style></head><body><main>
<h1>Shorts uploader</h1><p class="sub">Sourav Joshi gang + Sourav jyoshi hood + Facebook PM FF</p>
<div class="card"><div class="row" style="justify-content:space-between"><b>Status</b><span id="st" class="pill">...</span></div>
<p id="auto" style="margin:8px 0 0"><small>Automatic: starting...</small></p>
<p id="info" style="margin:8px 0 0"><small>Keeps a buffer of Shorts scheduled ahead on both channels (6 a day each, 2 from
the newest vlog + 4 from old vlogs, at 7:00, 7:30, 8:30, 9:00, 18:00, 19:00 IST, YouTube + Facebook). Shorts that already
went public leave the buffer, so a click only makes the missing ones.</small></p></div>
<div class="card"><b>Scheduled ahead</b>
<p id="inv" style="margin:6px 0 10px;font-size:18px">...</p>
<div class="row"><label for="days"><small>Keep</small></label>
<select id="days" style="font:16px system-ui;padding:10px;border-radius:10px;border:1px solid var(--line);background:var(--card);color:var(--fg)">
<option>2</option><option>3</option><option selected>4</option><option>5</option><option>7</option><option>10</option></select>
<label for="days"><small>days ahead</small></label>
<button id="fill" onclick="go(+document.getElementById('days').value)">Fill the buffer</button></div>
<p style="margin:10px 0 0"><small>First fill about 3-4 hours (analysing + making the Shorts + uploading), later ones only the
missing days. Keep the PC on until it says done. Extra Shorts YouTube's daily limit holds back are uploaded by GitHub.</small></p></div>
<div class="card"><b>Uploaded / scheduled</b><ul id="ups"><li><small>nothing yet</small></li></ul></div>
<div class="card"><b>Progress</b><pre id="tail"></pre></div>
<p><small>iPhone (same Wi-Fi, PC on): <span id="link">__LINK__</span></small></p>
</main><script>
const K=new URLSearchParams(location.search).get('k')||'';
async function go(n){if(!confirm('Keep '+n+' days of Shorts scheduled ahead? Only the missing ones are made.'))return;
 const r=await fetch('/start?days='+n+'&k='+K,{method:'POST'});alert((await r.json()).msg);tick();}
function esc(s){return s.replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))}
async function tick(){try{const s=await (await fetch('/status?k='+K)).json();
 const st=document.getElementById('st');
 if(s.running){st.textContent='working since '+s.started+' ('+s.days+' day'+(s.days>1?'s':'')+')';st.className='pill run'}
 else if(s.rc===null){st.textContent='ready';st.className='pill'}
 else{st.textContent=s.rc===0?'done':'done with problems (see progress)';st.className='pill done'}
 document.querySelectorAll('button').forEach(b=>b.disabled=s.running);
 document.getElementById('auto').innerHTML='<small>Automatic: '+esc(s.auto||'starting...')+(s.running&&s.by?' (started by '+s.by+')':'')+'</small>';
 if(!window._d&&s.buffer_days){document.getElementById('days').value=s.buffer_days;window._d=1}
 const iv=Object.entries(s.inventory||{}).map(([c,n])=>(c=='channel_a'?'Sourav Joshi gang':'Sourav jyoshi hood')+': <b>'+n+'</b>');
 document.getElementById('inv').innerHTML=iv.length?iv.join(' &nbsp;·&nbsp; ')+' &nbsp;<small>(6 a day each)</small>':'...';
 document.getElementById('ups').innerHTML=s.uploads.length?s.uploads.map(l=>'<li>'+esc(l).replace(/(https:\\/\\/youtu\\.be\\/[\\w-]+)/,'<a href="$1" target="_blank">$1</a>')+'</li>').join(''):'<li><small>nothing yet</small></li>';
 document.getElementById('tail').textContent=s.tail.join('\\n');}catch(e){}}
tick();setInterval(tick,5000);
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    def _ok(self) -> bool:
        local = self.client_address[0] in ("127.0.0.1", "::1")
        q = parse_qs(urlparse(self.path).query)
        return local or q.get("k", [""])[0] == key()

    def _send(self, code: int, body: str, ctype: str = "application/json") -> None:
        b = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):  # noqa: N802
        if not self._ok():
            return self._send(403, json.dumps({"msg": "wrong link"}))
        u = urlparse(self.path)
        if u.path == "/status":
            return self._send(200, json.dumps(status()))
        link = f"http://{lan_ip()}:{PORT}/?k={key()}"
        return self._send(200, PAGE.replace("__LINK__", html.escape(link)), "text/html")

    def do_POST(self):  # noqa: N802
        if not self._ok():
            return self._send(403, json.dumps({"msg": "wrong link"}))
        u = urlparse(self.path)
        if u.path == "/start":
            days = max(1, min(10, int(parse_qs(u.query).get("days", ["4"])[0])))
            ok = start(days)
            return self._send(200, json.dumps({"msg": f"Started: {days} day(s). Keep the PC on until it says done."
                                               if ok else "Already working - wait until it says done."}))
        return self._send(404, json.dumps({"msg": "?"}))

    def log_message(self, *a):  # quiet
        pass


def main() -> None:
    url = f"http://localhost:{PORT}/"
    try:
        srv = ThreadingHTTPServer(("0.0.0.0", PORT), H)
    except OSError:                       # already running (started at login): just show it
        if "--open" in sys.argv:
            webbrowser.open(url)
        return
    key()
    threading.Thread(target=auto_loop, daemon=True).start()
    if "--open" in sys.argv:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    srv.serve_forever()


if __name__ == "__main__":
    main()
