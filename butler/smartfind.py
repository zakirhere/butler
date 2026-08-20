"""SmartFind Express availability polling and explicitly opt-in acceptance."""

from __future__ import annotations

import logging
import re
import subprocess
import time
from dataclasses import dataclass

from playwright.sync_api import Page, sync_playwright

from butler.config import settings
from butler.slack import notify

log = logging.getLogger(__name__)
AVAILABLE_URL = "https://milpitas.eschoolsolutions.com/ui/#/substitute/jobs/available"
LOGIN_URL = "https://milpitas.eschoolsolutions.com/logOnInitAction.do"
BROWSER_PATH = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


@dataclass
class Job:
    date: str
    time: str
    employee: str
    classification: str
    location: str


def _notify(message: str) -> bool:
    return notify(message, channel=settings.smartfind_slack_channel_id or settings.slack_channel_id)


def _password() -> str:
    result = subprocess.run(
        [
            "/usr/bin/security",
            "find-generic-password",
            "-s",
            settings.smartfind_keychain_service,
            "-a",
            settings.smartfind_keychain_account,
            "-w",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    return result.stdout.strip()


def _logged_in(page: Page) -> bool:
    return page.locator("#userId").count() == 0 and "SmartFind" in page.locator("body").inner_text()


def _login_if_needed(page: Page) -> bool:
    if _logged_in(page):
        return True
    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
    page.locator("#userId").fill(settings.smartfind_keychain_account)
    page.locator("#userPin").fill(_password())
    # SmartFind may require CAPTCHA after a session expires. Never attempt to bypass it.
    if page.locator("#captcha, [name*=captcha i], iframe[src*=captcha i]").count():
        _notify("⚠️ *SmartFind needs manual CAPTCHA/login*\nThe auto-accept worker is paused.")
        return False
    page.locator("#submitBtn").click()
    page.wait_for_timeout(3000)
    if not _logged_in(page):
        _notify("⚠️ *SmartFind login failed or needs CAPTCHA*\nThe auto-accept worker is paused.")
        return False
    return True


def _read_jobs(page: Page) -> list[tuple[object, Job]]:
    jobs: list[tuple[object, Job]] = []
    # SmartFind renders each listing as a table row. Keep selectors broad because
    # the vendor has changed its Angular markup between deployments.
    rows = page.locator("tr").filter(has=page.locator("button"))
    for index in range(rows.count()):
        row = rows.nth(index)
        text = " ".join(row.inner_text(timeout=3000).split())
        if not text or "Accept" not in text:
            continue
        cells = [" ".join(cell.inner_text(timeout=1000).split()) for cell in row.locator("td").all()]
        if len(cells) < 5:
            cells = text.split(" | ")
        jobs.append(
            (
                row,
                Job(
                    date=cells[0] if len(cells) > 0 else text,
                    time=cells[1] if len(cells) > 1 else "",
                    employee=cells[2] if len(cells) > 2 else "",
                    classification=cells[3] if len(cells) > 3 else "",
                    location=cells[4] if len(cells) > 4 else "",
                ),
            )
        )
    return jobs


def _job_text(job: Job) -> str:
    return f"{job.date} · {job.time} · {job.employee} · {job.classification} · {job.location}"


def _body_text(page: Page) -> str:
    return " ".join(page.locator("body").inner_text(timeout=5000).split())


def _success_visible(page: Page) -> bool:
    return bool(re.search(r"job\s*(?:id\s*)?.*success|success.*job\s*(?:id)?", _body_text(page), re.I))


def _contention_visible(page: Page) -> bool:
    return bool(
        re.search(
            r"system\s+is\s+calling\s+other\s+people|another\s+substitute|already\s+being\s+accepted",
            _body_text(page),
            re.I,
        )
    )


def _popup_accept(page: Page):
    """Return an Accept button inside a visible contention dialog, if any."""
    for selector in ('[role="dialog"]', ".modal-dialog", ".modal", '[aria-modal="true"]'):
        dialogs = page.locator(selector).filter(has_text=re.compile("system|calling|other|substitute", re.I))
        for index in range(dialogs.count()):
            dialog = dialogs.nth(index)
            if dialog.is_visible():
                button = dialog.get_by_role("button", name="Accept", exact=True)
                if button.count() and button.last.is_visible():
                    return button.last
    return None


def _accept_until_success(page: Page, row, job: Job) -> bool:
    row.get_by_text("Accept", exact=True).click(timeout=5000)
    for attempt in range(settings.smartfind_accept_max_retries + 1):
        if _success_visible(page):
            return True
        if _contention_visible(page):
            button = _popup_accept(page)
            if button is None:
                log.warning("SmartFind contention message has no visible popup Accept button")
            else:
                log.info(
                    "SmartFind contention retry %d/%d for %s",
                    attempt + 1,
                    settings.smartfind_accept_max_retries,
                    _job_text(job),
                )
                button.click(timeout=5000)
        if attempt < settings.smartfind_accept_max_retries:
            time.sleep(max(1, settings.smartfind_accept_retry_seconds))
    return _success_visible(page)


def _scan_page(page: Page) -> dict:
    """Refresh and inspect one already-open authenticated browser page."""
    # A full navigation is intentional: SmartFind's Available Jobs grid can
    # remain stale unless the route is refreshed before each poll.
    page.goto(AVAILABLE_URL, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(2500)
    if not _login_if_needed(page):
        return {"enabled": True, "available": 0, "accepted": 0, "paused": True}
    page.goto(AVAILABLE_URL, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(2500)
    if not _logged_in(page):
        _notify("⚠️ *SmartFind session expired*\nThe auto-accept worker is paused.")
        return {"enabled": True, "available": 0, "accepted": 0, "paused": True}

    accepted = 0
    jobs = _read_jobs(page)
    if not jobs:
        log.info("SmartFind: no available jobs")
        return {"enabled": True, "available": 0, "accepted": 0}
    _notify(
        "📚 *SmartFind jobs detected*\n"
        + "\n".join(f"• {_job_text(job)}" for _, job in jobs)
    )
    if not settings.smartfind_auto_accept:
        return {"enabled": True, "available": len(jobs), "accepted": 0, "dry_run": True}

    for row, job in jobs:
        try:
            if _accept_until_success(page, row, job):
                accepted += 1
                _notify(f"✅ *SmartFind job accepted*\n• {_job_text(job)}")
            else:
                _notify(
                    f"⚠️ *SmartFind acceptance did not reach success*\n"
                    f"• {_job_text(job)}\n"
                    f"Stopped after {settings.smartfind_accept_max_retries} retries."
                )
        except Exception as exc:
            log.exception("SmartFind acceptance failed for %s: %s", _job_text(job), exc)
            _notify(f"⚠️ *SmartFind acceptance needs attention*\n• {_job_text(job)}")
    return {"enabled": True, "available": len(jobs) if 'jobs' in locals() else 0, "accepted": accepted}


def scan_once() -> dict:
    if not settings.smartfind_enabled:
        return {"enabled": False, "available": 0, "accepted": 0}
    if not settings.slack_bot_token and not settings.slack_webhook_url:
        raise RuntimeError("SmartFind requires Slack configuration")
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            settings.smartfind_browser_profile,
            headless=True,
            executable_path=BROWSER_PATH,
            locale="en-US",
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            return _scan_page(page)
        finally:
            context.close()


def run_forever() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    while True:
        try:
            with sync_playwright() as playwright:
                context = playwright.chromium.launch_persistent_context(
                    settings.smartfind_browser_profile,
                    headless=True,
                    executable_path=BROWSER_PATH,
                    locale="en-US",
                )
                page = context.pages[0] if context.pages else context.new_page()
                try:
                    while True:
                        log.info("SmartFind scan result: %s", _scan_page(page))
                        time.sleep(max(60, settings.smartfind_poll_seconds))
                finally:
                    context.close()
        except Exception:
            log.exception("SmartFind browser loop failed; will relaunch")
            time.sleep(60)


if __name__ == "__main__":
    run_forever()
