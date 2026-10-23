"""Managed, sandboxed Chromium with a browser version matched to Playwright."""

from playwright.sync_api import sync_playwright

from download_errors import SourceError


def launch_browser(*, headless: bool = True):
    try:
        driver = sync_playwright().start()
    except Exception as exc:
        raise SourceError("browser_launch_failed", error_type=type(exc).__name__) from None
    try:
        browser = driver.chromium.launch(
            channel="chromium", chromium_sandbox=True, headless=headless, timeout=60_000,
        )
    except BaseException as exc:
        try:
            driver.stop()
        except Exception:
            pass  # Preserve the launch failure instead of a secondary cleanup error.
        if not isinstance(exc, Exception):
            raise
        text = str(exc)
        code = ("browser_missing" if "Executable doesn't exist" in text else
                "browser_sandbox_failed" if "No usable sandbox!" in text or "Chromium sandboxing failed!" in text
                else "browser_launch_failed")
        raise SourceError(code, error_type=type(exc).__name__) from None
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
