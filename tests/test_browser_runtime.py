import unittest
from unittest.mock import Mock, patch

import browser_runtime


class BrowserRuntimeTests(unittest.TestCase):
    def test_chromium_sandbox_and_driver_cleanup(self):
        driver = Mock()
        browser = driver.chromium.launch.return_value
        original_close = browser.close
        with patch.object(browser_runtime, "sync_playwright") as playwright:
            playwright.return_value.start.return_value = driver
            result = browser_runtime.launch_browser()
        driver.chromium.launch.assert_called_once_with(
            channel="chromium", chromium_sandbox=True, headless=True, timeout=60_000,
        )
        result.close()
        original_close.assert_called_once_with()
        driver.stop.assert_called_once_with()

    def test_launch_failure_stops_driver(self):
        driver = Mock()
        driver.chromium.launch.side_effect = RuntimeError("fixture")
        with patch.object(browser_runtime, "sync_playwright") as playwright:
            playwright.return_value.start.return_value = driver
            with self.assertRaises(RuntimeError):
                browser_runtime.launch_browser()
        driver.stop.assert_called_once_with()

    def test_close_failure_still_stops_driver(self):
        driver = Mock()
        driver.chromium.launch.return_value.close.side_effect = RuntimeError("fixture")
        with patch.object(browser_runtime, "sync_playwright") as playwright:
            playwright.return_value.start.return_value = driver
            browser = browser_runtime.launch_browser(headless=False)
        with self.assertRaises(RuntimeError):
            browser.close()
        driver.stop.assert_called_once_with()
