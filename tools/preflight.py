"""Offline configuration checks. Never load or print a dotenv file."""

import argparse
import os
from pathlib import Path
import sys


def check_environment(environment):
    findings = [(name, bool(environment.get(name, "").strip()))
                for name in ("BOT_TOKEN", "DATABASE_URL")]
    for prefix in ("JANITORAI", "JANNYAI"):
        email = bool(environment.get(f"{prefix}_EMAIL", "").strip())
        password = bool(environment.get(f"{prefix}_PASSWORD", ""))
        findings.append((f"{prefix}_CREDENTIAL_PAIR", email == password))
    for name, default in (("DOWNLOAD_CONCURRENCY", "1"), ("DOWNLOAD_TIMEOUT_SECONDS", "3600")):
        try:
            valid = int(environment.get(name, default)) > 0
        except (ValueError, TypeError):
            valid = False
        findings.append((name, valid))
    findings.append(("NO_UNSUPPORTED_TRANSPORT_PROXY", not bool(environment.get("CLOAKBROWSER_PROXY"))))
    return findings


def main():
    arguments = argparse.ArgumentParser(description=__doc__)
    arguments.add_argument("--browser", action="store_true", help="Also render a local page with sandbox")
    args = arguments.parse_args()
    findings = check_environment(os.environ)
    root = Path(__file__).resolve().parents[1]
    session = Path(os.environ.get("JANITOR_STORAGE_STATE_PATH") or root / ".cache/janitor_auth_state.json")
    print("SESSION_FILE:", "PRESENT_NOT_VALIDATED" if session.is_file() else "ABSENT")
    if args.browser:
        sys.path.insert(0, str(root))
        from browser_runtime import launch_browser
        try:
            browser = launch_browser()
            try:
                page = browser.new_page(service_workers="block")
                page.set_content("<title>preflight</title><p>local</p>", timeout=10_000)
                findings.append(("BROWSER_SANDBOX_RENDER", page.title() == "preflight"))
            finally:
                browser.close()
        except Exception:
            findings.append(("BROWSER_SANDBOX_RENDER", False))
    for name, valid in findings:
        print(f"{name}: {'OK' if valid else 'FAIL'}")
    return 0 if all(valid for _, valid in findings) else 1


if __name__ == "__main__":
    raise SystemExit(main())
