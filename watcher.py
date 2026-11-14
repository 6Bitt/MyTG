"""
DeFi wallet watcher: tracks balances, protocol positions and liquidity moves.

Cadences:
  --quick  every 15 min -> RPC balances only. Alerts on any movement.
  --full   every 10 h   -> RPC + DeBank positions + SQLite history.

Alert types (DeFi-focused):
  🆕 новый протокол / новый пул
  🚪 протокол закрыт (позиция ушла в ноль)
  💧 отток / приток ликвидности (пул изменился)
  💰 изменение баланса в сети
"""
import argparse
import json
import os
import re
import sqlite3
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from debank import (fetch_positions, last_wallet_tokens, last_change_24h,
                    summarize_by_protocol)

ROOT = Path(__file__).parent
DB_FILE = ROOT / "history.db"
LOG_FILE = ROOT / "watcher.log"
# .env lookup: $WATCHER_ENV, then a local .env, then the Windows wand.
_ENV_CANDIDATES = [
    Path(os.environ["WATCHER_ENV"]) if os.environ.get("WATCHER_ENV") else None,
    ROOT / ".env",
    Path(r"C:\Code\tg-channel-tools\.env"),
]
ENV_FILE = next((p for p in _ENV_CANDIDATES if p and p.exists()),
                ROOT / ".env")

# 4. Wallets come from wallets.json: [{"address": "0x…", "label": "опц."}].
# The watcher runs the same checks for each of them.
WALLETS_FILE = ROOT / "wallets.json"
WALLET = "0xE9D49344e8404A5DaCA0ae4e7FfAC1cfa584f7B1"  # fallback
CHAT_ID = "8989940602"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36"

CHAINS = {
    "ethereum": ("https://ethereum-rpc.publicnode.com", "https://eth.llamarpc.com", "ETH"),
    "base": ("https://mainnet.base.org", "https://base.llamarpc.com", "ETH"),
    "arbitrum": ("https://arb1.arbitrum.io/rpc", "https://arbitrum.llamarpc.com", "ETH"),
    "fraxtal": ("https://rpc.frax.com", "https://fraxtal.drpc.org", "FRAX"),
    "monad": ("https://rpc.monad.xyz", "https://rpc.ankr.com/monad", "MON"),
    "bsc": ("https://bsc-dataseed1.binance.org", "https://bsc.meowrpc.com", "BNB"),
}

CONFIG_FILE = ROOT / "config.json"
CONFIG_DEFAULTS = {
    "dust_usd": 1.0,
    "move_usd": 1.0,
    "gas_limit_usd": 0.5,
    "chart_min_spread_usd": 1.0,
    "chart_enabled": True,
    "quick_interval_min": 15,
    "heartbeat_hours": 24,
    "db_retention_days": 90,
    "silence_hours": 2,
    "fail_streak_limit": 3,
    "report_schedule": ["09:00", "21:00"],
    "digest_enabled": True,
    "digest_threshold": 5,
    "muted_until": None,
}
CONFIG = dict(CONFIG_DEFAULTS)

BALANCE_EPS = 1e-9
FULL_INTERVAL_H = 10  # scheduler cadence; reports follow report_schedule


def load_config():
    """Thresholds live in config.json so they change without touching code."""
    global CONFIG
    CONFIG = dict(CONFIG_DEFAULTS)
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for k, v in data.items():
                    if k in CONFIG_DEFAULTS:
                        CONFIG[k] = v
        except Exception as e:
            log(f"config.json invalid, using defaults: {str(e)[:60]}")
    return CONFIG


