import sys, io, time
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
from playwright.sync_api import sync_playwright

URL = "https://github.com/6Bitt/MyTG/actions"

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    pg = b.new_page(viewport={"width": 1500, "height": 1200})
    pg.goto(URL, timeout=90000, wait_until="domcontentloaded")
    time.sleep(5)

    # open the run
    link = pg.query_selector('a[href*="/actions/runs/"]')
    if not link:
        print("запуск не найден")
        b.close()
        raise SystemExit
    href = link.get_attribute("href")
    print("run:", href)
    pg.goto(href if href.startswith("http") else "https://github.com" + href, timeout=90000, wait_until="domcontentloaded")
    time.sleep(6)

    txt = " ".join(pg.inner_text("body").split())
    print()
    print("=== СТРАНИЦА ЗАПУСКА ===")
    print(txt[:1800])
    print()
    print("=== ССЫЛКИ НА JOB ===")
    for a in pg.query_selector_all('a[href*="/job/"]')[:3]:
        print("  ", a.get_attribute("href"))
    b.close()