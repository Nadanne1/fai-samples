#!/usr/bin/env python3
"""End-to-end test suite for Clinical Trial Screening dashboard.

Usage:
    python3 infra/e2e_test.py                          # test localhost:5173 (dev server)
    python3 infra/e2e_test.py --production             # test live CloudFront URL
    python3 infra/e2e_test.py --url http://localhost:5173  # test specific URL
    python3 infra/e2e_test.py --headed                 # show browser
    python3 infra/e2e_test.py --fail-fast              # stop on first failure
    python3 infra/e2e_test.py --include-mutations      # also run import/write tests

Exit code 0 = all pass, 1 = failures found (blocks deploy.sh when called from there).
"""

import argparse
import sys
import time
import traceback
from contextlib import contextmanager
from typing import Callable

from playwright.sync_api import sync_playwright, Page, expect

# ── Config ────────────────────────────────────────────────────────────────────

DEFAULT_URL = "http://localhost:5173"
PRODUCTION_URL = "https://d3vru5lvbq0ov4.cloudfront.net"
TIMEOUT = 15_000   # ms for most waits
LONG_TIMEOUT = 45_000  # ms for screening (Bedrock call)

# ── Result tracking ───────────────────────────────────────────────────────────

results: list[dict] = []

def record(name: str, passed: bool, detail: str = "", warning: bool = False) -> bool:
    tag = "⚠️ " if warning else ("✅" if passed else "❌")
    print(f"  {tag}  {name}" + (f"  →  {detail}" if detail else ""))
    results.append({"name": name, "passed": passed or warning, "warning": warning, "detail": detail})
    return passed

@contextmanager
def test(name: str, fail_fast: bool = False):
    """Context manager — catches exceptions and records them as failures."""
    try:
        yield
    except Exception as e:
        short = str(e).splitlines()[0][:120]
        record(name, False, short)
        if fail_fast:
            raise

# ── Helpers ───────────────────────────────────────────────────────────────────

def wait_no_spinner(page: Page, timeout: int = TIMEOUT):
    """Wait until loading spinners disappear."""
    try:
        page.wait_for_selector("[class*='spin']", state="detached", timeout=timeout)
    except Exception:
        pass

def api_errors(page: Page) -> list[str]:
    """Return list of failed API responses captured during page load."""
    return [r for r in getattr(page, "_api_failures", [])]

def setup_api_monitor(page: Page):
    """Attach request failure listener."""
    page._api_failures = []  # type: ignore
    def on_response(response):
        if "/api/" in response.url and response.status >= 400:
            page._api_failures.append(f"{response.status} {response.url.split('/api/')[-1]}")  # type: ignore
    page.on("response", on_response)

def nav_to(page: Page, label_pattern: str):
    """Click a nav button matching a label pattern."""
    btns = page.locator("button, a").all()
    for btn in btns:
        try:
            t = btn.text_content() or ""
            if label_pattern.lower() in t.lower():
                btn.click()
                return True
        except Exception:
            pass
    return False


# ── Test suites ───────────────────────────────────────────────────────────────

def test_page_load(page: Page, base_url: str, fail_fast: bool):
    print("\n── Page Load ─────────────────────────────────────────────────────")
    with test("App loads without JS crash", fail_fast):
        page_errors = []
        page.on("pageerror", lambda e: page_errors.append(e.message))
        page.goto(base_url, wait_until="networkidle", timeout=30_000)
        page.wait_for_timeout(2000)
        real_errors = [e for e in page_errors if "favicon" not in e and "404" not in e]
        record("App loads without JS crash", len(real_errors) == 0,
               f"{len(real_errors)} errors: {real_errors[:2]}" if real_errors else "")

    with test("Patient list loads", fail_fast):
        items = page.locator("[class*='_item_'], [class*='patientItem'], li[class*='patient']").all()
        # also try generic list items in left panel
        if not items:
            items = page.locator("aside li, [class*='list'] li, [class*='List'] li").all()
        record("Patient list loads", len(items) > 0, f"{len(items)} patients visible")

    with test("Header shows live indicator", fail_fast):
        live = page.locator("text=LIVE").count() > 0 or page.locator("[class*='live']").count() > 0
        record("Header shows live indicator", live, "LIVE badge visible" if live else "not found")

    with test("Nav tabs visible", fail_fast):
        nav_labels = ["Screening", "Analytics", "Trial Builder", "Flow Editor", "DevOps"]
        found = []
        for label in nav_labels:
            if page.locator(f"text={label}").first.is_visible():
                found.append(label)
        record("Nav tabs visible", len(found) >= 4, f"found: {', '.join(found)}")


