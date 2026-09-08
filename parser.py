import asyncio
import base64
import binascii
import ipaddress
from io import BytesIO
import json
import logging
import os
import socket
import tempfile
import time
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import urljoin, urlparse

from cloakbrowser import launch
from PIL import Image, PngImagePlugin

GLOBAL_COOKIES: dict[str, str] = {}
AUTH_COOKIE_NAMES = {"cf_clearance", "sb-access-token", "sb-refresh-token"}
JSON_RESPONSE_LIMIT = 2_000_000
IMAGE_RESPONSE_LIMIT = 25_000_000
DATACAT_BASE = "https://datacat.run"
DATACAT_JANITOR_IMAGE_BASE = "https://ella.janitorai.com/bot-avatars/"

API_FETCH_SCRIPT = """
async ({ url, characterId, headers }) => {
  try {
    const response = await fetch(url, {
      method: "POST",
      headers,
      credentials: "include",
      body: JSON.stringify({ characterId }),
    });
    return { ok: true, status: response.status, body: await response.text() };
  } catch (error) {
    return { ok: false, message: String(error) };
  }
}
"""

DOWNLOAD_FETCH_SCRIPT = """
async (url) => {
  const response = await fetch(url, { credentials: "include" });
  const buffer = await response.arrayBuffer();
  const bytes = new Uint8Array(buffer);
  let binary = "";
  for (let i = 0; i < bytes.length; i += 32768) {
    binary += String.fromCharCode(...bytes.subarray(i, i + 32768));
  }
  return { status: response.status, body: btoa(binary) };
}
"""

JANITOR_STATE_SCRIPT = """
() => {
  const scripts = Array.from(document.scripts)
    .map((script) => ({
      id: script.id || "",
      type: script.type || "",
      text: script.textContent || "",
    }))
    .filter((script) =>
      script.id === "__NEXT_DATA__" ||
      script.type.includes("json") ||
      script.text.includes("first") ||
      script.text.includes("personality") ||
      script.text.includes("scenario")
    )
    .map((script) => ({ ...script, text: script.text.slice(0, 2000000) }));
  const meta = Object.fromEntries(
    Array.from(document.querySelectorAll("meta[property],meta[name]")).map((node) => [
      node.getAttribute("property") || node.getAttribute("name"),
      node.getAttribute("content") || "",
    ])
  );
  const imageNodes = Array.from(document.querySelectorAll("img, source, [data-src], [data-background-image], [style*='url(']"));
  const images = imageNodes
    .flatMap((node) => {
      const srcset = (node.getAttribute("srcset") || "")
        .split(",")
        .map((item) => item.trim().split(/\s+/)[0])
        .filter(Boolean);
      const background = (node.style && node.style.backgroundImage || "")
        .match(/url\\([\"']?([^\"')]+)[\"']?\\)/g)?.map((item) => item.replace(/^url\\([\"']?|[\"']?\\)$/g, "")) || [];
      const src = node.currentSrc || node.src || node.getAttribute("src") || node.getAttribute("data-src") ||
        node.getAttribute("data-background-image") || "";
      return [src, ...srcset, ...background].filter(Boolean).map((url) => ({
        src: url,
        alt: node.getAttribute("alt") || "",
        className: node.getAttribute("class") || "",
        width: node.naturalWidth || node.width || 0,
        height: node.naturalHeight || node.height || 0,
      }));
    })
    .slice(0, 100);
  return {
    bodyText: (document.body && document.body.innerText || "").slice(0, 5000),
    images,
    meta,
    scripts,
    title: document.title || "",
  };
}
"""


def load_data() -> None:
    raw_cookies = ""
    if os.path.exists("current_cookies.txt"):
        with open("current_cookies.txt", "r", encoding="utf-8") as f:
            raw_cookies = f.read()
    else:
        raw_cookies = os.getenv("MY_COOKIES", "")

    if raw_cookies.strip():
        update_global_cookies(raw_cookies)


def update_global_cookies(raw_string: str) -> None:
    global GLOBAL_COOKIES

    GLOBAL_COOKIES = {}
    raw_string = raw_string.strip().strip("'").strip('"')
    for item in raw_string.split(";"):
        if "=" not in item:
            continue
        key, value = item.strip().split("=", 1)
        if key:
            GLOBAL_COOKIES[key] = value
    logging.info("Cookies загружены: %s параметров.", len(GLOBAL_COOKIES))


def _browser_cookies() -> list[dict[str, Any]]:
    domains = (".jannyai.com", "api.jannyai.com", ".janitorai.com")
    return [
        {"name": name, "value": value, "domain": domain, "path": "/", "secure": True}
        for domain in domains
        for name, value in GLOBAL_COOKIES.items()
        if value and _cookie_is_usable(name, value)
    ]


def _cookie_is_usable(name: str, value: str) -> bool:
    if name != "sb-access-token" or value.count(".") != 2:
        return True
    try:
        encoded = value.split(".", 2)[1]
        encoded += "=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(encoded))
        return int(payload.get("exp", 0)) > int(time.time()) + 30
    except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError, binascii.Error):
        return False


