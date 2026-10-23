"""Ephemeral Docker-only public page reader (separate Camoufox environment)."""

import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))

from download_errors import SourceError, describe_error
from janny_transport import allowed_url, native_transport

BINARY = '/opt/camoufox/camoufox-bin'


def _checkbox(page):
    from Xlib import X, display
    from Xlib.ext import xtest

    connection = display.Display()
    try:
        windows = []
        for window in connection.screen().root.query_tree().children:
            geometry = window.get_geometry()
            if (window.get_attributes().map_state == X.IsViewable
                    and geometry.width > 500 and geometry.height > 400):
                windows.append((window, geometry))
        if len(windows) != 1:
            raise SourceError('browser_request_failed')
        window, geometry = windows[0]
        viewport = page.evaluate('() => ({width: innerWidth, height: innerHeight})')
        # Only the reviewed Cloudflare interstitial uses these coordinates.
        # Browser screenX/screenY may be virtualized; real X11 geometry is needed.
        left = max(32, (viewport['width'] - 896) / 2)
        x = round(geometry.x + (geometry.width - viewport['width']) / 2 + left + 21)
        y = round(geometry.y + geometry.height - viewport['height'] + 337)
        if not (0 < x < geometry.width and 0 < y < geometry.height):
            raise SourceError('browser_request_failed')
        window.set_input_focus(X.RevertToParent, X.CurrentTime)
        xtest.fake_input(connection, X.MotionNotify, x=x, y=y)
        connection.sync()
        time.sleep(.3)
        xtest.fake_input(connection, X.ButtonPress, 1)
        connection.sync()
        time.sleep(.12)
        xtest.fake_input(connection, X.ButtonRelease, 1)
        connection.sync()
    finally:
        connection.close()


def read_page(identifier):
    from camoufox import DefaultAddons
    from camoufox.fpgen_model import is_pinned
    from camoufox.sync_api import Camoufox

    # Fail closed on an incomplete image; no browser/model downloads at runtime.
    if not Path(BINARY).is_file() or not is_pinned():
        raise SourceError('browser_missing')
    url = f'https://jannyai.com/characters/{identifier}'
    with native_transport() as proxy, Camoufox(
        executable_path=BINARY, headless=False, geoip=False, block_webrtc=True,
        exclude_addons=list(DefaultAddons), timeout=60_000,
        proxy={'server': proxy},
        firefox_user_prefs={'network.trr.mode': 5, 'network.dns.disablePrefetch': True,
                            'network.prefetch-next': False, 'network.http.http3.enable': False},
    ) as browser:
        context = browser.new_context(service_workers='block', accept_downloads=False)
        count = 0

        def route(request):
            nonlocal count
            count += 1
            if count > 200 or not allowed_url(request.request.url):
                request.abort()
            else:
                request.continue_()

        context.route('**/*', route)
        context.route_web_socket('**/*', lambda socket: socket.close())
        page = context.new_page()
        page.goto(url, wait_until='domcontentloaded', timeout=45_000)
        deadline = time.monotonic() + 65
        next_click = time.monotonic() + 8
        while time.monotonic() < deadline:
            if not allowed_url(page.url) or not page.url.startswith('https://jannyai.com/characters/'):
                raise SourceError('unsafe_url')
            island = page.locator('astro-island[component-url*="CharacterButtons"][props]')
            if island.count():
                props = island.first.get_attribute('props') or ''
                if len(props) > 1_900_000:
                    raise SourceError('response_too_large')
                return {'url': page.url, 'props': props}
            if page.title() == 'Just a moment...' and time.monotonic() >= next_click:
                _checkbox(page)
                next_click = time.monotonic() + 12
            page.wait_for_timeout(1000)
        raise SourceError('challenge_required')


def main():
    from janny_public import _identifier

    identifier, output = sys.argv[1:]
    _identifier(identifier)
    path = Path(output)
    if path.name != 'public.json' or path.resolve().parent != Path.cwd().resolve():
        return 1
    try:
        state = read_page(identifier)
    except Exception as error:
        path.write_text(json.dumps({'error': describe_error(error).to_payload()}), encoding='utf-8')
        return 1
    path.write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')
    path.chmod(0o600)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