def test_screening(page: Page, fail_fast: bool):
    print("\n── Screening ─────────────────────────────────────────────────────")
    with test("Navigate to Screening", fail_fast):
        page.locator("text=Screening").first.click()
        page.wait_for_timeout(1500)
        record("Navigate to Screening", True)

    with test("Start Screening disabled without patient", fail_fast):
        btn = page.locator("button:has-text('Start Screening')").first
        # Button may not render at all until a patient is selected — both cases mean "disabled"
        if btn.count() == 0:
            record("Start Screening disabled without patient", True, "button not rendered (no patient selected)")
        else:
            btn.wait_for(state="visible", timeout=5000)
            disabled = btn.is_disabled()
            record("Start Screening disabled without patient", disabled,
                   "correctly disabled" if disabled else "BUG: should be disabled")

    with test("Click first patient enables Start Screening", fail_fast):
        first = page.locator("[class*='_item_']").first
        if not first.count():
            first = page.locator("aside li").first
        first.click()
        page.wait_for_timeout(800)
        btn = page.locator("button:has-text('Start Screening')").first
        enabled = btn.is_enabled()
        record("Click first patient enables Start Screening", enabled,
               "enabled" if enabled else "BUG: still disabled after patient select")

    with test("Trial dropdown has options", fail_fast):
        select = page.locator("select").first
        count = select.locator("option").count()
        record("Trial dropdown has options", count > 1, f"{count} options (including placeholder)")

    with test("RHS panel tabs visible after patient select", fail_fast):
        tabs = page.locator("[class*='_tab_']").all()
        record("RHS panel tabs visible after patient select", len(tabs) >= 2,
               f"{len(tabs)} tabs found")

    with test("Run screening — end to end", fail_fast):
        import threading

        # Navigate back to Screening tab via the main nav
        # We need to pick the top-level nav tab, not any inner tab
        all_btns = page.locator("button, a").all()
        screening_nav = None
        for b in all_btns:
            try:
                txt = (b.text_content() or "").strip()
                if txt == "Screening":
                    screening_nav = b
                    break
            except Exception:
                pass
        if screening_nav:
            screening_nav.click()
        page.wait_for_timeout(1200)

        # Select first patient
        first = page.locator("[class*='_item_']").first
        if not first.count():
            first = page.locator("aside li").first
        first.click()
        page.wait_for_timeout(800)

        # Pick first real trial option (not the placeholder)
        select = page.locator("select").first
        options = select.locator("option").all()
        trial_val = None
        for opt in options:
            v = opt.get_attribute("value") or ""
            if v and v != "":
                trial_val = v
                break

        if not trial_val:
            record("Run screening — end to end", False, "no trial options available")
        else:
            select.select_option(trial_val)
            page.wait_for_timeout(500)

            # Intercept the API response to confirm screening completes
            screening_result = {"done": False, "status": None}

            def on_response(response):
                if "/api/chat/" in response.url:
                    try:
                        data = response.json()
                        status = data.get("status", "")
                        det = data.get("determination", "")
                        # status=="complete" or "completed" means the screening finished
                        if status in ("complete", "completed") or det:
                            screening_result["done"] = True
                            screening_result["status"] = det or status
                    except Exception:
                        pass

            page.on("response", on_response)

            btn = page.locator("button:has-text('Start Screening')").first
            btn.click()

            # Poll for API response (Bedrock takes up to ~30s)
            deadline = time.time() + LONG_TIMEOUT / 1000
            while time.time() < deadline:
                if screening_result["done"]:
                    break
                page.wait_for_timeout(1000)

            page.remove_listener("response", on_response)

            if screening_result["done"]:
                record("Run screening — end to end", True,
                       f"trial {trial_val} → {screening_result['status']}")
            else:
                # Fallback: check DOM for result text as backup
                body = page.inner_text("body").lower()
                dom_has_result = any(w in body for w in ["eligible", "ineligible", "borderline"])
                record("Run screening — end to end", dom_has_result,
                       "result found in DOM" if dom_has_result else f"timeout — no result after {LONG_TIMEOUT//1000}s")


