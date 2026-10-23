"""Exercise real Chromium against in-memory fixtures. Run with Docker --network none."""

import base64
from io import BytesIO
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch
from urllib.parse import urlsplit

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    # No real cookie files, credentials or session state may enter this test.
    credential_names = ("MY_COOKIES", "JANITORAI_EMAIL", "JANITORAI_PASSWORD",
                        "JANNYAI_EMAIL", "JANNYAI_PASSWORD", "CLOAKBROWSER_PROXY")
    with tempfile.TemporaryDirectory(prefix="browser-fixture-") as directory, patch.dict(
        os.environ, {name: "" for name in credential_names}
    ):
        previous = Path.cwd()
        os.chdir(directory)
        try:
            import parser
            from browser_runtime import launch_browser
            from download_flow import DownloadSource

            browser = launch_browser()
            try:
                page = browser.new_page()
                page.goto("chrome://sandbox")
                sandbox = page.locator("body").inner_text()
                assert "adequately sandboxed" in sandbox.lower(), sandbox
                page.goto("about:blank")
                page.set_content("<title>local-render</title>", timeout=10_000)
                assert page.title() == "local-render"
                print("SANDBOX_RENDER: OK; Chromium", browser.version)
            finally:
                browser.close()

            image = BytesIO()
            Image.new("RGB", (8, 8), "red").save(image, format="PNG")
            avatar = "data:image/png;base64," + base64.b64encode(image.getvalue()).decode()
            payload = base64.urlsafe_b64encode(json.dumps({"exp": int(time.time()) + 3600}).encode()).decode().rstrip("=")
            token = "fixture." + payload + ".not-a-real-signature"

            for mode in ("janny-api", "janny-page", "proxy", "private"):
                calls = []
                raw = {"id": "fixture", "name": "Ada", "description": "Public summary",
                       "firstMessage": "Hello", "avatarUrl": avatar, "showdefinition": False,
                       "is_public": mode != "private", "allow_proxy": True}
                if mode.startswith("janny"):
                    raw["personality"] = "Fixture definition"
                html = ('<title>Ada</title><script id="__NEXT_DATA__" type="application/json">'
                        + json.dumps({"props": {"pageProps": {"character": raw}}}) + '</script><script>'
                        + 'localStorage.setItem("sb-fixture-auth-token",'
                        + json.dumps(json.dumps({"access_token": token})) + ')</script>')

                def fake_fetch(url, *, method="GET", **kwargs):
                    parts = urlsplit(url)
                    calls.append((parts.path, method))
                    headers = {"content-type": "application/json", "access-control-allow-origin": "https://jannyai.com",
                               "access-control-allow-credentials": "true",
                               "access-control-allow-headers": "content-type,authorization",
                               "access-control-allow-methods": "POST,GET,OPTIONS"}
                    if method == "OPTIONS":
                        return 204, headers, b"", url
                    if parts.path.startswith("/characters/"):
                        return 200, {"content-type": "text/html"}, html.encode(), url
                    if parts.path == "/api/v1/download":
                        if mode == "janny-page":
                            return 403, headers, b"{}", url
                        result = {"status": "ok", "downloadUrl": "https://datacat.run/fixture.png"}
                    elif parts.path == "/fixture.png":
                        card_path = Path(directory) / "fixture-input.png"
                        parser._write_chara_png(image.getvalue(), parser._to_chara_card_v2(raw), str(card_path))
                        return 200, {"content-type": "image/png"}, card_path.read_bytes(), url
                    elif parts.path == "/mb/characters/fixture":
                        result = raw
                    elif parts.path == "/mb/chats":
                        result = {"id": 12, "user_id": "fixture-user"}
                    elif parts.path == "/mb/chats/12":
                        result = {"chatMessages": [{"id": 5, "message": "Hello", "created_at": "2026-01-01T00:00:00Z"}]}
                    elif parts.path == "/generateAlpha":
                        result = {"messages": [{"role": "system", "content": "<Ada's Persona>Fixture definition</Ada's Persona>"}]}
                    else:
                        raise AssertionError("Unexpected fixture request")
                    return 200, headers, json.dumps(result).encode(), url

                with patch("security_policy.fetch", side_effect=fake_fetch), patch.object(
                    parser, "fetch", side_effect=fake_fetch
                ), patch.object(parser, "JANITOR_STATE_PATH", Path(directory) / "session.json"), patch.dict(
                    os.environ, {"JANITOR_STORAGE_STATE_PATH": ""}
                ):
                    source = DownloadSource.JANNYAI if mode.startswith("janny") else DownloadSource.PROXY
                    downloader = parser._download_janny_api_sync if mode.startswith("janny") else parser._download_janitor_character_sync
                    if mode == "private":
                        try:
                            parser._download_candidate_sync(downloader, "fixture", source)
                        except ValueError:
                            assert not any(path == "/mb/chats" for path, _ in calls)
                        else:
                            raise AssertionError("Private character was not rejected")
                    else:
                        result = parser._download_candidate_sync(downloader, "fixture", source)
                        assert result.definition_available
                        card = parser._read_card(result.file_path)
                        assert card["data"]["personality"] == "Fixture definition"
                        Path(result.file_path).unlink()
                print(mode.upper() + ": OK (synthetic, no live service)")
        finally:
            os.chdir(previous)


if __name__ == "__main__":
    main()
