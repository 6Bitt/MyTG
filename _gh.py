import sys, io, time
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
from playwright.sync_api import sync_playwright

URL = "https://github.com/6Bitt/MyTG/actions"

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    pg = b.new_page(viewport={"width": 1500, "height": 1200})
    pg.goto(URL, timeout=90000, wait_until="domcontentloaded")
    time.sleep(6)
    print("URL:", pg.url)
    print("TITLE:", pg.title())
    txt = " ".join(pg.inner_text("body").split())
    print()
    print("=== BODY (первые 2500) ===")
    print(txt[:2500])
    b.close()