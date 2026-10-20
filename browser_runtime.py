"""Managed, sandboxed Chromium with a browser version matched to Playwright."""

from playwright.sync_api import sync_playwright


def launch_browser(*, headless: bool = True):
    driver = sync_playwright().start()
    try:
        browser = driver.chromium.launch(
            channel="chromium", chromium_sandbox=True, headless=headless, timeout=60_000,
        )
    except BaseException:
        driver.stop()
        raise
    close = browser.close

    def close_managed(*, reason=None):
        try:
            if reason is None:
                close()
            else:
                close(reason=reason)
        finally:
            driver.stop()

    browser.close = close_managed
    return browser
