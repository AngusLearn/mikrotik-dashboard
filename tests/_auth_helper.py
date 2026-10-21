# Shared login helper for the browser suites (dashboard is always gated now).
# First run against a fresh instance completes the setup wizard with TEST_PW;
# against a dashboard whose owner set their own password, export DASH_TEST_PW.
import os

TEST_PW = os.environ.get("DASH_TEST_PW", "pytest-dash-12345")
# An instance in first-boot SETUP mode belongs to its OWNER — a test suite
# silently creating the account there locks the human out (this actually
# happened; see CHANGELOG Batch #22). Tests refuse unless explicitly opted in:
#   export DASH_TEST_ALLOW_SETUP=1
ALLOW_SETUP = os.environ.get("DASH_TEST_ALLOW_SETUP") == "1"
_SETUP_REFUSAL = ("instance is in first-boot SETUP mode — the tests refuse to "
                  "claim the owner's account. Complete the wizard yourself, or "
                  "export DASH_TEST_ALLOW_SETUP=1 if this is a disposable instance")


def api_opener(base_url, pw=None):
    """urllib opener that transparently passes the gate (login; setup only with
    explicit opt-in), with cookies, for tests that fetch /api/state first."""
    import json, urllib.request, urllib.error, http.cookiejar
    pw = pw or TEST_PW
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    try:
        op.open(base_url + "api/state", timeout=15)
        return op
    except urllib.error.HTTPError as e:
        if e.code != 401:
            raise
    def post(path, payload):
        req = urllib.request.Request(base_url + path,
                                     data=json.dumps(payload).encode(),
                                     headers={"content-type": "application/json"})
        try:
            op.open(req, timeout=15)
            return True
        except urllib.error.HTTPError as e2:
            if e2.code in (401, 409):
                return False
            raise
    if not post("api/login", {"password": pw}):
        # login failed or account absent: is this setup mode?
        try:
            setup_pending = "Setup ·" in op.open(base_url + "setup", timeout=10).read().decode()[:200]
        except Exception:
            setup_pending = False
        if setup_pending:
            if not ALLOW_SETUP:
                raise RuntimeError(_SETUP_REFUSAL)
            if not post("api/setup", {"password": pw, "confirm": pw}):
                raise RuntimeError("setup endpoint rejected the test password")
        else:
            raise RuntimeError("dashboard refused the test password — "
                               "export DASH_TEST_PW=<the dashboard password>")
    return op


def ensure_login(pg, pw=None):
    pw = pw or TEST_PW
    for _ in range(3):
        if pg.locator("#pw2").count():            # setup wizard
            if not ALLOW_SETUP:
                raise RuntimeError(_SETUP_REFUSAL)
            pg.fill("#pw", pw)
            pg.fill("#pw2", pw)
            pg.click("#go")
        elif pg.locator("#pw").count():           # login page
            pg.fill("#pw", pw)
            pg.click("#go")
        else:
            return True                           # already authenticated
        try:
            pg.wait_for_selector("#conn-dot", timeout=8000)
            return True
        except Exception:
            continue
    raise RuntimeError("could not reach dashboard — wrong password? "
                       "set DASH_TEST_PW to the dashboard login")