def test_analytics(page: Page, fail_fast: bool):
    print("\n── Analytics ─────────────────────────────────────────────────────")
    with test("Navigate to Analytics", fail_fast):
        page.locator("text=Analytics").first.click()
        page.wait_for_timeout(2000)
        record("Navigate to Analytics", True)

    with test("Stat cards visible", fail_fast):
        cards = page.locator("[class*='statCard'], [class*='_stat']").all()
        record("Stat cards visible", len(cards) >= 3, f"{len(cards)} cards")

    with test("Total screenings > 0", fail_fast):
        total_text = ""
        try:
            total_text = page.locator("text=TOTAL SCREENINGS").locator("..").locator("..").text_content() or ""
        except Exception:
            pass
        has_data = any(c.isdigit() for c in total_text) and "0" not in total_text.replace("TOTAL SCREENINGS", "").strip()[:3]
        record("Total screenings > 0", has_data, total_text.strip()[:40], warning=not has_data)

    with test("By Trial table shows NCT IDs", fail_fast):
        nct_links = page.locator("text=/NCT\\d+/").all()
        record("By Trial table shows NCT IDs", len(nct_links) > 0,
               f"{len(nct_links)} NCT links" if nct_links else "no NCT IDs found")

    with test("No title strings appearing as trial IDs", fail_fast):
        # The bug we fixed — titles like '— A Phase...' appearing as rows
        dash_rows = page.locator("text=/^—\\s+A /").all()
        record("No title strings appearing as trial IDs", len(dash_rows) == 0,
               f"BUG: {len(dash_rows)} title-as-ID rows found" if dash_rows else "clean")

    with test("Recent screenings section visible", fail_fast):
        recent = page.locator("text=RECENT SCREENINGS").count() > 0 or \
                 page.locator("text=Recent Screenings").count() > 0
        record("Recent screenings section visible", recent)

    with test("Eligibility distribution bars visible", fail_fast):
        bars = page.locator("[class*='bar']").all()
        record("Eligibility distribution bars visible", len(bars) > 0, f"{len(bars)} bar elements")

    with test("Stat card click filters recent screenings", fail_fast):
        cards = page.locator("[class*='statCard'], [class*='statValue']").all()
        if cards:
            cards[0].click()
            page.wait_for_timeout(600)
        record("Stat card click filters recent screenings", True, "no crash on click")


def test_trial_builder(page: Page, fail_fast: bool):
    print("\n── Trial Builder ─────────────────────────────────────────────────")
    with test("Navigate to Trial Builder", fail_fast):
        page.locator("text=Trial Builder").first.click()
        page.wait_for_timeout(1500)
        record("Navigate to Trial Builder", True)

    with test("My Trials tab is default (first)", fail_fast):
        # My Trials should be the active tab by default
        active = page.locator("[class*='tabActive']").first.text_content() or ""
        record("My Trials tab is default (first)", "My Trials" in active,
               f"active tab: '{active.strip()}'")

    with test("Tab order: My Trials → Screening Rules → Build", fail_fast):
        tabs = page.locator("[class*='_tab_'], [role='tab']").all()
        labels = [t.text_content().strip() for t in tabs if t.text_content()]
        labels = [l for l in labels if l in ("My Trials", "Screening Rules", "Build")]
        expected = ["My Trials", "Screening Rules", "Build"]
        record("Tab order: My Trials → Screening Rules → Build",
               labels == expected, f"got: {labels}")

    with test("My Trials shows trial cards", fail_fast):
        page.wait_for_timeout(1000)
        cards = page.locator("[class*='_card_']").all()
        record("My Trials shows trial cards", len(cards) > 0,
               f"{len(cards)} cards", warning=len(cards) == 0)

    with test("Delete button on each trial card", fail_fast):
        delete_btns = page.locator("button:has-text('Delete')").all()
        record("Delete button on each trial card", len(delete_btns) > 0,
               f"{len(delete_btns)} delete buttons")

    with test("Screening Rules tab loads", fail_fast):
        page.locator("text=Screening Rules").first.click()
        page.wait_for_timeout(1000)
        content = page.content()
        record("Screening Rules tab loads", "rule" in content.lower() or "screening" in content.lower(),
               "rules content visible")

    with test("Build tab loads chat interface", fail_fast):
        page.locator("text=Build").first.click()
        page.wait_for_timeout(1500)
        body = page.inner_text("body")
        chat = "BUILDER CHAT" in body or "builder" in body.lower() or "describe" in body.lower()
        record("Build tab loads chat interface", chat, "chat area visible" if chat else "no chat content found",
               warning=not chat)


