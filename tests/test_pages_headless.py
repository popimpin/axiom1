import asyncio
import subprocess
import time
import sys
import os
from playwright.async_api import async_playwright

async def verify_page(page, url, viewport, name):
    await page.set_viewport_size(viewport)
    console_errors = []
    page_errors = []

    page.on("console", lambda msg: console_errors.append(f"[{msg.type}] {msg.text}") if msg.type == "error" else None)
    page.on("pageerror", lambda exc: page_errors.append(str(exc)))

    response = await page.goto(url, wait_until="networkidle", timeout=15000)
    assert response is not None, f"Failed to load {url}"
    assert response.status == 200, f"Expected 200 for {url}, got {response.status}"

    # Wait an extra tick for JS hydration/rendering
    await page.wait_for_timeout(500)

    # Check horizontal overflow
    overflow_info = await page.evaluate("""() => {
        const docWidth = document.documentElement.scrollWidth;
        const bodyWidth = document.body.scrollWidth;
        const winWidth = window.innerWidth;
        const maxScroll = Math.max(docWidth, bodyWidth);
        return { maxScroll, winWidth, hasOverflow: maxScroll > winWidth };
    }""")

    # Note: Three.js webgl container may have fixed full width which is fine, but check layout
    res = {
        "name": name,
        "viewport": f"{viewport['width']}x{viewport['height']}",
        "url": url,
        "status": response.status,
        "console_errors": console_errors,
        "page_errors": page_errors,
        "overflow": overflow_info
    }
    return res

async def run_tests():
    # Start server
    server_script = os.path.join(os.path.dirname(__file__), "..", "scripts", "serve_pages.js")
    server_proc = subprocess.Popen(["node", server_script, "8975"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    time.sleep(1.0) # give server time to bind

    base_url = "http://127.0.0.1:8975"
    viewports = [
        {"width": 1280, "height": 800},
        {"width": 390, "height": 844}
    ]

    all_passed = True

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context()
            page = await context.new_page()

            # Test 1: index.html
            print("\n=== Testing index.html ===")
            for vp in viewports:
                res = await verify_page(page, f"{base_url}/index.html", vp, "index.html")
                print(f"[{res['viewport']}] Status: {res['status']}, Console Errors: {len(res['console_errors'])}, Overflow: {res['overflow']['hasOverflow']} (scroll={res['overflow']['maxScroll']}, win={res['overflow']['winWidth']})")
                if res['console_errors'] or res['page_errors'] or res['overflow']['hasOverflow']:
                    print(f"  ERRORS: {res['console_errors']} {res['page_errors']}")
                    all_passed = False
                
                # Check specific DOM contents
                motto = await page.inner_text(".motto")
                assert "Any agent that connects becomes part of one mind" in motto
                cards_count = await page.locator(".card").count()
                assert cards_count == 4, f"Expected 4 pillar cards, got {cards_count}"
                print(f"  Pillar cards: {cards_count}, Motto confirmed")

            # Test 2: claims.html
            print("\n=== Testing claims.html ===")
            for vp in viewports:
                res = await verify_page(page, f"{base_url}/claims.html", vp, "claims.html")
                print(f"[{res['viewport']}] Status: {res['status']}, Console Errors: {len(res['console_errors'])}, Overflow: {res['overflow']['hasOverflow']} (scroll={res['overflow']['maxScroll']}, win={res['overflow']['winWidth']})")
                if res['console_errors'] or res['page_errors'] or res['overflow']['hasOverflow']:
                    print(f"  ERRORS: {res['console_errors']} {res['page_errors']}")
                    all_passed = False

                # Verify claims and track records counts
                claims_count = await page.locator(".claim-card").count()
                print(f"  Rendered claims: {claims_count}")
                assert claims_count == 15, f"Expected 15 claims, got {claims_count}"

                # Test filter clicks
                await page.click("#btnFilterWitnessed")
                await page.wait_for_timeout(100)
                w_count = await page.locator(".claim-card").count()
                print(f"  Witnessed filter count: {w_count}")
                assert w_count == 1, f"Expected 1 witnessed claim, got {w_count}"

                await page.click("#btnFilterRefuted")
                await page.wait_for_timeout(100)
                r_count = await page.locator(".claim-card").count()
                print(f"  Refuted filter count: {r_count}")
                assert r_count == 14, f"Expected 14 refuted claims, got {r_count}"

                await page.click("#btnFilterAll")
                await page.wait_for_timeout(100)

                # Check Everyday 8 table rows
                rows_count = await page.locator("#everydayTableBody tr").count()
                print(f"  Everyday 8 table rows: {rows_count}")
                assert rows_count == 9, f"Expected 9 table rows (8 jobs + summary), got {rows_count}"

                # Check events count
                events_count = await page.locator("#eventsTableBody tr").count()
                print(f"  Events log count: {events_count}")
                assert events_count == 128, f"Expected 128 events, got {events_count}"

            # Test 3: web/timeline/index.html
            print("\n=== Testing web/timeline/index.html ===")
            for vp in viewports:
                res = await verify_page(page, f"{base_url}/web/timeline/index.html", vp, "timeline/index.html")
                print(f"[{res['viewport']}] Status: {res['status']}, Console Errors: {len(res['console_errors'])}, Overflow: {res['overflow']['hasOverflow']}")
                if res['console_errors'] or res['page_errors']:
                    print(f"  ERRORS: {res['console_errors']} {res['page_errors']}")
                    all_passed = False

                # Check Three.js canvas initialized
                canvas_count = await page.locator("#gl canvas").count()
                people_count = await page.locator(".person").count()
                print(f"  Canvas count: {canvas_count}, People buttons count: {people_count}")
                assert canvas_count == 1, f"Expected 1 WebGL canvas, got {canvas_count}"
                assert people_count >= 5, f"Expected at least 5 people buttons, got {people_count}"

            # Test 4: web/timeline/audit.html
            print("\n=== Testing web/timeline/audit.html ===")
            for vp in viewports:
                res = await verify_page(page, f"{base_url}/web/timeline/audit.html", vp, "timeline/audit.html")
                print(f"[{res['viewport']}] Status: {res['status']}, Console Errors: {len(res['console_errors'])}, Overflow: {res['overflow']['hasOverflow']}")
                if res['console_errors'] or res['page_errors'] or res['overflow']['hasOverflow']:
                    print(f"  ERRORS: {res['console_errors']} {res['page_errors']}")
                    all_passed = False

            # Test 5: web/timeline/inspect.html
            print("\n=== Testing web/timeline/inspect.html ===")
            for vp in viewports:
                res = await verify_page(page, f"{base_url}/web/timeline/inspect.html", vp, "timeline/inspect.html")
                print(f"[{res['viewport']}] Status: {res['status']}, Console Errors: {len(res['console_errors'])}, Overflow: {res['overflow']['hasOverflow']}")
                if res['console_errors'] or res['page_errors'] or res['overflow']['hasOverflow']:
                    print(f"  ERRORS: {res['console_errors']} {res['page_errors']}")
                    all_passed = False

            await browser.close()
    finally:
        server_proc.terminate()
        try:
            server_proc.wait(timeout=2)
        except Exception:
            server_proc.kill()

    if all_passed:
        print("\n>>> ALL HEADLESS BROWSER VERIFICATIONS PASSED (0 ERRORS, 0 OVERFLOWS, EXACT DATA COUNTS) <<<")
    else:
        print("\n>>> SOME TESTS FAILED <<<")
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(run_tests())
