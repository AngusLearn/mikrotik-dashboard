# End-to-end auth test: temp DASH_BASE + own port; never touches the live :8899 instance.
# run: <playwright-python> tests/test_auth_browser.py
import os, subprocess, sys, tempfile, time, json, urllib.request, urllib.error
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
VENV_PY = str(REPO / "venv" / "bin" / "python")
PORT = int(os.environ.get("AUTH_TEST_PORT", "8898"))
URL = f"http://127.0.0.1:{PORT}"
EXE = os.environ.get("CHROME", "/home/angus/.cache/ms-playwright/chromium_headless_shell-1228/chrome-headless-shell-linux64/chrome-headless-shell")
PW = "test-pass-1234"

fails = []
def check(name, ok, extra=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  {extra}" if extra else ""))
    if not ok: fails.append(name)

tmp = tempfile.mkdtemp(prefix="dash-auth-e2e-")
(tmp_path := Path(tmp))
(tmp_path / "static").symlink_to(REPO / "static")  # full static incl. vendor/
env = dict(os.environ, DASH_BASE=tmp)

# ---- CLI set password BEFORE the server starts
r = subprocess.run([VENV_PY, str(REPO / "server.py"), "--passwd-stdin"],
                   input=PW + "\n", capture_output=True, text=True, env=env)
check("CLI --passwd-stdin writes auth.json", (tmp_path / "auth.json").exists(), r.stdout.strip() or r.stderr)
check("auth.json is chmod 600", oct((tmp_path / "auth.json").stat().st_mode & 0o777) == "0o600")
raw = (tmp_path / "auth.json").read_text()
check("no plaintext in auth.json", PW not in raw)

srv = subprocess.Popen([VENV_PY, str(REPO / "server.py"), "--port", str(PORT)],
                       env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    for _ in range(40):
        try:
            urllib.request.urlopen(URL + "/api/session", timeout=1); break
        except urllib.error.HTTPError: break   # 401 still means "server is up"
        except Exception: time.sleep(0.4)

    def get(path):
        try:
            resp = urllib.request.urlopen(URL + path, timeout=3)
            return resp.status, resp.geturl()
        except urllib.error.HTTPError as e:
            return e.code, URL + path

    code, where = get("/")
    check("authed? no -> / redirects to /login", where.rstrip("/").endswith("/login"), f"{code} {where}")
    try:
        body = json.load(urllib.request.urlopen(URL + "/api/state", timeout=3))
        leak = True
    except urllib.error.HTTPError as e:
        leak, code401 = False, e.code
    check("/api/state blocked with 401 (no cookie)", (not leak) and code401 == 401)

    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=EXE)
        pg = b.new_page(viewport={"width": 1400, "height": 900})
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.goto(URL + "/", wait_until="networkidle")
        check("browser sent to login page", "Login ·" in pg.title() or pg.locator("#pw").count() == 1, pg.title())

        pg.fill("#pw", "totally-wrong")
        pg.click("#go")
        pg.wait_for_timeout(700)
        check("wrong password shows error, stays on login",
              pg.locator("#pw").count() == 1 and "wrong" in pg.inner_text("#err").lower(),
              repr(pg.inner_text("#err")))
        check("/api/state still 401 after failed login",
              pg.evaluate("fetch('/api/state').then(r=>r.status)") == 401)

        pg.fill("#pw", PW)
        pg.click("#go")
        pg.wait_for_timeout(1500)
        check("correct password reaches main dashboard", pg.locator("#conn-dot").count() == 1)
        check("api works with session cookie",
              pg.evaluate("fetch('/api/state').then(r=>r.status)") == 200)
        check("logout button visible when auth required",
              pg.evaluate("!document.getElementById('logout').hidden"))

        pg.click("#logout")
        pg.wait_for_timeout(1000)
        check("logout returns to login page", pg.locator("#pw").count() == 1, pg.url)
        check("session cookie dead after logout",
              pg.evaluate("fetch('/api/state').then(r=>r.status)") == 401)

        # wrong passwords now trip the counter (below lockout limit)
        for i in range(3):
            pg.fill("#pw", "x" * 8); pg.click("#go"); pg.wait_for_timeout(450)
        check("repeat wrong logins stay on login page", pg.locator("#pw").count() == 1)
        check("zero JS errors", not errs, errs[:3])
        b.close()

    # ---- CLI reset while server running (the 'forgot password' path)
    r = subprocess.run([VENV_PY, str(REPO / "server.py"), "--passwd-stdin"],
                       input="reset-pass-9\n", capture_output=True, text=True, env=env)
    check("CLI reset works while server is running", "password set" in r.stdout, r.stdout.strip())
    check("old password dead after reset",
          subprocess.run(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                          "-X", "POST", "-H", "content-type: application/json",
                          "-d", json.dumps({"password": PW}), URL + "/api/login"],
                         capture_output=True, text=True).stdout == "401")
    check("new password accepted after reset",
          subprocess.run(["curl", "-s", "-X", "POST", "-H", "content-type: application/json",
                          "-d", json.dumps({"password": "reset-pass-9"}), URL + "/api/login"],
                         capture_output=True, text=True).stdout.find('"ok":true') >= 0)

    # ---- CLI --no-passwd removes the account -> setup wizard (never open)
    r = subprocess.run([VENV_PY, str(REPO / "server.py"), "--no-passwd"],
                       capture_output=True, text=True, env=env)
    check("--no-passwd removes auth.json", not (tmp_path / "auth.json").exists(), r.stdout.strip())
    code, where = get("/")
    check("dashboard falls back to SETUP page, never open",
          where.rstrip("/").endswith("/setup"), f"{code} {where}")
    login_code = subprocess.run(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                                 "-X", "POST", "-H", "content-type: application/json",
                                 "-d", json.dumps({"password": "whatever-123"}),
                                 URL + "/api/login"], capture_output=True, text=True).stdout
    check("/api/login refused in setup mode", login_code in ("401", "409"),
          f"got {login_code} (401 = middleware gate refuses it before the route)")

    # ---- first-boot wizard in the browser: create account -> straight in
    from playwright.sync_api import sync_playwright as sp2
    with sp2() as p2:
        b2 = p2.chromium.launch(executable_path=EXE)
        pg2 = b2.new_page(viewport={"width": 1200, "height": 800})
        pg2.goto(URL + "/", wait_until="networkidle")
        check("setup wizard shown to a new visitor", pg2.locator("#pw2").count() == 1, pg2.url)
        pg2.fill("#pw", "brand-new-pw-1"); pg2.fill("#pw2", "typo-mismatch")
        pg2.click("#go"); pg2.wait_for_timeout(500)
        check("mismatch rejected on wizard", "match" in pg2.inner_text("#err").lower()
              and pg2.locator("#pw2").count() == 1, repr(pg2.inner_text("#err")))
        pg2.fill("#pw", "brand-new-pw-1"); pg2.fill("#pw2", "brand-new-pw-1")
        pg2.click("#go"); pg2.wait_for_timeout(1500)
        check("wizard creates account and logs straight in",
              pg2.locator("#conn-dot").count() == 1, pg2.url)
        check("wizard is one-shot: /api/setup now 409",
              pg2.evaluate("fetch('/api/setup',{method:'POST',headers:{'content-type':'application/json'},"
                           "body:'{\"password\":\"x\",\"confirm\":\"x\"}'}).then(r=>r.status)") == 409)
        check("login works with the wizard-created password",
              subprocess.run(["curl", "-s", "-X", "POST", "-H", "content-type: application/json",
                              "-d", json.dumps({"password": "brand-new-pw-1"}),
                              URL + "/api/login"], capture_output=True, text=True).stdout.find('"ok":true') >= 0)
        b2.close()
finally:
    srv.terminate(); srv.wait(timeout=10)

print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}"))
sys.exit(1 if fails else 0)