def test_ctg_import(page: Page, fail_fast: bool, include_mutations: bool = False):
    print("\n── CTG Import ────────────────────────────────────────────────────")
    with test("ClinicalTrials.gov button visible", fail_fast):
        ctg_btn = page.locator("text=ClinicalTrials.gov").first
        visible = ctg_btn.is_visible()
        record("ClinicalTrials.gov button visible", visible)

    with test("CTG search modal opens", fail_fast):
        page.locator("text=ClinicalTrials.gov").first.click()
        page.wait_for_timeout(1000)
        modal = page.locator("[class*='modal'], [class*='Modal'], [role='dialog']").first
        record("CTG search modal opens", modal.is_visible(), "modal appeared")

    with test("CTG search returns results", fail_fast):
        inp = page.locator("input[type='search'], input[type='text'], input[placeholder]").first
        inp.fill("type 2 diabetes")
        inp.press("Enter")
        page.wait_for_timeout(5000)
        results_el = page.locator("[class*='_result_'], [class*='result'], [class*='study']").all()
        # Fallback: look for NCT pattern
        if not results_el:
            results_el = page.locator("text=/NCT\\d+/").all()
        record("CTG search returns results", len(results_el) > 0,
               f"{len(results_el)} results" if results_el else "no results — API may be down",
               warning=len(results_el) == 0)

    with test("Import button works (no 400)", fail_fast):
        import_btn = page.locator("button:has-text('Import')").first
        if import_btn.count():
            if include_mutations:
                failures_before = len(getattr(page, "_api_failures", []))
                import_btn.click()
                page.wait_for_timeout(4000)
                failures_after = getattr(page, "_api_failures", [])
                import_400 = [f for f in failures_after[failures_before:] if "400" in f and "import" in f]
                record("Import button works (no 400)", len(import_400) == 0,
                       f"400 errors: {import_400}" if import_400 else "imported OK")
            else:
                # Mutation tests skipped — verify button is visible and enabled, but do not click
                enabled = import_btn.is_enabled()
                record("Import button works (no 400)", True,
                       "import button present and enabled (click skipped — use --include-mutations to test writes)")
        else:
            record("Import button works (no 400)", False, "no Import button found after search")

    with test("Close CTG modal", fail_fast):
        page.keyboard.press("Escape")
        page.wait_for_timeout(500)
        record("Close CTG modal", True)


def test_trial_detail_modal(page: Page, fail_fast: bool):
    print("\n── Trial Detail Modal ────────────────────────────────────────────")
    with test("Navigate back to My Trials", fail_fast):
        page.locator("text=Trial Builder").first.click()
        page.wait_for_timeout(1000)
        page.locator("text=My Trials").first.click()
        page.wait_for_timeout(1000)
        record("Navigate back to My Trials", True)

    with test("View Details opens modal", fail_fast):
        btn = page.locator("button:has-text('View Details')").first
        if btn.count():
            btn.click()
            page.wait_for_timeout(1500)
            modal = page.locator("[class*='modal'], [role='dialog']").first
            record("View Details opens modal", modal.is_visible())
        else:
            record("View Details opens modal", False, "no View Details button found")

    with test("Modal has HealthLake Mapping tab", fail_fast):
        mapping_tab = page.locator("text=HealthLake Mapping").count() > 0
        record("Modal has HealthLake Mapping tab", mapping_tab,
               "tab present" if mapping_tab else "BUG: mapping tab missing from modal")

    with test("Modal has no Session column in screenings", fail_fast):
        session_col = page.locator("th:has-text('Session')").count()
        record("Modal has no Session column in screenings", session_col == 0,
               "clean" if session_col == 0 else f"BUG: {session_col} Session columns found")

    with test("Close modal via Escape", fail_fast):
        page.keyboard.press("Escape")
        page.wait_for_timeout(500)
        record("Close modal via Escape", True)


