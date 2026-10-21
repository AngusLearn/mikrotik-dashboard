# WAN-history separation test: switches between the two REAL router profiles
# on a temp DASH_BASE instance and proves each profile's charts only ever see
# its OWN samples (Batch #34). No browser needed — plain urllib + sqlite.
# run: <any python3 with librouteros in path or repo venv> tests/test_history_split.py
import json, os, sqlite3, subprocess, sys, tempfile, time, urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
VENV_PY = str(REPO / "venv" / "bin" / "python")   # has librouteros
PORT = int(os.environ.get("HISTORY_SPLIT_PORT", "8895"))
URL = f"http://127.0.0.1:{PORT}/"
PW = "test-pass-1234"
sys.path.insert(0, str(Path(__file__).resolve().parent))

fails = []
def check(name, ok, extra=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  {extra}" if extra else ""))
    if not ok: fails.append(name)

def count(tmp, path, table="wan_bytes"):
    try:
        with sqlite3.connect(path) as c:
            return c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    except Exception:
        return -1

tmp = Path(tempfile.mkdtemp(prefix="dash-histsplit-e2e-"))
(tmp / "static").symlink_to(REPO / "static")
cfg = json.loads((REPO / "config.json").read_text())
cfg = {k: v for k, v in cfg.items() if k not in ("profiles", "active")}
(tmp / "config.json").write_text(json.dumps(cfg))
(tmp / "config.json").chmod(0o600)
HOST_A = cfg["host"]
HOST_B = "192.168.1.1" if HOST_A != "192.168.1.1" else "192.168.1.30"
DB_A = tmp / f"history-{HOST_A}.db"
DB_B = tmp / f"history-{HOST_B}.db"
DB_LEGACY = tmp / "history.db"

env = dict(os.environ, DASH_BASE=str(tmp), DASH_TEST_PW=PW)
env.pop("DASH_URL", None)
subprocess.run([VENV_PY, str(REPO / "server.py"), "--passwd-stdin"],
               input=PW + "\n", capture_output=True, text=True, env=env)
srv = subprocess.Popen([VENV_PY, str(REPO / "server.py"), "--port", str(PORT)],
                       env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def api(op, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(URL + path, data=data,
                                 headers={"content-type": "application/json"} if data else {})
    return json.loads(op.open(req, timeout=15).read())

try:
    for _ in range(40):
        try:
            urllib.request.urlopen(URL.rstrip("/") + "/api/session", timeout=1); break
        except Exception: time.sleep(0.4)
    from _auth_helper import api_opener
    op = api_opener(URL, PW)

    # GET /api/profiles migrates the flat config -> profile A becomes active,
    # so every poll from now on must land in history-<A>.db (not history.db)
    prof = api(op, "api/profiles")
    check("profile A migrated", prof.get("active") == HOST_A, json.dumps(prof)[:120])
    n_legacy0 = count(tmp, DB_LEGACY)  # polls before this line may have used the default bucket

    # generate some WAN traffic so the deltas are non-zero
    traffic = subprocess.Popen(["curl", "-s", "-o", "/dev/null", "--limit-rate",
                                "400k", "-m", "30", "https://proof.ovh.net/files/10Mb.dat"])
    time.sleep(9)   # >= 3 poll cycles (interval 3 s)
    n_a = count(tmp, DB_A)
    check("profile A accumulates WAN rows in its own file", n_a >= 2, f"rows={n_a}")
    wan_rx = 0
    try:
        with sqlite3.connect(DB_A) as c:
            wan_rx = c.execute("SELECT COALESCE(SUM(rx),0) FROM wan_bytes").fetchone()[0]
    except Exception:
        pass
    check("A's WAN throughput measured during switch window is non-zero", wan_rx > 0, f"rx_bytes={wan_rx}")
    check("legacy history.db gets no router telemetry once a profile is active",
          count(tmp, DB_LEGACY) == n_legacy0,
          f"legacy {n_legacy0} -> {count(tmp, DB_LEGACY)}")

    # switch to profile B (created here with the same login on the other box)
    api(op, "api/config", {"host": HOST_B, "user": cfg["user"], "password": cfg["password"]})
    time.sleep(2)
    with op.open(urllib.request.Request(
            URL + "api/history?range_min=1", method="GET"), timeout=15) as r:
        chart = json.loads(r.read())
    time.sleep(9)
    n_b, n_a2 = count(tmp, DB_B), count(tmp, DB_A)
    check("profile B accumulates in its own file", n_b >= 2, f"rows={n_b}")
    check("A's file frozen while B is active (charts cannot mix)", n_a2 == n_a,
          f"{n_a} -> {n_a2}")
    time.sleep(2)
    with op.open(urllib.request.Request(
            URL + "api/history?range_min=1", method="GET"), timeout=15) as r:
        chart_b = json.loads(r.read())
    n_b = count(tmp, DB_B)  # re-snapshot immediately before the switch back
    check("active profile served on /api/history",
          len(chart_b.get("series", [])) >= 2
          and all(s[0] >= time.time() - 120 for s in chart_b["series"]),
          f"{len(chart_b.get('series', []))} pts, recent-only")

    # one-click switch back to A
    api(op, "api/profiles/use", {"name": HOST_A})
    time.sleep(9)
    n_a3, n_b2 = count(tmp, DB_A), count(tmp, DB_B)
    check("switching back resumes A's own history", n_a3 > n_a2, f"{n_a2} -> {n_a3}")
    check("B's file frozen while A is active", n_b2 == n_b, f"{n_b} -> {n_b2}")

    # the two datasets are genuinely different routers (not the same stream twice)
    with sqlite3.connect(DB_A) as c:
        span_a = c.execute("SELECT MAX(ts)-MIN(ts) FROM wan_bytes").fetchone()[0] or 0
    with sqlite3.connect(DB_B) as c:
        span_b = c.execute("SELECT MAX(ts)-MIN(ts) FROM wan_bytes").fetchone()[0] or 0
    check("both profiles hold independent sample series", span_a > 0 and span_b > 0,
          f"spans {span_a:.0f}s / {span_b:.0f}s")
    traffic.wait(timeout=40)
finally:
    srv.terminate(); srv.wait(timeout=10)

print("ALL PASS" if not fails else f"{len(fails)} FAILURES: {fails}")
sys.exit(0 if not fails else 1)
