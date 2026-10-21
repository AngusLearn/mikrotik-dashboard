# Unit tests for the local web auth layer (no router, no server needed):
#   venv/bin/python tests/test_auth_unit.py
import json
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ["DASH_BASE"] = tempfile.mkdtemp(prefix="dash-auth-test-")
(Path(os.environ["DASH_BASE"]) / "static").mkdir()  # app mounts /static at import
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server  # noqa: E402

server.db_init()   # normally done by the startup event

fails = []

def check(name, ok, extra=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  {extra}" if extra else ""))
    if not ok:
        fails.append(name)

# ---- hashing round-trip + salt uniqueness
salt, digest = server.hash_password("hunter2-test")
_, digest2 = server.hash_password("hunter2-test", salt)
_, digest3 = server.hash_password("wrong-pass", salt)
check("pbkdf2 deterministic with same salt", digest == digest2)
check("different password -> different digest", digest != digest3)
salt_b, _ = server.hash_password("hunter2-test")
check("fresh salt per set", salt_b != salt)

# ---- setup mode until a password exists (dashboard is never open)
check("setup mode when no auth.json", server.setup_mode() and not server.auth_enabled())
check("verify fails with no password set", server.verify_password("x") is False)

# ---- set / verify / file perms
server.set_password_file("sesame-open-sesame")
check("auth enabled after set", server.auth_enabled())
check("correct password verifies", server.verify_password("sesame-open-sesame"))
check("wrong password rejected", not server.verify_password("nope"))
st = server.AUTH_PATH.stat()
check("auth.json chmod 600", oct(st.st_mode & 0o777) == "0o600", oct(st.st_mode & 0o777))
raw = json.loads(server.AUTH_PATH.read_text())
check("plaintext password never stored", "sesame-open-sesame" not in server.AUTH_PATH.read_text())
check("salt+hash+algo stored", {"salt", "hash", "algo"} <= set(raw))
try:
    server.set_password_file("short")
    check("set_password_file guards short passwords too", False, "no exception")
except ValueError:
    check("set_password_file guards short passwords too", True)

# ---- corrupt auth.json must fail CLOSED (gate stays up, nothing verifies)
server.AUTH_PATH.write_text("{ this is not json")
check("corrupt auth.json keeps gate enabled", server.auth_enabled())
check("corrupt auth.json accepts no password", not server.verify_password("sesame-open-sesame"))

# ---- set_password_file revokes sessions and fail counters (now DB-backed)
sid = server.session_issue()
check("fresh session valid", server.session_valid(sid))
with server._db_lock, server.db() as c:
    c.execute("INSERT OR REPLACE INTO login_fail VALUES('1.2.3.4',5,0)")
server.set_password_file("rotated-pass-2")
check("sessions revoked on password change", not server.session_valid(sid))
check("fail counters cleared on password change", server.check_login_lock("1.2.3.4") == 0)
with server._db_lock, server.db() as c:
    n = c.execute("SELECT COUNT(*) FROM login_fail WHERE ip='1.2.3.4'").fetchone()[0]
check("fail row actually deleted", n == 0)

# ---- session lifecycle (SQLite = survives a process restart)
sid2 = server.session_issue()
check("valid session accepted", server.session_valid(sid2))
with server._db_lock, server.db() as c:
    exp = c.execute("SELECT exp FROM sessions WHERE sid=?", (sid2,)).fetchone()[0]
    check("sliding expiry refreshed", exp > time.time() + 3600)
    # simulate expiry
    c.execute("UPDATE sessions SET exp=? WHERE sid=?", (time.time() - 1, sid2))
check("expired session rejected", not server.session_valid(sid2))
with server._db_lock, server.db() as c:
    n = c.execute("SELECT COUNT(*) FROM sessions WHERE sid=?", (sid2,)).fetchone()[0]
check("expired session purged", n == 0)
check("unknown sid rejected", not server.session_valid("nope"))

# ---- lockout counter
ip = "9.9.9.9"
for i in range(server.LOGIN_FAIL_LIMIT - 1):
    server.note_login_fail(ip)
check("below limit not locked", server.check_login_lock(ip) == 0)
server.note_login_fail(ip)
lock = server.check_login_lock(ip)
check("locked after limit", lock > server.LOGIN_FAIL_COOLDOWN - 5, f"{lock}s")
server.note_login_ok(ip)
check("success clears the record", server.check_login_lock(ip) == 0)

print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}"))
sys.exit(1 if fails else 0)
