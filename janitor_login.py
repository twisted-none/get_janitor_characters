"""Save a Janitor browser session for the Docker bot."""

from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

from janitor_proxy import get_access_token  # noqa: E402
from parser import _janitor_state_path, _login_janitor, _new_janitor_context, _save_janitor_state, _launch  # noqa: E402


def main() -> None:
    browser = _launch(headless=False)
    try:
        context = _new_janitor_context(browser)
        page = context.new_page()
        _login_janitor(page)
        if not get_access_token(page, context):
            raise ValueError("Janitor не выдал действующую сессию.")
        _save_janitor_state(context)
        print(f"Сессия Janitor сохранена: {_janitor_state_path()}")
    finally:
        browser.close()


if __name__ == "__main__":
    main()
