# End-to-end "Forget router / switch router" test on a temp DASH_BASE instance
# (reads the REAL router via a copy of config.json; never touches live :8899).
# run: DASH_TEST_ALLOW_SETUP unset; <playwright-python> tests/test_forget_browser.py
import os, subprocess, sys, tempfile, time, json, urllib.request, urllib.error
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
VENV_PY = str(REPO / "venv" / "bin" / "python")
PORT = int(os.environ.get("FORGET_TEST_PORT", "8896"))
URL = f"http://127.0.0.1:{PORT}"
EXE = os.environ.get("CHROME", "/home/angus/.cache/ms-playwright/chromium-1228/chrome-linux64/chrome")
PW = "test-pass-1234"
sys.path.insert(0, str(Path(__file__).resolve().parent))

fails = []
def check(name, ok, extra=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  {extra}" if extra else ""))
    if not ok: fails.append(name)

tmp = Path(tempfile.mkdtemp(prefix="dash-forget-e2e-"))
(tmp / "static").symlink_to(REPO / "static")
cfg = json.loads((REPO / "config.json").read_text())
# flatten to a pre-profiles config so migration is deterministic
cfg = {k: v for k, v in cfg.items() if k not in ("profiles", "active")}
(tmp / "config.json").write_text(json.dumps(cfg))
(tmp / "config.json").chmod(0o600)
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

    # 1) forget must never work without a login
    req = urllib.request.Request(URL + "/api/config/forget", method="POST")
    try:
        urllib.request.urlopen(req, timeout=3); code = 200
    except urllib.error.HTTPError as e:
        code = e.code
    check("forget without login is refused (401)", code == 401, f"got {code}")

    from playwright.sync_api import sync_playwright
    from _auth_helper import ensure_login
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=EXE, args=["--no-sandbox"])
        pg = b.new_page(viewport={"width": 1400, "height": 900})
        errs = []; pg.on("pageerror", lambda e: errs.append(str(e)))
        dlg_handled = {"n": 0}
        pg.on("dialog", lambda d: (dlg_handled.__setitem__("n", dlg_handled["n"] + 1), d.accept()))
        pg.goto(URL); ensure_login(pg)
        try:
            pg.wait_for_function("() => window._lastState && window._lastState.connected", timeout=30000)
        except Exception: pass
        check("temp instance connected to the real router",
              pg.evaluate("() => !!(window._lastState||{}).connected"))

        # 2) gear opens the dialog: saved list shows the router, form is blank
        pg.click("#cfgbtn")
        pg.wait_for_selector("#prof-list .p-del", timeout=5000)
        row = pg.text_content("#prof-list")
        check("saved list shows current router + user",
              cfg["host"] in row and cfg["user"] in row, row[:100])
        check("active row marked connected", "connected" in row)
        check("add-form starts blank (password never shown)",
              pg.input_value("#a-host") == "" and pg.input_value("#a-pass") == "")

        # 2b) the ✕ close button works, and the dialog reopens
        pg.click("#a-close")
        check("✕ closes the dialog",
              pg.evaluate("() => !document.querySelector('#authdlg').open"))
        pg.click("#cfgbtn")
        pg.wait_for_selector("#prof-list .p-del", timeout=5000)

        # 3) Delete removes the active profile from config.json
        pg.click("#prof-list .p-del")   # confirm() auto-accepted by dialog hook
        pg.wait_for_timeout(200)
        check("confirm() was asked", dlg_handled["n"] >= 1)
        try:
            cfg2 = json.loads((tmp / "config.json").read_text())
        except Exception:
            cfg2 = {}
        check("active credentials removed from config.json",
              not cfg2.get("user") and not cfg2.get("password")
              and not cfg2.get("active"), json.dumps({k: v for k, v in cfg2.items() if k != "password"})[:120])
        pg.wait_for_function("() => !window._lastState.connected", timeout=15000)
        st = pg.evaluate("() => window._lastState")
        check("/api/state stays 200 and reports not-configured",
              "credentials" in (st.get("last_error") or ""), str(st.get("last_error")))
        check("banner offers Enter credentials",
              pg.is_visible("#banner #openauth") or pg.evaluate(
                  "() => document.querySelector('#banner').textContent.includes('login')"))

        # 4) reopening after delete: empty list + empty form
        pg.click("#banner #openauth")
        pg.wait_for_function("() => document.querySelector('#prof-list').textContent.includes('No saved routers')", timeout=5000)
        check("after delete the saved list is empty",
              pg.query_selector("#prof-list .p-del") is None)
        vals = pg.evaluate("() => [document.querySelector('#a-host').value,"
                           " document.querySelector('#a-user').value]")
        check("after delete the form is empty", vals[0] == "" and vals[1] == "", str(vals))

        # 5) full switch-back: re-enter credentials -> reconnects (the actual use case)
        pg.fill("#a-host", cfg["host"]); pg.fill("#a-user", cfg["user"]); pg.fill("#a-pass", cfg["password"])
        pg.click("#a-save")
        try:
            pg.wait_for_function("() => window._lastState && window._lastState.connected", timeout=30000)
        except Exception: pass
        check("re-entered credentials reconnect to the new router",
              pg.evaluate("() => !!(window._lastState||{}).connected"))
        check("config.json written back chmod 600",
              oct((tmp / "config.json").stat().st_mode & 0o777) == "0o600")
        check("zero pageerror", not errs, "; ".join(errs)[:200])
        b.close()
finally:
    srv.terminate(); srv.wait(timeout=10)

print("ALL PASS" if not fails else f"{len(fails)} FAILURES: {fails}")
sys.exit(0 if not fails else 1)
