"""Structured DeBank position scraper.

Returns positions as dicts:
    {"protocol": "Accountable", "type": "Yield", "chain": "monad",
     "pool": "Hyperithm Delta Neutral Vault", "usd": 104.53,
     "assets": ["104.4509 USDC"], "health": None}
"""
import re
import time

PROFILE_URL = "https://debank.com/profile/{wallet}"

JS_CARDS = """() => {
  const cards = [...document.querySelectorAll('div[class*="Project_project__"]')];
  return cards.map(el => {
    const lines = (el.innerText || '').split('\\n').map(s => s.trim()).filter(Boolean);
    const hrefs = [...el.querySelectorAll('a[href]')].map(a => a.getAttribute('href') || '');
    return { lines, hrefs };
  });
}"""

JS_TOTAL = """() => {
  const body = document.body.innerText || '';
  return body;
}"""

CHAIN_ALIASES = {
    "eth": "ethereum", "ethereum": "ethereum",
    "bsc": "bsc", "bnb": "bsc",
    "base": "base", "arb": "arbitrum", "arbitrum": "arbitrum",
    "frax": "fraxtal", "fraxtal": "fraxtal",
    "monad": "monad", "op": "optimism",
}

# words that are table headers, never pool names
NOISE = {
    "yield", "lending", "pool", "balance", "usd value", "supplied", "borrowed",
    "health rate", "rewards", "withdraw", "claim", "deposit", "supply", "borrow",
    "stake", "unstake", "lock", "unlock", "wallet", "tokens", "price", "amount",
}

MONEY = re.compile(r"^\$?([\d,]+(?:\.\d+)?)$")
ASSET = re.compile(r"^([\d,]+(?:\.\d+)?)\s+([A-Za-z0-9._-]{2,12})$")


def _parse_card(card):
    lines = [l for l in card["lines"] if l]
    if not lines:
        return None

    protocol = lines[0]
    usd = None
    ptype = None
    health = None
    pool = None
    assets = []

    i = 1
    # value right after the protocol name
    if i < len(lines) and MONEY.match(lines[i]) and lines[i].startswith("$"):
        usd = float(lines[i].lstrip("$").replace(",", ""))
        i += 1
    if i < len(lines) and lines[i].lower() in {"yield", "lending", "rewards", "liquidity", "staking"}:
        ptype = lines[i].capitalize()
        i += 1

    rest = lines[i:]
    # health rate is the number right after "Health Rate"
    for j, ln in enumerate(rest):
        if ln.lower() == "health rate" and j + 1 < len(rest):
            try:
                health = float(rest[j + 1])
            except ValueError:
                pass
            break

    # pool name: first meaningful line that is not a header / amount / $
    for ln in rest:
        low = ln.lower()
        if low in NOISE:
            continue
        if MONEY.match(ln) or ASSET.match(ln):
            continue
        if low.startswith("health rate"):
            continue
        pool = ln
        break

    for ln in rest:
        m = ASSET.match(ln)
        if m:
            assets.append(ln)

    # the pool's own USD value is the largest $ figure after the header block
    pool_usd = None
    for ln in rest:
        if ln.startswith("$") and MONEY.match(ln):
            v = float(ln.lstrip("$").replace(",", ""))
            if pool_usd is None or v > pool_usd:
                pool_usd = v
    if pool_usd is not None:
        usd = pool_usd
    elif usd is None:
        usd = 0.0

    chain = None
    for h in card["hrefs"]:
        m = re.search(r"/token/([a-z0-9]+)/", h)
        if m:
            chain = CHAIN_ALIASES.get(m.group(1), m.group(1))
            break

    return {
        "protocol": protocol,
        "type": ptype,
        "chain": chain,
        "pool": pool,
        "usd": round(usd or 0.0, 2),
        "assets": assets[:6],
        "health": health,
    }


def fetch_positions(wallet, timeout_wait=60):
    """Scrape DeBank. Returns (total_usd, [positions], error|None)."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        return None, [], f"playwright unavailable: {e}"

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1600, "height": 1800})
            page.goto(PROFILE_URL.format(wallet=wallet), timeout=90000,
                      wait_until="domcontentloaded")
            deadline = time.time() + timeout_wait
            started = time.time()
            body = ""
            while time.time() < deadline:
                time.sleep(2)
                body = page.inner_text("body")
                if "Morpho" in body and ("Lending" in body or "Yield" in body):
                    break
            cards = page.evaluate(JS_CARDS)
            waited = time.time() - started
            print(f"[debank] waited {waited:.0f}s, cards={len(cards)}, "
                  f"body_len={len(body)}")
            browser.close()
    except Exception as e:
        return None, [], f"debank scrape failed: {str(e)[:90]}"

    # A loaded profile page always contains the address and the "Wallet" block.
    # Their absence means we got a captcha / empty shell / blocked IP - that is
    # an error, NOT "this wallet has no positions".
    body_low = body.lower()
    address_seen = wallet.lower() in body_low
    page_ok = address_seen or ("wallet" in body_low and "$" in body)
    if not page_ok:
        head = " ".join(body.split())[:120]
        return None, [], f"debank page did not load (blocked/captcha?): {head}"

    total = None
    seg = body[body.find("All Chain"): body.find("Unfold")] if "All Chain" in body else ""
    vals = [float(v.replace(",", "")) for v in re.findall(r"\$([\d,]+(?:\.\d+)?)\s+\d+%", seg)]
    if vals:
        total = sum(vals)

    positions = [c for c in (_parse_card(card) for card in cards) if c]
    # drop empty wallets / dust rows
    positions = [p for p in positions if p["usd"] > 0 or p["assets"]]

    # Page loaded but the protocol strip is missing while other data exists:
    # report it, so a silent zero does not masquerade as "all closed".
    if not positions and total and total > 1:
        return total, [], ("debank: portfolio shows $%.2f but no protocol cards "
                           "parsed (layout changed?)" % total)
    return total, positions, None


def summarize_by_protocol(positions):
    agg = {}
    for p in positions:
        agg[p["protocol"]] = agg.get(p["protocol"], 0.0) + p["usd"]
    return sorted(agg.items(), key=lambda kv: -kv[1])