def _cookie_header() -> str:
    return "; ".join(
        f"{name}={value}"
        for name, value in GLOBAL_COOKIES.items()
        if value and _cookie_is_usable(name, value)
    )


def _browser_headers() -> dict[str, str]:
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    access_token = GLOBAL_COOKIES.get("sb-access-token", "")
    if access_token and _cookie_is_usable("sb-access-token", access_token):
        headers["Authorization"] = f"Bearer {access_token}"
    return headers


def _sync_context_cookies(context: Any) -> None:
    for cookie in context.cookies():
        name = str(cookie.get("name", ""))
        value = str(cookie.get("value", ""))
        if name and value:
            GLOBAL_COOKIES[name] = value


def _request_headers(page_url: str, user_agent: str) -> dict[str, str]:
    headers = {
        **_browser_headers(),
        "Origin": "https://jannyai.com",
        "Referer": page_url,
        "User-Agent": user_agent,
        "sec-ch-ua-platform": '"Windows"',
    }
    cookie_header = _cookie_header()
    if cookie_header:
        headers["Cookie"] = cookie_header
    return headers


def _launch() -> Any:
    proxy = os.getenv("CLOAKBROWSER_PROXY")
    args = ["--disable-web-security", "--disable-features=IsolateOrigins,site-per-process"]
    logging.info("Запускаем CloakBrowser: proxy=%s, cookies=%s", bool(proxy), len(GLOBAL_COOKIES))
    return launch(proxy=proxy, args=args, timeout=60_000) if proxy else launch(args=args, timeout=60_000)


def _post_download_api(context: Any, api_url: str, page_url: str, character_id: str, user_agent: str) -> tuple[int, str]:
    response = context.request.post(
        api_url,
        data=json.dumps({"characterId": character_id}),
        headers=_request_headers(page_url, user_agent),
        timeout=30_000,
    )
    return response.status, response.text()


def _capture_json_response(response: Any, payloads: list[Any], hosts: tuple[str, ...]) -> None:
    try:
        content_type = response.headers.get("content-type", "")
        if not any(host in response.url.lower() for host in hosts) or "json" not in content_type:
            return
        text = response.text()
        if len(text) > JSON_RESPONSE_LIMIT:
            return
        parsed = _json_from_text(text)
        if parsed is not None:
            payloads.append(parsed)
    except Exception:
        logging.debug("Не удалось прочитать JSON response", exc_info=True)


def _download_janny_character_sync(character_id: str) -> str:
    logging.info("Старт скачивания character_id=%s", character_id)
    load_data()

    api_url = "https://api.jannyai.com/api/v1/download"
    page_url = f"https://jannyai.com/characters/{character_id}"
    uuid_part = character_id.split("_")[0]
    file_path = os.path.join(tempfile.gettempdir(), f"{character_id}.png")
    browser = _launch()
    payloads: list[Any] = []
    logging.info("CloakBrowser запущен")

    try:
        context = browser.new_context()
        if GLOBAL_COOKIES:
            logging.info("Добавляем auth cookies в browser context: %s", sorted(AUTH_COOKIE_NAMES & GLOBAL_COOKIES.keys()))
            context.add_cookies(_browser_cookies())

        page = context.new_page()
        _login_janny(page)
        _sync_context_cookies(context)
        page.on("response", lambda response: _capture_json_response(response, payloads, ("jannyai.com",)))
        logging.info("Открываем страницу через CloakBrowser: %s", page_url)
        page.goto(page_url, wait_until="domcontentloaded", timeout=30_000)
        user_agent = page.evaluate("navigator.userAgent")
        logging.info("Страница открыта, UA CloakBrowser получен")

        result = page.evaluate(
            API_FETCH_SCRIPT,
            {"url": api_url, "characterId": uuid_part, "headers": _browser_headers()},
        )
        if result.get("ok"):
            status, api_text = int(result["status"]), result["body"]
            logging.info("Download API через browser fetch ответил статусом %s", status)
        else:
            logging.warning("Browser fetch не сработал: %s", result.get("message"))
            status, api_text = _post_download_api(context, api_url, page_url, uuid_part, user_agent)

        if status == 403:
            logging.warning("Browser/API ответил 403, повторяем с явным Cookie header")
            status, api_text = _post_download_api(context, api_url, page_url, uuid_part, user_agent)
        if status != 200:
            reason = f"Ошибка API: {status} — {api_text[:300]}"
            logging.warning("Janny download API недоступен, пробуем извлечь карточку со страницы: %s", reason)
            return _save_character_from_page_state(context, page, page_url, file_path, character_id, "Janny AI", payloads)

        try:
            data = json.loads(api_text)
        except json.JSONDecodeError:
            logging.warning("Janny download API вернул не JSON, пробуем страницу")
            return _save_character_from_page_state(context, page, page_url, file_path, character_id, "Janny AI", payloads)
        if data.get("status") != "ok":
            logging.warning("Janny download API вернул ошибку, пробуем страницу: %s", data)
            return _save_character_from_page_state(context, page, page_url, file_path, character_id, "Janny AI", payloads)

        logging.info("Скачиваем PNG по downloadUrl")
        image_response = context.request.get(
            data["downloadUrl"],
            headers={"Referer": page_url, "User-Agent": user_agent},
            timeout=60_000,
        )
        if image_response.status != 200:
            image = page.evaluate(DOWNLOAD_FETCH_SCRIPT, data["downloadUrl"])
            if int(image["status"]) != 200:
                raise ValueError(f"Ошибка скачивания файла: {image_response.status}/{image['status']}")
            image_body = base64.b64decode(image["body"])
        else:
            image_body = image_response.body()

        with open(file_path, "wb") as f:
            f.write(image_body)
        logging.info("PNG сохранен: %s", file_path)
        return file_path
    finally:
        logging.info("Закрываем CloakBrowser")
        browser.close()