def save_config():
    CONFIG_FILE.write_text(json.dumps(CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")


def cfg(key):
    return CONFIG.get(key, CONFIG_DEFAULTS.get(key))


def is_muted():
    until = cfg("muted_until")
    if not until:
        return False
    try:
        return datetime.fromisoformat(until) > datetime.now(timezone.utc)
    except Exception:
        return False


LOG_MAX_BYTES = 512 * 1024
LOG_BACKUPS = 2


def log(msg):
    line = f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] {msg}"
    print(line)
    try:
        # rotate instead of growing forever: watcher.log -> .1 -> .2
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > LOG_MAX_BYTES:
            for i in range(LOG_BACKUPS - 1, 0, -1):
                src = LOG_FILE.with_suffix(f".log.{i}")
                dst = LOG_FILE.with_suffix(f".log.{i + 1}")
                if src.exists():
                    src.replace(dst)
            LOG_FILE.replace(LOG_FILE.with_suffix(".log.1"))
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_wallets():
    """4. [{"address": ..., "label": ...}] from wallets.json, else the built-in one."""
    if WALLETS_FILE.exists():
        try:
            data = json.loads(WALLETS_FILE.read_text(encoding="utf-8"))
            items = data if isinstance(data, list) else data.get("wallets", [])
            out = []
            for it in items:
                addr = it.get("address") if isinstance(it, dict) else it
                if isinstance(addr, str) and addr.startswith("0x") and len(addr) == 42:
                    label = it.get("label") if isinstance(it, dict) else None
                    out.append({"address": addr, "label": label})
            if out:
                return out
        except Exception as e:
            log(f"wallets.json invalid: {str(e)[:60]}")
    return [{"address": WALLET, "label": None}]


def load_token():
    if not ENV_FILE.exists():
        return ""
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("BOT_TOKEN="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def rpc(url, method, params, timeout=20):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json", "User-Agent": UA},
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = json.load(r)
            if "result" in d:
                return d["result"]
        except Exception:
            pass
        if attempt < 2:
            time.sleep(1.5 * (attempt + 1))
    return None


def rpc_chain(chain, method, params):
    for url in CHAINS[chain][:2]:
        r = rpc(url, method, params)
        if r is not None:
            return r
    return None


# ---------------------------------------------------------------- db

def db_init():
    con = sqlite3.connect(DB_FILE)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL, kind TEXT NOT NULL,
            portfolio_total REAL, notes TEXT,
            ok INTEGER DEFAULT 1,
            wallet TEXT,
            wallet_tokens TEXT,
            change_24h REAL
        );
        CREATE TABLE IF NOT EXISTS balances (
            run_id INTEGER, chain TEXT, balance REAL, nonce INTEGER, symbol TEXT
        );
        CREATE TABLE IF NOT EXISTS positions (
            run_id INTEGER, protocol TEXT, ptype TEXT, chain TEXT,
            pool TEXT, usd REAL, assets TEXT, health REAL, rewards TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_runs_ts ON runs(ts);
    """)
    # migration for databases created before the `ok` column existed
    cols = {r[1] for r in con.execute("PRAGMA table_info(runs)")}
    if "ok" not in cols:
        con.execute("ALTER TABLE runs ADD COLUMN ok INTEGER DEFAULT 1")
    if "wallet" not in cols:
        con.execute("ALTER TABLE runs ADD COLUMN wallet TEXT")
        con.execute("UPDATE runs SET wallet=?", (WALLET,))
    if "wallet_tokens" not in cols:
        con.execute("ALTER TABLE runs ADD COLUMN wallet_tokens TEXT")
    if "change_24h" not in cols:
        con.execute("ALTER TABLE runs ADD COLUMN change_24h REAL")
    pcols = {r[1] for r in con.execute("PRAGMA table_info(positions)")}
    if "rewards" not in pcols:
        con.execute("ALTER TABLE positions ADD COLUMN rewards TEXT")
    con.commit()
    return con


def db_last(con, need_positions=False, wallet=None):
    """Last run (optionally for one wallet / one that has positions)."""
    where, args = [], []
    if wallet:
        where.append("wallet=?")
        args.append(wallet)
    if need_positions:
        where.append("ok=1")
        where.append("id IN (SELECT run_id FROM positions)")
    sql = "SELECT id, portfolio_total, ts, wallet_tokens, change_24h FROM runs"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT 1"
    row = con.execute(sql, args).fetchone()
    if not row:
        return None
    run_id = row[0]
    balances = {
        c: {"balance": b, "nonce": n, "symbol": s}
        for c, b, n, s in con.execute(
            "SELECT chain, balance, nonce, symbol FROM balances WHERE run_id=?", (run_id,))
    }
    positions = [
        {"protocol": p, "type": t, "chain": ch, "pool": pool,
         "usd": u, "assets": a.split("|") if a else [],
         "rewards": rw.split("|") if rw else [], "health": h}
        for p, t, ch, pool, u, a, rw, h in con.execute(
            "SELECT protocol, ptype, chain, pool, usd, assets, rewards, health "
            "FROM positions WHERE run_id=?", (run_id,))
    ]
    try:
        tokens = json.loads(row[3]) if row[3] else []
    except (json.JSONDecodeError, TypeError):
        tokens = []
    return {"run_id": run_id, "portfolio_total": row[1], "ts": row[2],
            "balances": balances, "positions": positions,
            "wallet_tokens": tokens, "change_24h": row[4]}


def db_known_protocols(con):
    """Every protocol ever seen in history (so a 'new' one is genuinely new)."""
    return {r[0] for r in con.execute("SELECT DISTINCT protocol FROM positions")}


def db_fail_streak(con, wallet):
    """How many successful runs ago the last failure was (0 = latest run failed)."""
    rows = list(con.execute(
        "SELECT ok FROM runs WHERE wallet=? AND kind='full' ORDER BY id DESC LIMIT 10",
        (wallet,)))
    streak = 0
    for (ok,) in rows:
        if ok == 0:
            streak += 1
        else:
            break
    return streak


def db_last_ok_ts(con, wallet):
    row = con.execute(
        "SELECT ts FROM runs WHERE wallet=? AND ok=1 ORDER BY id DESC LIMIT 1",
        (wallet,)).fetchone()
    return row[0] if row else None


def db_prune(con, days=None):
    """6. Drop rows older than the retention window."""
    days = days or cfg("db_retention_days")
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    old = [r[0] for r in con.execute("SELECT id FROM runs WHERE ts < ?", (cutoff,))]
    if not old:
        return 0
    marks = ",".join("?" * len(old))
    con.execute(f"DELETE FROM balances WHERE run_id IN ({marks})", old)
    con.execute(f"DELETE FROM positions WHERE run_id IN ({marks})", old)
    con.execute(f"DELETE FROM runs WHERE id IN ({marks})", old)
    con.commit()
    return len(old)


def check_silence(con, wallets, token):
    """Yellow flag: successful checks stopped running (scheduler died?)."""
    now = datetime.now(timezone.utc)
    stale = []
    for w in wallets:
        ts = db_last_ok_ts(con, w["address"])
        if not ts:
            continue
        age_h = (now - datetime.fromisoformat(ts)).total_seconds() / 3600
        if age_h >= cfg("silence_hours"):
            stale.append((w["address"], int(age_h)))
    if not stale:
        return
    lines = ["🔕 <b>Проверки не проходят</b>"]
    for addr, h in stale:
        lines.append(f"  <code>{addr[:10]}…</code> — {h} ч без успешной проверки")
    lines.append("\n<i>Возможно, ПК выключен или задача упала.</i>")
    send(token, "\n".join(lines))
    log(f"silence alert: {len(stale)} wallet(s)")


def heartbeat_due(con):
    """3. True when no heartbeat has been sent for heartbeat_hours."""
    row = con.execute(
        "SELECT ts FROM runs WHERE kind='heartbeat' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not row:
        return True
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(row[0])).total_seconds() / 3600
    return age >= cfg("heartbeat_hours")


def db_save(con, snap, notes):
    cur = con.execute(
        "INSERT INTO runs(ts, kind, portfolio_total, notes, ok, wallet, "
        "wallet_tokens, change_24h) VALUES (?,?,?,?,?,?,?,?)",
        (snap["ts"], snap["kind"], snap.get("portfolio_total"),
         "; ".join(notes)[:2000] or None, 0 if snap.get("debank_error") else 1,
         snap.get("wallet"),
         json.dumps(snap.get("wallet_tokens") or [], ensure_ascii=False),
         snap.get("change_24h")))
    rid = cur.lastrowid
    # Balances always come from RPC, so they are valid even when DeBank fails.
    for c, v in snap["balances"].items():
        con.execute("INSERT INTO balances VALUES (?,?,?,?,?)",
                    (rid, c, v.get("balance"), v.get("nonce"), v.get("symbol")))
    # Failed run: store no positions. Otherwise the next diff would see
    # "everything closed" now, then "everything new" once the feed recovers.
    if not snap.get("debank_error"):
        for p in snap.get("positions", []):
            con.execute("INSERT INTO positions VALUES (?,?,?,?,?,?,?,?,?)",
                        (rid, p["protocol"], p.get("type"), p.get("chain"), p.get("pool"),
                         p.get("usd"), "|".join(p.get("assets") or []), p.get("health"),
                         "|".join(p.get("rewards") or [])))
    con.commit()


# ---------------------------------------------------------------- collect

def snapshot_balances(wallet):
    out = {}
    for name, (_, _, sym) in CHAINS.items():
        bal = rpc_chain(name, "eth_getBalance", [wallet, "latest"])
        nonce = rpc_chain(name, "eth_getTransactionCount", [wallet, "latest"])
        out[name] = {
            "symbol": sym,
            "balance": int(bal, 16) / 1e18 if bal else None,
            "nonce": int(nonce, 16) if nonce else None,
        }
    return out


def pos_key(p):
    return f"{p['protocol']}|{p.get('type') or ''}|{p.get('chain') or ''}|{p.get('pool') or ''}"


# ---------------------------------------------------------------- diff

COINGECKO_IDS = {
    "ETH": "ethereum", "BNB": "binancecoin", "MON": "monad",
    "FRAX": "frax-share",
}


def fetch_prices():
    """USD prices, cached for 10 minutes. Returns {symbol: price}."""
    cache = ROOT / "prices.json"
    try:
        if cache.exists() and time.time() - cache.stat().st_mtime < 600:
            return json.loads(cache.read_text(encoding="utf-8"))
    except Exception:
        pass

    ids = ",".join(sorted(set(COINGECKO_IDS.values())))
    url = f"https://api.coingecko.com/api/v3/simple/price?ids={ids}&vs_currencies=usd"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = json.load(r)
        out = {sym: float(raw[cg]["usd"]) for sym, cg in COINGECKO_IDS.items() if cg in raw}
    except Exception as e:
        log(f"prices unavailable: {str(e)[:60]}")
        return {}
    try:
        cache.write_text(json.dumps(out), encoding="utf-8")
    except Exception:
        pass
    return out


def esc(s):
    """Escape for Telegram HTML. Tokens like '<$0.01' otherwise break parsing."""
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


ASSET_RE = re.compile(r"^([\d,]+(?:\.\d+)?)\s+([A-Za-z0-9._-]{2,12})")


def _parse_assets(items):
    """['15.0050 MUSD', '0.7491 USDC'] -> {'MUSD': 15.005, 'USDC': 0.7491}"""
    out = {}
    for it in (items or []):
        m = ASSET_RE.match(str(it))
        if not m:
            continue
        try:
            out[m.group(2)] = out.get(m.group(2), 0.0) + float(m.group(1).replace(",", ""))
        except ValueError:
            pass
    return out


def _asset_delta(prev_assets, cur_assets, min_change=0.005):
    """3. Which token inside the pool moved: 'MUSD +0.03', 'USDT -0.05'."""
    a, b = _parse_assets(prev_assets), _parse_assets(cur_assets)
    out = []
    for sym in sorted(set(a) | set(b)):
        d = b.get(sym, 0.0) - a.get(sym, 0.0)
        if abs(d) >= min_change:
            out.append(f"{sym} {d:+.4g}")
    return out


def compute_pnl(con, wallet, current_total, days=7):
    """4. Portfolio change over the last N days from stored history."""
    if current_total is None:
        return None
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    row = con.execute(
        "SELECT portfolio_total FROM runs WHERE wallet=? AND ts >= ? "
        "AND portfolio_total IS NOT NULL ORDER BY ts LIMIT 1",
        (wallet, since)).fetchone()
    if not row or not row[0]:
        return None
    start = row[0]
    delta = current_total - start
    pct = (delta / start * 100) if start else 0.0
    return (days, start, delta, pct)


def plural_ru(n, one, few, many):
    """Russian plural form: 1 отправка / 3 отправки / 5 отправок."""
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not (12 <= n % 100 <= 14):
        return few
    return many


def diff_defi(prev, cur, known_protocols=None, prices=None):
    """DeFi-focused change list. known_protocols = every protocol ever seen."""
    notes = []
    prices = prices or {}
    if not prev:
        return notes
    known = known_protocols if known_protocols is not None else {}

    # If the position feed failed this run, positions are unknown - comparing
    # them against the previous snapshot would emit bogus "protocol closed"
    # alerts. Skip position diffing entirely in that case.
    positions_known = not cur.get("debank_error")

    # 1. native balance moves
    for chain, c in cur["balances"].items():
        p = prev["balances"].get(chain, {})
        pb, cb = p.get("balance"), c.get("balance")
        pn, cn = p.get("nonce"), c.get("nonce")
        nonce_grew = pn is not None and cn is not None and cn > pn

        if pb is not None and cb is not None and abs(pb - cb) > BALANCE_EPS:
            d = cb - pb
            if d > 0:
                label = "Поступление"
            else:
                # gas-sized delta = a few cents regardless of the coin
                price = prices.get(c["symbol"])
                is_gas = (nonce_grew and price is not None
                          and abs(d) * price < cfg("gas_limit_usd"))
                label = "Комиссия" if is_gas else "Перевод"
            usd = ""
            price = prices.get(c["symbol"])
            if price:
                usd = f" ≈${abs(d) * price:,.2f}"
            notes.append(f"{'🟢' if d > 0 else '🔴'} <b>{chain.upper()}</b> "
                         f"{pb:.6f} → {cb:.6f} {c['symbol']} {label} ({d:+.6f}{usd})")

        if nonce_grew:
            n = cn - pn
            word = plural_ru(n, "новая отправка", "новые отправки", "новых отправок")
            notes.append(f"⚡ <b>{chain.upper()}</b>: {n} {word} (всего {cn})")

    # 2. protocol / pool level
    if not positions_known:
        return notes

    prev_map = {pos_key(p): p for p in prev.get("positions", []) if p["usd"] >= cfg("dust_usd")}
    cur_map = {pos_key(p): p for p in cur.get("positions", []) if p["usd"] >= cfg("dust_usd")}

    for key, p in cur_map.items():
        if key not in prev_map:
            # 🔥 — protocol never seen before; 🆕 — new pool in a known protocol
            icon = "🆕" if p["protocol"] in known else "🔥"
            label = "НОВЫЙ ПРОТОКОЛ" if icon == "🔥" else "новый пул"
            notes.append(f"{icon} <b>{p['protocol']}</b> · {p.get('type') or 'позиция'} · "
                         f"{p.get('chain') or '?'} — ${p['usd']:,.2f}  ({label})"
                         + (f"\n     {p['pool']}" if p.get("pool") else ""))
    for key, p in prev_map.items():
        if key not in cur_map:
            notes.append(f"🚪 <b>{p['protocol']}</b> · {p.get('type') or 'позиция'} · "
                         f"{p.get('chain') or '?'} — закрыто (было ${p['usd']:,.2f})")

    for key in set(prev_map) & set(cur_map):
        a, b = prev_map[key]["usd"], cur_map[key]["usd"]
        if abs(b - a) >= cfg("move_usd"):
            icon = "📈" if b > a else "📉"
            line = (f"{icon} <b>{cur_map[key]['protocol']}</b> "
                    f"({cur_map[key].get('chain') or '?'}) "
                    f"${a:,.2f} → ${b:,.2f} ({b - a:+,.2f})")
            # 3. show which asset inside the pool actually moved
            asset_delta = _asset_delta(prev_map[key].get("assets"),
                                       cur_map[key].get("assets"))
            if asset_delta:
                line += "\n     " + ", ".join(asset_delta)
            notes.append(line)

    pt, ct = prev.get("portfolio_total"), cur.get("portfolio_total")
    if pt is not None and ct is not None and abs(pt - ct) >= 1.0:
        notes.append(f"💼 <b>Портфель</b>: ${pt:,.2f} → ${ct:,.2f} ({ct - pt:+,.2f})")

    return notes


# ---------------------------------------------------------------- render

CHAIN_ICON = {
    "ethereum": "⟠", "base": "🔵", "arbitrum": "🔷",
    "fraxtal": "🟣", "monad": "🟪", "bsc": "🟡",
}
TYPE_ICON = {
    "Lending": "🏦", "Yield": "🌾", "Rewards": "🎁",
    "Liquidity": "💧", "Staking": "🔒",
}
SEP = "──────────────"


def render(snap, notes):
    """Telegram HTML. Layout uses blank lines + separators, never leading spaces."""
    WALLET = snap.get("wallet") or ""
    total = snap.get("portfolio_total")
    parts = []

    # --- header -------------------------------------------------
    parts.append(f"🏦 <b>DeFi-кошелёк</b>\n"
                 f"<code>{WALLET}</code>\n"
                 f"<i>{snap['ts'].replace('T', ' ')} · {snap['kind']}</i>")

    if total is not None:
        ch = snap.get("change_24h")
        suffix = ""
        if ch is not None:
            arrow = "📈" if ch >= 0 else "📉"
            suffix = f"  {arrow} {ch:+.2f}% за 24ч"
        parts.append(f"💼 <b>Итого: ${total:,.2f}</b>{suffix}")

    # 4. PnL over the stored history
    pnl = snap.get("pnl")
    if pnl:
        days, start, delta, pct = pnl
        arrow = "📈" if delta >= 0 else "📉"
        parts.append(f"{arrow} <b>За {days} дн.:</b> ${start:,.2f} → "
                     f"${total:,.2f} ({delta:+,.2f}, {pct:+.1f}%)")

    # --- changes ------------------------------------------------
    if notes:
        parts.append(f"🔔 <b>ИЗМЕНЕНИЯ</b>\n{SEP}\n" + "\n\n".join(notes))
    else:
        parts.append(f"<i>Без изменений</i>")

    # --- positions ----------------------------------------------
    positions = [p for p in (snap.get("positions") or []) if p["usd"] >= 0.01]
    if positions:
        blocks = [f"📊 <b>ПРОТОКОЛЫ</b>\n{SEP}"]
        for p in sorted(positions, key=lambda x: -x["usd"]):
            chain = p.get("chain") or "?"
            ci = CHAIN_ICON.get(chain, "•")
            ti = TYPE_ICON.get(p.get("type") or "", "▫️")
            usd = f"${p['usd']:,.2f}"

            line = f"{ti} <b>{esc(p['protocol'])}</b> — <b>{usd}</b>\n"
            line += f"{ci} {chain}"
            if p.get("type"):
                line += f" · {p['type']}"
            if p.get("health"):
                line += f" · HF <b>{p['health']:.2f}</b>"
            if p.get("pool"):
                line += f"\n└ <i>{esc(p['pool'])}</i>"
            if p.get("assets"):
                line += f"\n└ <code>{esc(', '.join(p['assets'][:3]))}</code>"
            blocks.append(line)
        parts.append("\n\n".join(blocks))

    # --- wallet balances ----------------------------------------
    wallet_lines = []
    for chain, c in snap["balances"].items():
        if c.get("balance") is None:
            continue
        if c["balance"] <= 0 and not (c.get("nonce") or 0) > 0:
            continue
        ci = CHAIN_ICON.get(chain, "•")
        wallet_lines.append(f"{ci} {chain:<9} {c['balance']:.6f} {c['symbol']}")
    if wallet_lines:
        parts.append(f"👛 <b>КОШЕЛЁК</b>\n{SEP}\n<pre>" + "\n".join(wallet_lines) + "</pre>")

    # 1. tokens sitting in the wallet itself (FXB, frxUSD, CRV...)
    tokens = snap.get("wallet_tokens") or []
    if tokens:
        rows = []
        for t in tokens:
            if not isinstance(t, dict):
                continue
            usd = t.get("usd", "")
            try:
                if float(str(usd).lstrip("$").replace(",", "")) < 0.01:
                    continue
            except (ValueError, AttributeError):
                pass
            rows.append(f"{t.get('symbol','?'):<12} {t.get('amount',''):>12} {usd:>9}")
        if rows:
            parts.append(f"🪙 <b>ТОКЕНЫ В КОШЕЛЬКЕ</b>\n{SEP}\n<pre>"
                         + "\n".join(rows) + "</pre>")

    # 2. unclaimed rewards across positions
    reward_rows = []
    for p in positions:
        for r in (p.get("rewards") or []):
            # "<$0.01" would open a fake HTML tag and Telegram rejects the
            # whole message, so escape every user-derived string.
            safe = esc(r)
            reward_rows.append(f"{esc(p['protocol'])}: {safe}")
    if reward_rows:
        parts.append("🎁 <b>К ПОЛУЧЕНИЮ</b>\n" + "\n".join(reward_rows[:6]))

    if snap.get("debank_error"):
        parts.append(f"⚠️ <i>DeBank: {snap['debank_error'][:60]}</i>")

    return "\n\n".join(parts)


def render_chart(con, wallet, out_path):
    """8. Portfolio value over time as a PNG. Returns path or None."""
    rows = con.execute(
        "SELECT ts, portfolio_total FROM runs "
        "WHERE wallet=? AND portfolio_total IS NOT NULL AND kind IN ('full','heartbeat') "
        "ORDER BY ts", (wallet,)).fetchall()
    if len(rows) < 2:
        return None
    # A flat line tells you nothing - skip it entirely.
    vals = [r[1] for r in rows]
    if max(vals) - min(vals) < cfg("chart_min_spread_usd"):
        return None
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from datetime import datetime as _dt
    except Exception as e:
        log(f"chart unavailable: {str(e)[:60]}")
        return None

    xs = [_dt.fromisoformat(r[0]) for r in rows]
    ys = [r[1] for r in rows]
    fig, ax = plt.subplots(figsize=(9, 3.2), dpi=140)
    ax.plot(xs, ys, marker="o", markersize=2.5, linewidth=1.6, color="#2e86de")
    ax.fill_between(xs, ys, min(ys) * 0.98, alpha=0.16, color="#2e86de")
    ax.set_ylabel("USD")
    ax.set_title(f"Портфель {wallet[:8]}…{wallet[-4:]}  ({len(ys)} точек)", fontsize=9)
    ax.grid(alpha=0.25, linestyle="--")
    ax.tick_params(labelsize=7)
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)
    return out_path


def send_photo(token, photo_path, caption):
    """Send a PNG to Telegram (multipart)."""
    try:
        import uuid
        boundary = uuid.uuid4().hex
        with open(photo_path, "rb") as f:
            data = f.read()
        parts = []
        for k, v in (("chat_id", CHAT_ID), ("caption", caption),
                     ("parse_mode", "HTML")):
            parts.append(f"--{boundary}\r\nContent-Disposition: form-data; "
                         f"name=\"{k}\"\r\n\r\n{v}\r\n".encode())
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; "
                     f"name=\"photo\"; filename=\"chart.png\"\r\n"
                     f"Content-Type: image/png\r\n\r\n".encode())
        parts.append(data)
        parts.append(f"\r\n--{boundary}--\r\n".encode())
        body = b"".join(parts)
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendPhoto", data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        with urllib.request.urlopen(req, timeout=45) as r:
            return json.load(r).get("ok", False)
    except Exception as e:
        log(f"sendPhoto error: {str(e)[:100]}")
        return False


def send(token, text):
    try:
        body = json.dumps({"chat_id": CHAT_ID, "text": text[:4000],
                           "parse_mode": "HTML", "disable_web_page_preview": True}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.load(r).get("ok", False)
    except Exception as e:
        log(f"telegram error: {str(e)[:100]}")
        return False


# ---------------------------------------------------------------- main

def check_wallet(con, wallet, label, kind, force, prices, token):
    """One wallet: collect, diff, maybe send."""
    prev = db_last(con, need_positions=True, wallet=wallet) if kind == "full" \
        else db_last(con, wallet=wallet)
    prev_any = db_last(con, wallet=wallet)

    snap = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "kind": kind, "wallet": wallet, "label": label,
            "balances": snapshot_balances(wallet), "prices": prices}

    # 7. detect skipped checks (machine was off)
    if prev_any and kind == "full":
        age_h = (datetime.now(timezone.utc)
                 - datetime.fromisoformat(prev_any["ts"])).total_seconds() / 3600
        if age_h >= FULL_INTERVAL_H + 1:
            snap["gap_hours"] = int(age_h)

    if kind == "full":
        total, positions, err = fetch_positions(wallet)
        snap["portfolio_total"] = total
        snap["positions"] = positions
        snap["wallet_tokens"] = last_wallet_tokens()
        snap["change_24h"] = last_change_24h()
        if err:
            snap["debank_error"] = err
    else:
        snap["portfolio_total"] = prev.get("portfolio_total") if prev else None
        snap["positions"] = prev.get("positions") if prev else []

    snap["pnl"] = compute_pnl(con, wallet, snap.get("portfolio_total"))
    notes = diff_defi(prev, snap, db_known_protocols(con), prices)
    if snap.get("gap_hours"):
        notes.insert(0, f"⏸ <b>Пропуск {snap['gap_hours']} ч</b> — "
                        f"проверок не было, пока ПК был выключен")

    if is_muted() and not force:
        log(f"{kind} {wallet[:8]}: muted, skipping send")
        db_save(con, snap, notes)
        return

    should_send = force or kind == "full" or bool(notes)
    if should_send:
        ok = send(token, render(snap, notes))
        log(f"{kind} {wallet[:8]}: sent={ok} alerts={len(notes)}")
    else:
        log(f"{kind} {wallet[:8]}: quiet")

    # Persist before charting so the plot includes this run.
    db_save(con, snap, notes)

    # 8. Chart rides along with a real alert, and only when the curve moved -
    # a flat line tells you nothing.
    if (should_send and notes and cfg("chart_enabled")
            and not is_muted()):
        png = render_chart(con, wallet, ROOT / f"chart_{wallet[:8]}.png")
        if png:
            send_photo(token, png, "📈 Динамика портфеля")

    # Loud, separate notice when the position feed keeps failing - the
    # inline "⚠️ DeBank: …" line is too easy to miss.
    if kind == "full" and snap.get("debank_error"):
        streak = db_fail_streak(con, wallet)
        if streak >= cfg("fail_streak_limit"):
            word = "раза" if streak < 5 else "раз"
            send(token, (
                f"⚠️ <b>DeBank недоступен {streak} {word} подряд</b>\n"
                f"<code>{wallet}</code>\n"
                f"Последний сбой: {datetime.now(timezone.utc).strftime('%H:%M')} UTC\n\n"
                f"Позиции не обновляются — проверь вручную.\n"
                f"<i>{snap['debank_error'][:120]}</i>"
            ))
            log(f"alert: debank fail streak {streak} for {wallet[:8]}")


def report_due(con):
    """True when the current local time matches one of report_schedule slots."""
    now = datetime.now()
    slot = now.strftime("%H:%M")
    if slot not in (cfg("report_schedule") or []):
        return False
    # don't fire twice in the same minute window
    last = con.execute(
        "SELECT ts FROM runs WHERE kind='report' ORDER BY id DESC LIMIT 1").fetchone()
    if last:
        age_min = (datetime.now(timezone.utc)
                   - datetime.fromisoformat(last[0])).total_seconds() / 60
        if age_min < 30:
            return False
    return True


def pending_changes(con):
    """Changes recorded since the last delivered report (for digest)."""
    last = con.execute(
        "SELECT ts FROM runs WHERE kind='report' ORDER BY id DESC LIMIT 1").fetchone()
    since = last[0] if last else "1970-01-01T00:00:00+00:00"
    rows = list(con.execute(
        "SELECT ts, notes FROM runs WHERE kind='quick' AND ts > ? AND notes IS NOT NULL "
        "ORDER BY id DESC LIMIT 40", (since,)))
    return rows


def run(kind, force):
    con = db_init()
    load_config()
    token = load_token()
    prices = fetch_prices()
    wallets = load_wallets()

    if kind == "quick":
        check_silence(con, wallets, token)

    for w in wallets:
        try:
            check_wallet(con, w["address"], w.get("label"), kind, force, prices, token)
        except Exception as e:
            log(f"ERROR {w['address'][:8]}: {str(e)[:120]}")

    # Morning/evening digest instead of a fixed 10-hour cadence.
    if kind == "full" and report_due(con):
        pending = pending_changes(con)
        n = len(pending)
        head = [f"📋 <b>Сводка за период</b>",
                f"<i>{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC</i>"]
        if n == 0:
            head.append("\nСобытий не было.")
        elif n == 1:
            head.append(f"\nБыло {n} изменение (см. отчёт ниже).")
        else:
            word = "изменения" if n < 5 else "изменений"
            head.append(f"\nБыло {n} {word} за период.")
            tail = [r[1] for r in pending[:5] if r[1]]
            if tail:
                head.append("\n<i>Последние:</i>")
                head.extend("• " + t.split(";")[0].strip() for t in tail)
        send(token, "\n".join(head))
        con.execute("INSERT INTO runs(ts, kind, portfolio_total, notes, ok) "
                    "VALUES (?,?,?,?,?)",
                    (datetime.now(timezone.utc).isoformat(timespec="seconds"),
                     "report", None, f"{n} pending", 1))
        con.commit()
        log(f"digest sent ({n} pending changes)")

    # 3. daily heartbeat - proof the bot is alive even with zero activity
    if kind == "full" and heartbeat_due(con):
        lines = [f"✅ <b>Бот работает</b>",
                 f"<i>{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC</i>",
                 f"Кошельков в мониторинге: {len(wallets)}"]
        for w in wallets:
            last = db_last(con, wallet=w["address"])
            if last:
                total = last.get("portfolio_total")
                t = f"${total:,.2f}" if total is not None else "—"
                lines.append(f"  {w['address'][:10]}… — {t}")
        send(token, "\n".join(lines))
        con.execute("INSERT INTO runs(ts, kind, portfolio_total, notes, ok) "
                    "VALUES (?,?,?,?,?)",
                    (datetime.now(timezone.utc).isoformat(timespec="seconds"),
                     "heartbeat", None, None, 1))
        con.commit()
        log("heartbeat sent")

    # 6. retention
    pruned = db_prune(con)
    if pruned:
        log(f"pruned {pruned} runs older than {cfg('db_retention_days')} days")

    con.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    run("full" if (args.full or not args.quick) else "quick", args.force)


if __name__ == "__main__":
    main()