# Unit tests for db_prune retention + clock-skew guard (no router, no server):
#   venv/bin/python tests/test_prune_unit.py
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ["DASH_BASE"] = tempfile.mkdtemp(prefix="dash-prune-test-")
(Path(os.environ["DASH_BASE"]) / "static").mkdir()  # app mounts /static at import
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server  # noqa: E402

server.db_init()

fails = []

def check(name, ok, extra=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  {extra}" if extra else ""))
    if not ok:
        fails.append(name)

now = time.time()
RETENTION_H = 1.0

with server._db_lock, server.db() as c:
    # samples: fresh, old (past retention), future burst (RTC-ahead-at-boot),
    # and slightly-future inside the 300 s margin (must survive).
    c.executemany(
        "INSERT OR REPLACE INTO samples VALUES (?,?,?,?)",
        [(now, "fresh", 1.0, 1.0),
         (now - 7200, "old", 1.0, 1.0),
         (now + 18455, "future-burst", 1.0, 1.0),
         (now + 120, "margin", 1.0, 1.0)],
    )
    c.executemany(
        "INSERT OR REPLACE INTO wan_bytes VALUES (?,?,?)",
        [(now, 100, 100),
         (now - 7200, 100, 100),
         (now + 18455, 100, 100)],
    )
    # an expired + a live session
    c.executemany(
        "INSERT OR REPLACE INTO sessions VALUES (?,?)",
        [("gone", now - 10), ("keep", now + 3600)],
    )

server.db_prune(RETENTION_H)

with server._db_lock, server.db() as c:
    keys = {r[0] for r in c.execute("SELECT key FROM samples")}
    check("fresh sample kept", "fresh" in keys)
    check("sample within 300 s margin kept", "margin" in keys)
    check("past-retention sample pruned", "old" not in keys)
    check("future-dated sample pruned", "future-burst" not in keys)
    nb = c.execute("SELECT COUNT(*) FROM wan_bytes").fetchone()[0]
    check("wan_bytes pruned on both ends", nb == 1, f"rows={nb}")
    sess = {r[0] for r in c.execute("SELECT sid FROM sessions")}
    check("expired session pruned, live kept", sess == {"keep"}, str(sess))

# the guard must also fix the exact production shape: a burst stamped ~5 h
# ahead (boot-time RTC skew) disappears on the next prune pass.
with server._db_lock, server.db() as c:
    c.executemany(
        "INSERT OR REPLACE INTO samples VALUES (?,?,?,?)",
        [(now + 18460 + i, f"k{i}", 5.0, 5.0) for i in range(9)],
    )
server.db_prune(RETENTION_H)
with server._db_lock, server.db() as c:
    n = c.execute("SELECT COUNT(*) FROM samples WHERE ts > ?", (now + 300,)).fetchone()[0]
    check("RTC-skew burst self-heals via prune", n == 0, f"future rows={n}")

print("ALL PASS" if not fails else f"{len(fails)} FAILURES: {fails}")
sys.exit(0 if not fails else 1)
