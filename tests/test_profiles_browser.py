# End-to-end router-profiles test on a temp DASH_BASE instance (reads the REAL
# routers via a copy of config.json; never touches live :8899).
# Creates a profile for a SECOND router, switches between them with one click
# and checks the display state is never stale across the switch.
# run: <playwright-python> tests/test_profiles_browser.py
import os, subprocess, sys, tempfile, time, json, urllib.request, urllib.error
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
VENV_PY = str(REPO / "venv" / "bin" / "python")
PORT = int(os.environ.get("PROFILES_TEST_PORT", "8897"))
URL = f"http://127.0.0.1:{PORT}"
EXE = os.environ.get("CHROME", "/home/angus/.cache/ms-playwright/chromium-1228/chrome-linux64/chrome")
PW = "test-pass-1234"
sys.path.insert(0, str(Path(__file__).resolve().parent))

fails = []
def check(name, ok, extra=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  {extra}" if extra else ""))
    if not ok: fails.append(name)

def board_of(host, cfg):
    from librouteros import connect
    api = connect(host=host, port=int(cfg.get("port", 8728)),
                  username=cfg["user"], password=cfg["password"], timeout=5)
    try:
        return list(api("/system/resource/print"))[0].get("board-name", "")
    finally:
        api.close()

tmp = Path(tempfile.mkdtemp(prefix="dash-profiles-e2e-"))
(tmp / "static").symlink_to(REPO / "static")
cfg = json.loads((REPO / "config.json").read_text())
# flatten to a pre-profiles config so the migration step is deterministic
cfg = {k: v for k, v in cfg.items() if k not in ("profiles", "active")}
(tmp / "config.json").write_text(json.dumps(cfg))
(tmp / "config.json").chmod(0o600)
HOST_A = cfg["host"]
HOST_B = "192.168.1.1" if HOST_A != "192.168.1.1" else "192.168.1.30"
BOARD_A, BOARD_B = board_of(HOST_A, cfg), board_of(HOST_B, cfg)
assert BOARD_A and BOARD_B and BOARD_A != BOARD_B, "need two distinct routers"

env = dict(os.environ, DASH_BASE=str(tmp))
env.pop("DASH_URL", None)
env["DASH_TEST_PW"] = PW
os.environ["DASH_TEST_PW"] = PW
subprocess.run([VENV_PY, str(REPO / "server.py"), "--passwd-stdin"],
               input=PW + "\n", capture_output=True, text=True, env=env)

srv = subprocess.Popen([VENV_PY, str(REPO / "server.py"), "--port", str(PORT)],
                       env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    for _ in range(40):
        try:
            urllib.request.urlopen(URL + "/api/session", timeout=1); break
        except urllib.error.HTTPError: break
        except Exception: time.sleep(0.4)

    # 1) profiles endpoints are login-gated
    try:
        urllib.request.urlopen(URL + "/api/profiles", timeout=3); code = 200
    except urllib.error.HTTPError as e:
        code = e.code
    check("GET /api/profiles refused without login (401)", code == 401, f"got {code}")

    from playwright.sync_api import sync_playwright
    from _auth_helper import ensure_login
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=EXE, args=["--no-sandbox"])
        pg = b.new_page(viewport={"width": 1400, "height": 900})
        errs = []; pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.on("dialog", lambda d: d.accept())
        pg.goto(URL); ensure_login(pg)
        try:
            pg.wait_for_function("() => window._lastState && window._lastState.connected", timeout=30000)
        except Exception: pass
        check("temp instance connected to the real router (A)",
              pg.evaluate("() => !!(window._lastState||{}).connected"))

        # 2) migration: the flat config auto-became one profile, no passwords served
        prof = pg.evaluate("() => fetch('/api/profiles').then(r => r.json())")
        pg.wait_for_timeout(50)
        names = [x["name"] for x in prof.get("profiles", [])]
        check("flat config migrated into a profile named by its host",
              names == [HOST_A] and prof.get("active") == HOST_A, json.dumps(prof)[:150])
        raw = pg.evaluate("() => fetch('/api/profiles').then(r => r.text())")
        check("profile list never carries passwords", "password" not in raw)

        # 3) add a profile for the SECOND router (separate add-form zone)
        pg.click("#cfgbtn")
        pg.wait_for_selector("#prof-list .p-del", timeout=5000)
        check("saved list shows the migrated profile", HOST_A in pg.text_content("#prof-list"))
        check("add-form is separate from the list and starts blank",
              pg.input_value("#a-host") == "" and pg.input_value("#a-user") == "")
        pg.fill("#a-host", HOST_B); pg.fill("#a-user", cfg["user"]); pg.fill("#a-pass", cfg["password"])
        pg.click("#a-save")
        try:
            pg.wait_for_function(
                "() => window._lastState && window._lastState.connected && "
                "((window._lastState.system||{}).board||'').startsWith('%s')" % BOARD_B[:5], timeout=30000)
        except Exception: pass
        st = pg.evaluate("() => window._lastState")
        check(f"second profile connected and shows {HOST_B}'s board ({BOARD_B})",
              st.get("connected") and (st.get("system") or {}).get("board", "").startswith(BOARD_B[:5]),
              f"board={(st.get('system') or {}).get('board')!r}")

        # 4) one-click switch back to profile A (no password retyped)
        prof = pg.evaluate("() => fetch('/api/profiles').then(r => r.json())")
        check("both routers now saved as profiles",
              sorted(x["name"] for x in prof["profiles"]) == sorted([HOST_A, HOST_B]),
              json.dumps(prof)[:160])
        r = pg.request.fetch(URL + "/api/profiles/use", method="POST",
                             data=json.dumps({"name": HOST_A}),
                             headers={"content-type": "application/json"})
        check("switch API ok", r.status == 200)
        st = pg.evaluate("() => fetch('/api/state').then(r => r.json())")
        check("switch instantly clears the previous router's view",
              not st.get("devices") and not st.get("system"),
              f"devices={len(st.get('devices') or [])}")
        try:
            pg.wait_for_function(
                "() => window._lastState && window._lastState.connected && "
                "((window._lastState.system||{}).board||'').startsWith('%s')" % BOARD_A[:5], timeout=30000)
        except Exception: pass
        st = pg.evaluate("() => window._lastState")
        check(f"switched back: shows {HOST_A}'s board ({BOARD_A}) using the stored password",
              st.get("connected") and (st.get("system") or {}).get("board", "").startswith(BOARD_A[:5]),
              f"board={(st.get('system') or {}).get('board')!r}")
        cfg2 = json.loads((tmp / "config.json").read_text())
        check("config active profile + flat creds follow the switch",
              cfg2.get("active") == HOST_A and cfg2.get("host") == HOST_A
              and cfg2.get("password") == cfg["password"])

        # 5) switching via the dialog UI (Switch button on the inactive row)
        pg.click("#cfgbtn")
        pg.wait_for_function("() => document.querySelectorAll('#prof-list .p-del').length === 2", timeout=5000)
        check("active row has no Switch, inactive row offers Switch",
              pg.query_selector(f'#prof-list .p-use[data-name="{HOST_A}"]') is None
              and pg.is_visible(f'#prof-list .p-use[data-name="{HOST_B}"]'))
        pg.click(f'#prof-list .p-use[data-name="{HOST_B}"]')
        try:
            pg.wait_for_function("() => window._lastState && window._lastState.connected"
                                 " && window._lastState.config.profile === '%s'" % HOST_B, timeout=30000)
        except Exception: pass
        check("dialog Switch button reconnects to the picked profile",
              (pg.evaluate("() => window._lastState") or {}).get("connected"))

        # 6) delete the inactive profile B -> list back to one, nothing else breaks
        r = pg.request.fetch(URL + "/api/profiles/delete", method="POST",
                             data=json.dumps({"name": HOST_B}),
                             headers={"content-type": "application/json"})
        check("delete profile API ok", r.status == 200)
        prof = pg.evaluate("() => fetch('/api/profiles').then(r => r.json())")
        check("only the active profile remains and router stays connected",
              [x["name"] for x in prof["profiles"]] == [HOST_A]
              and (pg.evaluate("() => window._lastState") or {}).get("connected"),
              json.dumps(prof)[:150])
        r = pg.request.fetch(URL + "/api/profiles/use", method="POST",
                             data=json.dumps({"name": "ghost"}),
                             headers={"content-type": "application/json"})
        check("switching to an unknown profile is refused (404)", r.status == 404, f"got {r.status}")

        check("config.json still chmod 600", oct((tmp / "config.json").stat().st_mode & 0o777) == "0o600")
        check("zero pageerror", not errs, "; ".join(errs)[:200])
        b.close()
finally:
    srv.terminate(); srv.wait(timeout=10)

print("ALL PASS" if not fails else f"{len(fails)} FAILURES: {fails}")
sys.exit(0 if not fails else 1)