def _json_from_text(text: str) -> Any | None:
    stripped = text.strip()
    if not stripped:
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        for marker in ('{"props"', '{"pageProps"', '{"data"', '{"character"', '[{"'):
            index = stripped.find(marker)
            if index < 0:
                continue
            try:
                return decoder.raw_decode(stripped[index:])[0]
            except json.JSONDecodeError:
                continue
    return None


def _walk_dicts(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        result = [value]
        for child in value.values():
            result.extend(_walk_dicts(child))
        return result
    if isinstance(value, list):
        result: list[dict[str, Any]] = []
        for child in value:
            result.extend(_walk_dicts(child))
        return result
    return []


def _key_lookup(data: dict[str, Any], aliases: tuple[str, ...]) -> Any:
    normalized = {key.lower().replace("_", ""): value for key, value in data.items()}
    for alias in aliases:
        value = normalized.get(alias.lower().replace("_", ""))
        if value not in (None, ""):
            return value
    return None


def _as_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "\n".join(item.strip() for item in value if isinstance(item, str) and item.strip())
    return ""


def _as_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [item.strip() for item in value if isinstance(item, str) and item.strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _find_url(value: Any) -> str:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith(("http://", "https://", "data:image/", "/")):
            return stripped
        return ""
    if isinstance(value, dict):
        for child in value.values():
            found = _find_url(child)
            if found:
                return found
    if isinstance(value, list):
        for child in value:
            found = _find_url(child)
            if found:
                return found
    return ""


def _find_image_url(data: dict[str, Any]) -> str:
    for key, value in data.items():
        normalized = str(key).lower().replace("_", "")
        if any(marker in normalized for marker in ("avatar", "image", "picture", "photo", "thumbnail", "portrait", "cover")):
            image_url = _find_url(value)
            if image_url:
                return image_url

    value = _key_lookup(
        data,
        (
            "avatar",
            "avatar_url",
            "avatarUrl",
            "avatar_image_url",
            "image",
            "image_url",
            "imageUrl",
            "profile_picture",
            "profilePicture",
            "profileImage",
            "thumbnail",
            "thumbnailUrl",
        ),
    )
    return _find_url(value)


def _find_state_image(state: dict[str, Any], raw: dict[str, Any], character_id: str) -> str:
    uuid_part = character_id.split("_", 1)[0].lower()
    name = _as_text(_key_lookup(raw, ("name", "title"))).lower()
    candidates: list[tuple[int, int, str]] = []
    for index, image in enumerate(state.get("images", [])):
        if not isinstance(image, dict):
            continue
        image_url = _find_url(image.get("src"))
        if not image_url:
            continue
        haystack = " ".join(
            str(image.get(key, "")).lower()
            for key in ("src", "alt", "className")
        )
        score = 0
        if uuid_part and uuid_part in haystack:
            score += 8
        if name and name in haystack:
            score += 5
        if any(marker in haystack for marker in ("avatar", "character", "profile", "portrait", "image")):
            score += 2
        if any(marker in haystack for marker in ("logo", "favicon", "sprite", "placeholder", "emoji", "badge")):
            score -= 5
        if min(_as_int(image.get("width")), _as_int(image.get("height"))) >= 128:
            score += 1
        if score > -5 and _image_candidate_allowed(image_url, haystack):
            candidates.append((score, -index, image_url))
    return max(candidates)[2] if candidates else ""


def _image_candidate_allowed(image_url: str, haystack: str) -> bool:
    lowered = image_url.lower()
    if lowered.startswith("data:image/svg"):
        return False
    if any(marker in lowered or marker in haystack for marker in ("favicon", "logo", "placeholder", "sprite", "badge")):
        return False
    return True


def _find_image_in_tree(value: Any) -> str:
    if isinstance(value, dict):
        image_url = _find_image_url(value)
        if image_url:
            return image_url
        for child in value.values():
            image_url = _find_image_in_tree(child)
            if image_url:
                return image_url
    elif isinstance(value, list):
        for child in value:
            image_url = _find_image_in_tree(child)
            if image_url:
                return image_url
    return ""


def _find_janitor_image(
    raw: dict[str, Any],
    state: dict[str, Any],
    payloads: list[Any],
    character_id: str = "",
) -> str:
    image_url = _find_image_in_tree(raw)
    if image_url:
        return image_url
    objects: list[Any] = list(payloads)
    for script in state.get("scripts", []):
        parsed = _json_from_text(str(script.get("text", "")))
        if parsed is not None:
            objects.append(parsed)
    candidates = [candidate for item in objects for candidate in _walk_dicts(item)]
    uuid_part = character_id.split("_", 1)[0].lower()
    matching = [
        candidate
        for candidate in candidates
        if uuid_part and uuid_part in str(_key_lookup(candidate, ("id", "characterId", "uuid"))).lower()
    ]
    for candidate in matching + candidates:
        image_url = _find_image_in_tree(candidate)
        if image_url:
            return image_url
    meta = {str(key).lower(): value for key, value in state.get("meta", {}).items()}
    image_url = _find_url(meta.get("og:image") or meta.get("twitter:image"))
    if image_url:
        return image_url
    return _find_state_image(state, raw, character_id)


def _validate_image_url(image_url: str, page_url: str) -> str:
    if image_url.startswith("data:image/"):
        return image_url
    absolute_url = urljoin(page_url, image_url)
    parsed = urlparse(absolute_url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("Сайт вернул недопустимый URL изображения.")
    try:
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError("Не удалось определить адрес сервера изображения.") from exc
    if any(not ipaddress.ip_address(address[4][0]).is_global for address in addresses):
        raise ValueError("Сайт вернул небезопасный адрес изображения.")
    return absolute_url


def _candidate_score(data: dict[str, Any]) -> int:
    if _looks_like_site_ui_candidate(data):
        return 0

    name = _as_text(_key_lookup(data, ("name", "title", "characterName", "character_name", "displayName", "display_name")))
    if not name or _looks_like_label_name(name):
        return 0

    score = 3
    if _as_text(_key_lookup(data, ("description", "definition", "persona", "personality"))):
        score += 3
    if _as_text(
        _key_lookup(data, ("first_mes", "firstMessage", "first_message", "greeting", "initial_message", "initialMessage"))
    ):
        score += 3
    if _as_text(_key_lookup(data, ("scenario", "message_example", "example_dialogs", "exampleDialogue"))):
        score += 1
    if _find_image_url(data):
        score += 2
    return score


def _looks_like_site_ui_candidate(data: dict[str, Any]) -> bool:
    keys = {str(key) for key in data}
    lowered_keys = {key.lower() for key in keys}
    ui_keys = {
        "completesecurityverification",
        "emailaddress",
        "moderationauditlog",
        "moderationname",
        "reportcontent",
        "reporttitle",
        "securityverification",
        "selectlanguage",
        "somethingwentwrong",
    }
    normalized_keys = {key.replace("_", "").replace("-", "") for key in lowered_keys}
    if normalized_keys & ui_keys:
        return True
    title = _as_text(_key_lookup(data, ("name", "title"))).lower()
    return title in {"moderation tools", "login", "sign in", "security verification"}


def _looks_like_label_name(name: str) -> bool:
    return name.strip().lower() in {"name", "title", "character", "character name"}


def _extract_janitor_character(state: dict[str, Any], payloads: list[Any]) -> dict[str, Any]:
    return _extract_character_from_state(state, payloads, "Janitor AI")


def _extract_character_from_state(state: dict[str, Any], payloads: list[Any], site_name: str) -> dict[str, Any]:
    objects = list(payloads)
    for script in state.get("scripts", []):
        parsed = _json_from_text(str(script.get("text", "")))
        if parsed is not None:
            objects.append(parsed)

    best: dict[str, Any] | None = None
    best_score = 0
    for item in objects:
        for candidate in _walk_dicts(item):
            score = _candidate_score(candidate)
            if score > best_score:
                best, best_score = candidate, score

    if best and best_score >= 6:
        return best

    body = f"{state.get('title', '')}\n{state.get('bodyText', '')}".lower()
    if any(marker in body for marker in ("sign in", "log in", "login", "private")):
        raise ValueError(f"Персонаж недоступен: нужны актуальные cookies {site_name}.")
    if any(marker in body for marker in ("cloudflare", "verify you are human", "access denied", "challenge")):
        raise ValueError(f"{site_name} вернул Cloudflare/WAF вместо карточки.")
    logging.warning(
        "%s card data not found. title=%r meta=%r body=%r",
        site_name,
        state.get("title", ""),
        state.get("meta", {}),
        state.get("bodyText", "")[:500],
    )
    raise ValueError(f"Не удалось извлечь данные карточки {site_name}.")


def _to_chara_card_v2(raw: dict[str, Any]) -> dict[str, Any]:
    name = _as_text(_key_lookup(raw, ("name", "title", "characterName", "character_name", "displayName", "display_name")))
    description = _as_text(_key_lookup(raw, ("description", "definition", "persona", "personality")))
    first_mes = _as_text(
        _key_lookup(raw, ("first_mes", "firstMessage", "first_message", "greeting", "initial_message", "initialMessage"))
    )
    if not name or not description or not first_mes:
        logging.warning("Incomplete Janitor card fields: keys=%s raw=%r", sorted(raw.keys()), str(raw)[:1000])
        raise ValueError("Не удалось извлечь обязательные поля карточки Janitor AI.")

    return {
        "spec": "chara_card_v2",
        "spec_version": "2.0",
        "data": {
            "name": name,
            "description": description,
            "personality": _as_text(_key_lookup(raw, ("personality", "persona"))),
            "scenario": _as_text(_key_lookup(raw, ("scenario",))),
            "first_mes": first_mes,
            "mes_example": _as_text(
                _key_lookup(raw, ("mes_example", "message_example", "example_dialogs", "exampleDialogue"))
            ),
            "creator_notes": _as_text(_key_lookup(raw, ("creator_notes", "creatorNotes"))),
            "system_prompt": _as_text(_key_lookup(raw, ("system_prompt", "systemPrompt"))),
            "post_history_instructions": _as_text(
                _key_lookup(raw, ("post_history_instructions", "postHistoryInstructions"))
            ),
            "alternate_greetings": _as_list(_key_lookup(raw, ("alternate_greetings", "alternateGreetings"))),
            "tags": _as_list(_key_lookup(raw, ("tags",))),
            "creator": _as_text(_key_lookup(raw, ("creator", "creatorName", "author", "username"))),
            "character_version": _as_text(_key_lookup(raw, ("character_version", "version"))),
            "extensions": {"janitorai": raw},
        },
    }


def _write_chara_png(image_body: bytes, card: dict[str, Any], file_path: str) -> None:
    png_info = PngImagePlugin.PngInfo()
    payload = json.dumps(card, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    png_info.add_text("chara", base64.b64encode(payload).decode("ascii"))
    with Image.open(BytesIO(image_body)) as image:
        image.save(file_path, format="PNG", pnginfo=png_info)


def _save_character_from_page_state(
    context: Any,
    page: Any,
    page_url: str,
    file_path: str,
    character_id: str,
    site_name: str,
    payloads: list[Any],
) -> str:
    try:
        page.wait_for_load_state("networkidle", timeout=15_000)
    except Exception:
        logging.info("%s networkidle timeout, продолжаем с уже полученными данными", site_name)
    user_agent = page.evaluate("navigator.userAgent")
    state = page.evaluate(JANITOR_STATE_SCRIPT)
    raw_character = _extract_character_from_state(state, payloads, site_name)
    image_url = _find_janitor_image(raw_character, state, payloads, character_id)
    if not image_url:
        raise ValueError("Не удалось найти изображение персонажа.")
    card = _to_chara_card_v2(raw_character)
    image_body = _download_image_body(context, page, image_url, page_url, user_agent)
    _write_chara_png(image_body, card, file_path)
    logging.info("%s PNG с metadata сохранен: %s", site_name, file_path)
    return file_path


def _visible_locator(page: Any, selectors: tuple[str, ...]) -> Any | None:
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            if locator.count() and locator.is_visible():
                return locator
        except Exception:
            continue
    return None


def _janitor_login_error(body_text: str) -> str:
    body = body_text.lower()
    if any(marker in body for marker in ("captcha", "verify you are human", "cloudflare", "challenge")):
        return "Janitor AI запросил CAPTCHA или проверку Cloudflare."
    if any(
        marker in body
        for marker in (
            "invalid email",
            "invalid password",
            "incorrect password",
            "wrong password",
            "invalid login credentials",
            "email or password is incorrect",
        )
    ):
        return "Janitor AI отклонил логин или пароль."
    return ""


def _page_looks_like_login(page: Any, body_text: str) -> bool:
    body = body_text.lower()
    current_url = str(getattr(page, "url", "")).lower()
    if current_url.endswith(("/login", "/signin")):
        return True
    return any(marker in body for marker in ("welcome back", "sign in to your account", "forgot password?"))


def _login_site(page: Any, base_url: str, site_name: str, *, strict_form: bool) -> None:
    email = os.getenv("JANITORAI_EMAIL", "").strip()
    password = os.getenv("JANITORAI_PASSWORD", "")
    if not email and not password:
        return
    if not email or not password:
        raise ValueError("Для входа укажи JANITORAI_EMAIL и JANITORAI_PASSWORD.")
    email_selectors = (
        'input[type="email"]', 'input[name="email"]',
        'input[autocomplete="email"]', 'input[placeholder*="mail" i]',
    )
    password_selectors = (
        'input[type="password"]', 'input[name="password"]',
        'input[autocomplete="current-password"]',
    )
    for login_url in (f"{base_url}/login", f"{base_url}/signin"):
        response = page.goto(login_url, wait_until="domcontentloaded", timeout=45_000)
        if response and response.status in (401, 403):
            if strict_form:
                raise ValueError(f"{site_name} заблокировал страницу входа (Cloudflare/WAF).")
            logging.warning("%s отклонил страницу входа; продолжаем с cookies/API", site_name)
            return
        try:
            page.locator(", ".join(email_selectors)).first.wait_for(state="visible", timeout=8_000)
        except Exception:
            pass
        email_input = _visible_locator(page, email_selectors)
        password_input = _visible_locator(page, password_selectors)
        if email_input and password_input:
            break
    else:
        body = str(page.locator("body").inner_text()).lower()
        if any(marker in body for marker in ("cloudflare", "verify you are human", "access denied", "challenge")):
            if strict_form:
                raise ValueError(f"{site_name} запросил проверку Cloudflare/WAF.")
            logging.warning("%s запросил проверку; продолжаем с cookies/API", site_name)
            return
        if strict_form:
            raise ValueError(f"Не удалось открыть форму входа {site_name}.")
        logging.info("Форма входа %s не найдена; продолжаем с текущей сессией", site_name)
        return
    email_input.fill(email)
    password_input.fill(password)
    submit = _visible_locator(page, (
        'button[type="submit"]', 'input[type="submit"]',
        'button:has-text("Sign in")', 'button:has-text("Log in")',
        'button:has-text("Login")',
    ))
    if not submit:
        if strict_form:
            raise ValueError(f"Не удалось найти кнопку входа {site_name}.")
        logging.info("Кнопка входа %s не найдена; продолжаем с текущей сессией", site_name)
        return
    submit.click()
    try:
        page.wait_for_load_state("networkidle", timeout=20_000)
    except Exception:
        logging.info("Janitor login networkidle timeout, проверяем состояние страницы")
    body = str(page.locator("body").inner_text())
    login_error = _janitor_login_error(body)
    if login_error:
        if strict_form:
            raise ValueError(login_error)
        logging.warning("%s не подтвердил вход; продолжаем с cookies/API", site_name)
        return
    if _page_looks_like_login(page, body):
        logging.warning("%s оставил страницу входа открытой; продолжаем и проверяем фактический доступ", site_name)
        return
    if _visible_locator(page, password_selectors):
        logging.warning(
            "Форма входа %s осталась видимой; продолжаем и проверяем фактический доступ",
            site_name,
        )
    else:
        logging.info("Вход в %s через браузер выполнен", site_name)


def _login_janitor(page: Any) -> None:
    _login_site(page, "https://janitorai.com", "Janitor AI", strict_form=True)


def _login_janny(page: Any) -> None:
    _login_site(page, "https://jannyai.com", "Janny AI", strict_form=False)


def _download_image_body(context: Any, page: Any, image_url: str, page_url: str, user_agent: str) -> bytes:
    if image_url.startswith("data:image/") and ";base64," in image_url:
        try:
            body = base64.b64decode(image_url.split(";base64,", 1)[1], validate=True)
        except binascii.Error as exc:
            raise ValueError("Не удалось прочитать изображение персонажа.") from exc
        if len(body) > IMAGE_RESPONSE_LIMIT:
            raise ValueError("Изображение персонажа слишком большое.")
        return body

    absolute_url = _validate_image_url(image_url, page_url)
    response = None
    for _ in range(4):
        response = context.request.get(
            absolute_url,
            headers={"Referer": page_url, "User-Agent": user_agent},
            timeout=60_000,
            max_redirects=0,
        )
        if response.status not in (301, 302, 303, 307, 308):
            break
        location = response.headers.get("location", "")
        if not location:
            raise ValueError("Сервер изображения вернул некорректный redirect.")
        absolute_url = _validate_image_url(location, absolute_url)
    if not response or response.status != 200:
        raise ValueError(f"Не удалось скачать изображение персонажа: HTTP {response.status if response else 0}.")
    _validate_image_url(response.url, page_url)
    content_type = response.headers.get("content-type", "").lower()
    if not content_type.startswith("image/"):
        raise ValueError("Сервер вернул не изображение.")
    body = response.body()
    if len(body) > IMAGE_RESPONSE_LIMIT:
        raise ValueError("Изображение персонажа слишком большое.")
    return body


def _datacat_headers(token: str = "") -> dict[str, str]:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
        "Origin": DATACAT_BASE,
        "Referer": f"{DATACAT_BASE}/",
    }
    if token:
        headers["X-Session-Token"] = token
    return headers


def _datacat_json(method: str, path: str, *, token: str = "", body: dict[str, Any] | None = None) -> tuple[int, Any]:
    data = None
    headers = _datacat_headers(token)
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urlrequest.Request(f"{DATACAT_BASE}{path}", data=data, headers=headers, method=method)
    try:
        with urlrequest.urlopen(request, timeout=90) as response:
            text = response.read().decode("utf-8", "replace")
            return response.status, json.loads(text) if text else None
    except urlerror.HTTPError as exc:
        text = exc.read().decode("utf-8", "replace")
        try:
            payload = json.loads(text) if text else None
        except json.JSONDecodeError:
            payload = {"error": text[:500]}
        return exc.code, payload
    except (urlerror.URLError, TimeoutError) as exc:
        raise ValueError("DataCat API недоступен.") from exc


def _datacat_session_token() -> str:
    status, data = _datacat_json(
        "POST",
        "/api/liberator/identify",
        body={"deviceToken": f"janny-bot-{time.time_ns()}"},
    )
    token = data.get("sessionToken") if isinstance(data, dict) else ""
    if status != 200 or not token:
        raise ValueError("Не удалось открыть DataCat session.")
    return str(token)


def _datacat_character_url(character_id: str) -> str:
    return f"https://janitorai.com/characters/{character_id}"


def _fetch_datacat_character(uuid_part: str, token: str) -> dict[str, Any] | None:
    for query in ("?sourceKind=janitor", ""):
        status, data = _datacat_json("GET", f"/api/characters/{uuid_part}{query}", token=token)
        if status == 200 and isinstance(data, dict) and isinstance(data.get("character"), dict):
            return data["character"]
    return None


def _submit_datacat_extraction(character_id: str, token: str) -> None:
    request_id = f"janny-bot-{time.time_ns()}"
    body = {
        "url": _datacat_character_url(character_id),
        "openLoginIfNoSession": True,
        "sessionId": None,
        "appearOnPublicFeed": False,
        "useSeparateWorkerServer": True,
        "inlinePostExtractCreatorProfile": True,
        "idempotencyKey": request_id,
        "extractSourceMode": "core_plus_janny",
        "alwaysReextract": False,
    }
    status, data = _datacat_json("POST", "/api/character/smart-extract-v2", token=token, body=body)
    if status >= 400:
        message = data.get("message") or data.get("error") if isinstance(data, dict) else ""
        raise ValueError(f"DataCat extraction не стартовал: HTTP {status} {message}".strip())


def _datacat_card_from_character(character: dict[str, Any]) -> dict[str, Any]:
    card = character.get("chara_card_v2_json")
    if isinstance(card, dict) and isinstance(card.get("data"), dict):
        data = card["data"]
        if _as_text(data.get("name")) and _as_text(data.get("first_mes")):
            if not _as_text(data.get("description")) and _as_text(character.get("personality")):
                data["description"] = _as_text(character.get("personality"))
            if not _as_text(data.get("creator_notes")) and _as_text(character.get("description")):
                data["creator_notes"] = _as_text(character.get("description"))
            if not _as_text(data.get("scenario")) and _as_text(character.get("scenario")):
                data["scenario"] = _as_text(character.get("scenario"))
            data.setdefault("extensions", {})
            data["extensions"].setdefault("datacat", {})
            data["extensions"]["datacat"].update(
                {
                    "id": character.get("character_id"),
                    "sourceKind": character.get("primary_content_source_kind"),
                    "creatorId": character.get("creator_id"),
                    "creatorName": character.get("creator_name"),
                }
            )
            return card

    raw = {
        "name": character.get("name") or character.get("chat_name"),
        "description": character.get("personality") or character.get("description"),
        "personality": "",
        "scenario": character.get("scenario"),
        "firstMessage": character.get("first_message"),
        "creator_notes": character.get("description"),
        "creator": character.get("creator_name"),
        "alternate_greetings": character.get("alternate_greetings") or [],
        "tags": character.get("tags") or character.get("custom_tags") or [],
    }
    return _to_chara_card_v2(raw)


def _datacat_image_url(character: dict[str, Any], card: dict[str, Any]) -> str:
    data = card.get("data", {}) if isinstance(card, dict) else {}
    candidates = [
        data.get("avatar") if isinstance(data, dict) else "",
        character.get("avatar"),
        character.get("avatarDisplayUrl") or character.get("avatar_display_url"),
        (character.get("avatarVariantUrls") or {}).get("hero") if isinstance(character.get("avatarVariantUrls"), dict) else "",
        (character.get("avatar_variant_urls") or {}).get("hero") if isinstance(character.get("avatar_variant_urls"), dict) else "",
    ]
    for candidate in candidates:
        if not isinstance(candidate, str) or not candidate.strip():
            continue
        candidate = candidate.strip()
        return candidate if candidate.startswith(("http://", "https://", "data:image/")) else f"{DATACAT_JANITOR_IMAGE_BASE}{candidate}"
    raise ValueError("DataCat не вернул изображение персонажа.")


def _download_url_image_body(image_url: str, page_url: str) -> bytes:
    if image_url.startswith("data:image/") and ";base64," in image_url:
        return base64.b64decode(image_url.split(";base64,", 1)[1], validate=True)
    absolute_url = _validate_image_url(image_url, page_url)
    request = urlrequest.Request(
        absolute_url,
        headers={"User-Agent": "Mozilla/5.0", "Referer": page_url},
        method="GET",
    )
    try:
        with urlrequest.urlopen(request, timeout=60) as response:
            content_type = response.headers.get("content-type", "").lower()
            if not content_type.startswith("image/"):
                raise ValueError("Сервер DataCat вернул не изображение.")
            body = response.read(IMAGE_RESPONSE_LIMIT + 1)
    except urlerror.HTTPError as exc:
        raise ValueError(f"Не удалось скачать изображение DataCat: HTTP {exc.code}.") from exc
    except (urlerror.URLError, TimeoutError) as exc:
        raise ValueError("Не удалось скачать изображение DataCat.") from exc
    if len(body) > IMAGE_RESPONSE_LIMIT:
        raise ValueError("Изображение персонажа слишком большое.")
    return body


def _download_datacat_character_sync(character_id: str) -> str:
    uuid_part = character_id.split("_", 1)[0]
    file_path = os.path.join(tempfile.gettempdir(), f"{character_id}.png")
    token = _datacat_session_token()
    character = _fetch_datacat_character(uuid_part, token)
    if not character:
        logging.info("DataCat: персонаж не найден, отправляем extraction request")
        _submit_datacat_extraction(character_id, token)
        for _ in range(18):
            time.sleep(5)
            character = _fetch_datacat_character(uuid_part, token)
            if character:
                break
    if not character:
        raise ValueError("DataCat не вернул карточку персонажа.")

    card = _datacat_card_from_character(character)
    page_url = _datacat_character_url(character_id)
    image_url = _datacat_image_url(character, card)
    image_body = _download_url_image_body(image_url, page_url)
    _write_chara_png(image_body, card, file_path)
    logging.info("DataCat PNG с metadata сохранен: %s", file_path)
    return file_path

def _download_janitor_character_sync(character_id: str) -> str:
    logging.info("Старт скачивания Janitor character_id=%s", character_id)
    load_data()

    page_url = f"https://janitorai.com/characters/{character_id}"
    file_path = os.path.join(tempfile.gettempdir(), f"{character_id}.png")
    browser = _launch()
    payloads: list[Any] = []

    try:
        context = browser.new_context()
        if GLOBAL_COOKIES:
            logging.info("Добавляем cookies в Janitor browser context: %s параметров", len(GLOBAL_COOKIES))
            context.add_cookies(_browser_cookies())

        page = context.new_page()
        _login_janitor(page)
        _sync_context_cookies(context)

        page.on("response", lambda response: _capture_json_response(response, payloads, ("janitorai",)))
        logging.info("Открываем Janitor страницу через CloakBrowser: %s", page_url)
        response = page.goto(page_url, wait_until="domcontentloaded", timeout=45_000)
        if response and response.status in (401, 403):
            raise ValueError("Janitor AI отклонил доступ: проверь авторизацию или Cloudflare/WAF.")
        try:
            page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            logging.info("Janitor networkidle timeout, продолжаем с уже полученными данными")

        user_agent = page.evaluate("navigator.userAgent")
        state = page.evaluate(JANITOR_STATE_SCRIPT)
        raw_character = _extract_janitor_character(state, payloads)
        card = _to_chara_card_v2(raw_character)
        image_url = _find_janitor_image(raw_character, state, payloads, character_id)
        if not image_url:
            raise ValueError("Не удалось найти изображение персонажа.")

        image_body = _download_image_body(context, page, image_url, page_url, user_agent)
        _write_chara_png(image_body, card, file_path)
        logging.info("Janitor PNG с metadata сохранен: %s", file_path)
        return file_path
    finally:
        logging.info("Закрываем CloakBrowser")
        browser.close()


async def download_character(character_id: str, source: str = "jannyai") -> str:
    source_name = source.lower().replace(".com", "")
    if source_name == "janitorai":
        try:
            logging.info("Janitor URL: сначала пробуем DataCat")
            return await asyncio.to_thread(_download_datacat_character_sync, character_id)
        except Exception as datacat_error:
            logging.warning("DataCat не вернул персонажа, fallback на Janny/Janitor: %s", datacat_error)
            try:
                return await asyncio.to_thread(_download_janny_character_sync, character_id)
            except Exception as janny_error:
                logging.warning("Janny AI не вернул персонажа, fallback на Janitor AI: %s", janny_error)
                try:
                    return await asyncio.to_thread(_download_janitor_character_sync, character_id)
                except Exception as janitor_error:
                    raise ValueError(
                        f"Персонаж недоступен через DataCat ({datacat_error}), "
                        f"Janny AI ({janny_error}) и Janitor AI ({janitor_error})."
                    ) from janitor_error
    try:
        return await asyncio.to_thread(_download_janny_character_sync, character_id)
    except Exception as janny_error:
        logging.warning("Janny AI не вернул персонажа, fallback на DataCat: %s", janny_error)
        try:
            return await asyncio.to_thread(_download_datacat_character_sync, character_id)
        except Exception as datacat_error:
            raise ValueError(
                f"Персонаж недоступен через Janny AI ({janny_error}) и DataCat ({datacat_error})."
            ) from datacat_error


load_data()
