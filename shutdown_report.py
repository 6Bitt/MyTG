"""
Send a final report right before Windows shuts down.

Windows gives a shutdown task only a few seconds, and DeBank scraping takes
~20 s, so we cannot do a live check here. Instead we report the last stored
snapshot and clearly say it is a snapshot - that is what the owner needs
before the machine goes off for the night.
"""
import json
from datetime import datetime, timezone

import watcher as w

LOCK = w.ROOT / "shutdown_report.lock"


def main():
    w.load_config()
    token = w.load_token()
    if not token:
        w.log("shutdown report: no BOT_TOKEN")
        return

    # Don't send twice if Windows fires the event more than once.
    now = datetime.now(timezone.utc)
    if LOCK.exists():
        try:
            last = datetime.fromisoformat(LOCK.read_text(encoding="utf-8").strip())
            if (now - last).total_seconds() < 900:
                w.log("shutdown report: already sent within 15 min, skip")
                return
        except Exception:
            pass
    LOCK.write_text(now.isoformat(timespec="seconds"), encoding="utf-8")

    con = w.db_init()
    parts = []
    for wal in w.load_wallets():
        last = w.db_last(con, wallet=wal["address"])
        if not last:
            parts.append(f"💤 <code>{wal['address'][:10]}…</code> — данных ещё нет")
            continue

        age_min = (now - datetime.fromisoformat(last["ts"])).total_seconds() / 60
        snap = {
            "ts": last["ts"], "kind": "shutdown", "wallet": wal["address"],
            "label": wal.get("label"),
            "portfolio_total": last.get("portfolio_total"),
            "balances": last.get("balances") or {},
            "positions": last.get("positions") or [],
            "prices": {},
        }
        body = w.render(snap, [])
        parts.append(body + f"\n\n⏱ <i>снимок на {age_min:.0f} мин назад</i>")

    text = ("🛑 <b>ПК выключается — последний снимок</b>\n"
            f"<i>{now.strftime('%Y-%m-%d %H:%M')} UTC</i>\n\n"
            + "\n\n➖➖➖\n\n".join(parts)
            + "\n\n<i>До следующего включения ПК проверок не будет.</i>")

    for i in range(0, len(text), 4000):
        w.send(token, text[i:i + 4000])
    w.log(f"shutdown report sent ({len(text)} chars)")
    con.close()


if __name__ == "__main__":
    main()