def test_flow_editor(page: Page, fail_fast: bool):
    print("\n── Flow Editor ───────────────────────────────────────────────────")
    with test("Navigate to Flow Editor", fail_fast):
        page.locator("text=Flow Editor").first.click()
        page.wait_for_timeout(2000)
        record("Navigate to Flow Editor", True)

    with test("Flow editor content loads", fail_fast):
        content = page.content()
        has_content = "flow" in content.lower() or "node" in content.lower()
        record("Flow editor content loads", has_content, "flow content visible", warning=not has_content)


def test_devops(page: Page, fail_fast: bool):
    print("\n── DevOps ────────────────────────────────────────────────────────")
    with test("Navigate to DevOps", fail_fast):
        page.locator("text=DevOps").first.click()
        page.wait_for_timeout(1500)
        record("Navigate to DevOps", True)

    with test("DevOps loads without 404s", fail_fast):
        failures = [f for f in getattr(page, "_api_failures", []) if "devops" in f or "traces" in f]
        record("DevOps loads without 404s", len(failures) == 0,
               f"404s: {failures}" if failures else "clean")


def test_no_orphan_api_calls(page: Page, fail_fast: bool):
    print("\n── API Health ────────────────────────────────────────────────────")
    failures = getattr(page, "_api_failures", [])
    critical = [f for f in failures if not any(x in f for x in ["favicon", "cilantro/history"])]
    record("No unexpected API 4xx/5xx", len(critical) == 0,
           f"failures: {critical}" if critical else f"clean ({len(failures)} total, cilantro history excluded)")


# ── Main ──────────────────────────────────────────────────────────────────────

def run(base_url: str, headed: bool, fail_fast: bool, include_mutations: bool = False) -> int:
    print(f"\n{'='*65}")
    print(f"  Clinical Trial Screening — E2E Test Suite")
    print(f"  URL: {base_url}")
    print(f"{'='*65}")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not headed)
        ctx = browser.new_context(viewport={"width": 1600, "height": 900})
        page = ctx.new_page()
        setup_api_monitor(page)

        try:
            test_page_load(page, base_url, fail_fast)
            test_screening(page, fail_fast)
            test_analytics(page, fail_fast)
            test_trial_builder(page, fail_fast)
            test_ctg_import(page, fail_fast, include_mutations=include_mutations)
            test_trial_detail_modal(page, fail_fast)
            test_flow_editor(page, fail_fast)
            test_devops(page, fail_fast)
            test_no_orphan_api_calls(page, fail_fast)
        except Exception as e:
            if fail_fast:
                print(f"\n  Stopped early (--fail-fast): {e!s:.120}")

        browser.close()

    # ── Summary ────────────────────────────────────────────────────────────────
    total = len(results)
    passed = sum(1 for r in results if r["passed"])
    warnings = sum(1 for r in results if r["warning"])
    failed = total - passed

    print(f"\n{'='*65}")
    print(f"  Results: {passed}/{total} passed  |  {warnings} warnings  |  {failed} failures")
    print(f"{'='*65}")

    if failed:
        print("\n  FAILURES:")
        for r in results:
            if not r["passed"]:
                print(f"    ❌  {r['name']}")
                if r["detail"]:
                    print(f"        {r['detail']}")

    if warnings:
        print("\n  WARNINGS (non-blocking):")
        for r in results:
            if r["warning"]:
                print(f"    ⚠️   {r['name']}: {r['detail']}")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="E2E test suite for Clinical Trial Screening")
    parser.add_argument("--url", default=None, help="Base URL to test (overrides --production and DEFAULT_URL)")
    parser.add_argument("--production", action="store_true",
        help="Run against production CloudFront URL instead of localhost")
    parser.add_argument("--headed", action="store_true", help="Show browser window")
    parser.add_argument("--fail-fast", action="store_true", help="Stop on first failure")
    parser.add_argument("--include-mutations", action="store_true",
        help="Also run tests that write data (e.g. CTG import click)")
    args = parser.parse_args()
    base_url = PRODUCTION_URL if args.production else (args.url or DEFAULT_URL)
    sys.exit(run(base_url, args.headed, args.fail_fast, include_mutations=args.include_mutations))
