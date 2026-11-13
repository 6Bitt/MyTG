"""
Entry point for scheduled runs (GitHub Actions / cron on a VPS).

Decides what to do instead of trusting the scheduler: GitHub throttles cron
(the documented minimum is 5 minutes, real drift is 5-20), so the workflow
fires often and this script picks quick vs full from the stored history.
"""
import sys
import time
from datetime import datetime, timezone

import watcher as w


def main():
    force = "--force" in sys.argv
    w.load_config()

    con = w.db_init()
    wallets = w.load_wallets()

    # Is a full (DeBank) run due for any wallet?
    due = []
    now = datetime.now(timezone.utc)
    for wal in wallets:
        last_full = con.execute(
            "SELECT ts FROM runs WHERE wallet=? AND kind='full' ORDER BY id DESC LIMIT 1",
            (wal["address"],)).fetchone()
        if not last_full:
            due.append(wal["address"])
            continue
        age_h = (now - datetime.fromisoformat(last_full[0])).total_seconds() / 3600
        if age_h >= w.FULL_INTERVAL_H:
            due.append(wal["address"])
    con.close()

    if due or force:
        print(f"full run due for {len(due)} wallet(s)")
        t0 = time.time()
        w.run("full", force)
        print(f"full finished in {time.time() - t0:.0f}s")
    else:
        w.run("quick", False)
        print("quick finished")


if __name__ == "__main__":
    main()