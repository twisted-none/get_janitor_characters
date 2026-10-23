import unittest
from unittest.mock import Mock, patch

import browser_runtime
from download_errors import SourceError


class BrowserRuntimeTests(unittest.TestCase):
    def test_driver_cleanup_does_not_mask_launch_failure(self):
        driver = Mock()
        driver.chromium.launch.side_effect = RuntimeError("Executable doesn't exist: fixture-secret")
        driver.stop.side_effect = OSError("fixture-cleanup-secret")
        with patch.object(browser_runtime, "sync_playwright") as playwright:
            playwright.return_value.start.return_value = driver
            with self.assertRaises(SourceError) as caught:
                browser_runtime.launch_browser()
        self.assertEqual(caught.exception.code, "browser_missing")
        self.assertNotIn("fixture", str(caught.exception))

    def test_interrupted_launch_stops_driver(self):
        driver = Mock()
        driver.chromium.launch.side_effect = KeyboardInterrupt()
        with patch.object(browser_runtime, "sync_playwright") as playwright:
            playwright.return_value.start.return_value = driver
            with self.assertRaises(KeyboardInterrupt):
                browser_runtime.launch_browser()
        driver.stop.assert_called_once_with()

    def test_missing_browser_has_explicit_code_without_raw_message(self):
        driver = Mock()
        driver.chromium.launch.side_effect = RuntimeError("Executable doesn't exist: fixture-secret")
        with patch.object(browser_runtime, "sync_playwright") as playwright:
            playwright.return_value.start.return_value = driver
            with self.assertRaises(SourceError) as caught:
                browser_runtime.launch_browser()
        self.assertEqual(caught.exception.code, "browser_missing")
        self.assertNotIn("fixture-secret", str(caught.exception))
        driver.stop.assert_called_once_with()

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
            with self.assertRaises(SourceError) as caught:
                browser_runtime.launch_browser()
        self.assertEqual(caught.exception.code, "browser_launch_failed")
        self.assertEqual(caught.exception.error_type, "RuntimeError")
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
