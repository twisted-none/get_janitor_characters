import asyncio
import base64
import binascii
from collections.abc import Callable
from copy import deepcopy
from functools import partial
from io import BytesIO
import json
import logging
import os
from pathlib import Path
import tempfile
import time
from typing import Any
from urllib.parse import urljoin, urlparse
from uuid import uuid4

from browser_runtime import launch_browser
from PIL import Image, PngImagePlugin

from janitor_proxy import extract_definition, get_access_token
from download_flow import Candidate, DownloadResult, DownloadSource, ProgressCallback, remove_download, run_download_flow
from safe_http import fetch, validate_url
from security_policy import RAW_FIELDS, clean_card, protect_context, require_origin, require_public

GLOBAL_COOKIES: dict[str, str] = {}
JSON_RESPONSE_LIMIT = 2_000_000
IMAGE_RESPONSE_LIMIT = 25_000_000
Image.MAX_IMAGE_PIXELS = 16_000_000
IMAGE_FORMATS = ("PNG", "JPEG", "WEBP")
DATACAT_BASE = "https://datacat.run"
DATACAT_JANITOR_IMAGE_BASE = "https://ella.janitorai.com/bot-avatars/"
JANITOR_STATE_PATH = Path(__file__).resolve().parent / ".cache" / "janitor_auth_state.json"

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
      script.text.includes("definition") ||
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
        .map((item) => item.trim().split(/\\s+/)[0])
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
    domains = (".janitorai.com",)
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


def _sync_context_cookies(context: Any) -> None:
    for cookie in context.cookies():
        domain = str(cookie.get("domain", "")).lstrip(".")
        if domain != "janitorai.com" and not domain.endswith(".janitorai.com"):
            continue
        name = str(cookie.get("name", ""))
        value = str(cookie.get("value", ""))
        if name and value:
            GLOBAL_COOKIES[name] = value


def _launch(*, headless: bool = True) -> Any:
    if os.getenv("CLOAKBROWSER_PROXY"):
        raise ValueError("Транспортный proxy не поддерживается защищённым HTTPS-клиентом.")
    logging.info("Запускаем Chromium с sandbox")
    return launch_browser(headless=headless)


def _post_download_api(context: Any, api_url: str, page_url: str, character_id: str, user_agent: str) -> tuple[int, str]:
    status, _, body, _ = fetch(api_url, method="POST",
                               body=json.dumps({"characterId": character_id}).encode(),
                               headers=_janny_headers(context, page_url, user_agent),
                               limit=JSON_RESPONSE_LIMIT, timeout=30, redirects=0,
                               domains=("api.jannyai.com",))
    return status, body.decode("utf-8")


def _janny_headers(context: Any, page_url: str, user_agent: str) -> dict[str, str]:
    cookies = {
        str(cookie["name"]): str(cookie["value"])
        for cookie in context.cookies()
        if str(cookie.get("domain", "")).lstrip(".") in {"jannyai.com", "api.jannyai.com"}
    }
    headers = {
        "Accept": "application/json", "Content-Type": "application/json",
        "Origin": "https://jannyai.com", "Referer": page_url, "User-Agent": user_agent,
    }
    if cookies:
        headers["Cookie"] = "; ".join(f"{name}={value}" for name, value in cookies.items())
    access_token = cookies.get("sb-access-token", "")
    if access_token and _cookie_is_usable("sb-access-token", access_token):
        headers["Authorization"] = f"Bearer {access_token}"
    return headers


def _character_file_path() -> str:
    return os.path.join(tempfile.gettempdir(), f"character-{uuid4().hex}.png")


def _capture_json_response(response: Any, payloads: list[Any], hosts: tuple[str, ...]) -> None:
    try:
        content_type = response.headers.get("content-type", "")
        hostname = urlparse(response.url).hostname or ""
        if not any(hostname == host or hostname.endswith("." + host) for host in hosts) or "json" not in content_type:
            return
        if len(payloads) >= 16:
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
    api_url = "https://api.jannyai.com/api/v1/download"
    page_url = f"https://jannyai.com/characters/{character_id}"
    uuid_part = character_id.split("_")[0]
    file_path = _character_file_path()
    browser = _launch()
    payloads: list[Any] = []
    logging.info("Chromium запущен")

    try:
        context = protect_context(browser.new_context(service_workers="block", accept_downloads=False))
        page = context.new_page()
        _login_janny(page)
        page.on("response", lambda response: _capture_json_response(response, payloads, ("jannyai.com",)))
        logging.info("Открываем страницу через Chromium: %s", page_url)
        page.goto(page_url, wait_until="domcontentloaded", timeout=30_000)
        require_origin(page, "https://jannyai.com")
        user_agent = page.evaluate("navigator.userAgent")
        logging.info("Страница открыта, UA Chromium получен")

        result = page.evaluate(
            API_FETCH_SCRIPT,
            {"url": api_url, "characterId": uuid_part, "headers": _janny_headers(context, page_url, user_agent)},
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
            reason = f"Ошибка API: HTTP {status}"
            logging.warning("Janny download API недоступен, пробуем извлечь карточку со страницы: %s", reason)
            return _save_character_from_page_state(context, page, page_url, file_path, character_id, "Janny AI", payloads)

        try:
            data = json.loads(api_text)
        except json.JSONDecodeError:
            logging.warning("Janny download API вернул не JSON, пробуем страницу")
            return _save_character_from_page_state(context, page, page_url, file_path, character_id, "Janny AI", payloads)
        if data.get("status") != "ok":
            logging.warning("Janny download API вернул ошибку, пробуем страницу")
            return _save_character_from_page_state(context, page, page_url, file_path, character_id, "Janny AI", payloads)

        logging.info("Скачиваем PNG по downloadUrl")
        image_body = _download_url_image_body(data["downloadUrl"], page_url)

        with open(file_path, "wb") as f:
            f.write(image_body)
        logging.info("PNG сохранен: %s", file_path)
        return file_path
    except BaseException:
        remove_download(file_path)
        raise
    finally:
        logging.info("Закрываем Chromium")
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


def _definition_text(data: dict[str, Any]) -> str:
    return _as_text(
        _key_lookup(
            data,
            (
                "definition",
                "characterDefinition",
                "character_definition",
                "hiddenDefinition",
                "hidden_definition",
                "privateDefinition",
                "private_definition",
                "personality",
                "persona",
            ),
        )
    )


def _public_description_text(data: dict[str, Any]) -> str:
    return _as_text(_key_lookup(data, ("description", "publicDescription", "public_description")))


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
    validate_url(absolute_url)
    return absolute_url


def _candidate_score(data: dict[str, Any]) -> int:
    if _looks_like_site_ui_candidate(data):
        return 0

    name = _as_text(_key_lookup(data, ("name", "title", "characterName", "character_name", "displayName", "display_name")))
    if not name or _looks_like_label_name(name):
        return 0

    score = 3
    if _public_description_text(data) or _definition_text(data):
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
    logging.warning("%s card data not found", site_name)
    raise ValueError(f"Не удалось извлечь данные карточки {site_name}.")


def _to_chara_card_v2(raw: dict[str, Any]) -> dict[str, Any]:
    name = _as_text(_key_lookup(raw, ("name", "title", "characterName", "character_name", "displayName", "display_name")))
    public_description = _public_description_text(raw)
    hidden_definition = _definition_text(raw)
    description = public_description or hidden_definition
    personality = hidden_definition if public_description else ""
    creator_notes = _as_text(_key_lookup(raw, ("creator_notes", "creatorNotes")))
    if hidden_definition and public_description and public_description != hidden_definition and not creator_notes:
        creator_notes = public_description
    first_mes = _as_text(
        _key_lookup(raw, ("first_mes", "firstMessage", "first_message", "greeting", "initial_message", "initialMessage"))
    )
    if not name or not description or not first_mes:
        logging.warning("Incomplete Janitor card fields: keys=%s", sorted(raw.keys()))
        raise ValueError("Не удалось извлечь обязательные поля карточки Janitor AI.")

    return {
        "spec": "chara_card_v2",
        "spec_version": "2.0",
        "data": {
            "name": name,
            "description": description,
            "personality": personality,
            "scenario": _as_text(_key_lookup(raw, ("scenario",))),
            "first_mes": first_mes,
            "mes_example": _as_text(
                _key_lookup(raw, ("mes_example", "message_example", "example_dialogs", "exampleDialogue"))
            ),
            "creator_notes": creator_notes,
            "system_prompt": _as_text(_key_lookup(raw, ("system_prompt", "systemPrompt"))),
            "post_history_instructions": _as_text(
                _key_lookup(raw, ("post_history_instructions", "postHistoryInstructions"))
            ),
            "alternate_greetings": _as_list(_key_lookup(raw, ("alternate_greetings", "alternateGreetings"))),
            "tags": _as_list(_key_lookup(raw, ("tags",))),
            "creator": _as_text(_key_lookup(raw, ("creator", "creatorName", "author", "username"))),
            "character_version": _as_text(_key_lookup(raw, ("character_version", "version"))),
            "extensions": {"janitorai": {key: value for key, value in raw.items() if key in RAW_FIELDS}},
        },
    }


def _write_chara_png(image_body: bytes, card: dict[str, Any], file_path: str) -> None:
    png_info = PngImagePlugin.PngInfo()
    payload = json.dumps(card, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    png_info.add_text("chara", base64.b64encode(payload).decode("ascii"))
    with Image.open(BytesIO(image_body), formats=IMAGE_FORMATS) as image:
        if image.width * image.height > Image.MAX_IMAGE_PIXELS:
            raise ValueError("Слишком большое разрешение изображения.")
        image.save(file_path, format="PNG", pnginfo=png_info)


def has_verified_hidden_definition(file_path: str) -> bool:
    try:
        with Image.open(file_path) as image:
            encoded = image.info.get("chara", "")
        card = json.loads(base64.b64decode(encoded, validate=True))
        raw = card["data"]["extensions"]["janitorai"]
        return isinstance(raw, dict) and raw.get("definition_source") == "janitor_proxy_prompt"
    except (OSError, ValueError, TypeError, KeyError, binascii.Error):
        return False


def _read_card(file_path: str) -> dict[str, Any]:
    with Image.open(file_path, formats=("PNG",)) as image:
        encoded = image.info.get("chara", "")
    if not isinstance(encoded, str) or not encoded or len(encoded) > 12_000_000:
        raise ValueError("PNG не содержит допустимые данные карточки.")
    card = json.loads(base64.b64decode(encoded, validate=True))
    if not isinstance(card, dict):
        raise ValueError("Неверный формат карточки.")
    data = card.get("data", card)
    if not isinstance(data, dict) or not _as_text(data.get("name")) or not _as_text(data.get("first_mes")):
        raise ValueError("Карточка не содержит обязательные поля.")
    return card


def _card_definition_available(data: dict[str, Any]) -> bool:
    extensions = data.get("extensions")
    extensions = extensions if isinstance(extensions, dict) else {}
    datacat = extensions.get("datacat")
    if isinstance(datacat, dict) and isinstance(datacat.get("definition_available"), bool):
        return datacat["definition_available"]
    if _as_text(data.get("personality")):
        return True
    janitor = extensions.get("janitorai")
    if isinstance(janitor, dict):
        return bool(_definition_text(janitor))
    return bool(_as_text(data.get("description")))


def _mark_download(result: DownloadResult) -> None:
    card = clean_card(_read_card(result.file_path))
    data = card.get("data", card)
    if not isinstance(data.get("extensions"), dict):
        data["extensions"] = {}
    data["extensions"]["download_bot"] = {
        "source": result.source.value,
        "definition_status": ("reconstructed" if result.reconstructed else
                              "available" if result.definition_available else "unavailable"),
    }
    with open(result.file_path, "rb") as image_file:
        image_body = image_file.read(IMAGE_RESPONSE_LIMIT + 1)
    if len(image_body) > IMAGE_RESPONSE_LIMIT:
        raise ValueError("Карточка слишком большая.")
    _write_chara_png(image_body, card, result.file_path)


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


def _janitor_state_path() -> Path:
    return Path(os.getenv("JANITOR_STORAGE_STATE_PATH") or JANITOR_STATE_PATH)


def _new_janitor_context(browser: Any) -> Any:
    path = _janitor_state_path()
    if path.is_file():
        try:
            return protect_context(browser.new_context(storage_state=str(path), service_workers="block", accept_downloads=False))
        except Exception:
            logging.warning("Сохранённая сессия Janitor повреждена; пробуем новый вход")
    return protect_context(browser.new_context(service_workers="block", accept_downloads=False))


def _save_janitor_state(context: Any) -> None:
    state = context.storage_state()
    state["cookies"] = [cookie for cookie in state.get("cookies", [])
                        if (domain := str(cookie.get("domain", "")).lstrip(".")) == "janitorai.com"
                        or domain.endswith(".janitorai.com")]
    state["origins"] = [origin for origin in state.get("origins", [])
                        if origin.get("origin") == "https://janitorai.com"]
    path = _janitor_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(state, handle)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _dismiss_janitor_locale_modal(page: Any) -> None:
    modal = page.locator('[class*="modalOverlay"]')
    if modal.count() and "Русский" in modal.first.inner_text():
        modal.first.locator("button").first.click(timeout=5_000)


def _login_site(page: Any, base_url: str, site_name: str, *, strict_form: bool) -> None:
    credential_prefix = "JANITORAI" if strict_form else "JANNYAI"
    email = os.getenv(f"{credential_prefix}_EMAIL", "").strip()
    password = os.getenv(f"{credential_prefix}_PASSWORD", "")
    if not email and not password:
        return
    if not email or not password:
        raise ValueError(f"Для входа укажи {credential_prefix}_EMAIL и {credential_prefix}_PASSWORD.")
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
        require_origin(page, base_url)
        if response and response.status in (401, 403):
            if strict_form:
                raise ValueError(f"{site_name} заблокировал страницу входа (Cloudflare/WAF).")
            logging.warning("%s отклонил страницу входа; продолжаем с cookies/API", site_name)
            return
        if strict_form:
            for _ in range(10 if _janitor_state_path().is_file() else 1):
                if get_access_token(page, page.context):
                    logging.info("Используем сохранённую сессию %s", site_name)
                    _save_janitor_state(page.context)
                    return
                page.wait_for_timeout(1_000)
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
    if strict_form:
        try:
            page.wait_for_function(
                """() => {
                  const form = document.querySelector('input[type="email"]')?.closest('form');
                  return form && Object.keys(form).some((key) => key.startsWith('__reactProps$'));
                }""",
                timeout=20_000,
            )
        except Exception as exc:
            raise ValueError(f"{site_name} не загрузил обработчик формы входа.") from exc
        _dismiss_janitor_locale_modal(page)
    require_origin(page, base_url)
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
    require_origin(page, base_url)
    submit.click()
    if strict_form:
        resubmitted = False
        for _ in range(45):
            require_origin(page, base_url)
            if get_access_token(page, page.context):
                logging.info("Вход в %s через браузер выполнен", site_name)
                _save_janitor_state(page.context)
                return
            body = str(page.locator("body").inner_text())
            login_error = _janitor_login_error(body)
            if login_error:
                raise ValueError(login_error)
            turnstile_ready = page.evaluate(
                "() => !!document.querySelector('input[name=cf-turnstile-response]')?.value"
            )
            if turnstile_ready and not resubmitted:
                page.wait_for_timeout(1_000)
                if get_access_token(page, page.context):
                    _save_janitor_state(page.context)
                    return
                _dismiss_janitor_locale_modal(page)
                submit.click(timeout=5_000)
                resubmitted = True
            page.wait_for_timeout(1_000)
        turnstile_pending = page.evaluate(
            """() => {
              const field = document.querySelector('input[name="cf-turnstile-response"]');
              return Boolean(field && !field.value);
            }"""
        )
        if turnstile_pending:
            raise ValueError(f"{site_name} не завершил проверку Cloudflare Turnstile.")
        raise ValueError(f"{site_name} не подтвердил вход после отправки формы.")
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
    return _download_url_image_body(urljoin(page_url, image_url), page_url)


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
    status, _, raw, _ = fetch(f"{DATACAT_BASE}{path}", method=method, headers=headers,
                             body=data, limit=JSON_RESPONSE_LIMIT, timeout=60, redirects=0,
                             domains=("datacat.run",))
    if status >= 400:
        return status, None
    return status, json.loads(raw) if raw else None


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
        if status not in (200, 404):
            raise ValueError(f"DataCat API: HTTP {status}.")
    return None


def _datacat_definition_available(character: dict[str, Any], data: dict[str, Any]) -> bool:
    if _definition_text(character) or _as_text(data.get("personality")):
        return True
    description = _as_text(data.get("description"))
    return bool(description and description != _public_description_text(character))


def _datacat_card_from_character(character: dict[str, Any]) -> dict[str, Any]:
    card = deepcopy(character.get("chara_card_v2_json"))
    if isinstance(card, dict) and isinstance(card.get("data"), dict):
        data = card["data"]
        if _as_text(data.get("name")) and _as_text(data.get("first_mes")):
            personality = _definition_text(character)
            if not _as_text(data.get("personality")) and personality:
                data["personality"] = personality
            if not _as_text(data.get("description")):
                data["description"] = _public_description_text(character) or personality
            if not _as_text(data.get("creator_notes")) and _as_text(character.get("description")):
                data["creator_notes"] = _as_text(character.get("description"))
            if not _as_text(data.get("scenario")) and _as_text(character.get("scenario")):
                data["scenario"] = _as_text(character.get("scenario"))
            if not isinstance(data.get("extensions"), dict):
                data["extensions"] = {}
            if not isinstance(data["extensions"].get("datacat"), dict):
                data["extensions"]["datacat"] = {}
            data["extensions"]["datacat"].update(
                {
                    "id": character.get("character_id"),
                    "sourceKind": character.get("primary_content_source_kind"),
                    "creatorId": character.get("creator_id"),
                    "creatorName": character.get("creator_name"),
                    "definition_available": _datacat_definition_available(character, data),
                }
            )
            return card

    raw = {
        "name": character.get("name") or character.get("chat_name"),
        "description": character.get("description") or character.get("personality"),
        "personality": _definition_text(character),
        "scenario": character.get("scenario"),
        "firstMessage": character.get("first_message"),
        "creator_notes": character.get("description"),
        "creator": character.get("creator_name"),
        "alternate_greetings": character.get("alternate_greetings") or [],
        "tags": character.get("tags") or character.get("custom_tags") or [],
    }
    card = _to_chara_card_v2(raw)
    card["data"]["personality"] = _definition_text(character)
    card["data"]["extensions"]["datacat"] = {
        "definition_available": _datacat_definition_available(character, card["data"]),
    }
    return card


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
        if len(image_url) > IMAGE_RESPONSE_LIMIT * 4 // 3 + 256:
            raise ValueError("Изображение персонажа слишком большое.")
        body = base64.b64decode(image_url.split(";base64,", 1)[1], validate=True)
        if len(body) > IMAGE_RESPONSE_LIMIT:
            raise ValueError("Изображение персонажа слишком большое.")
        return body
    status, headers, body, _ = fetch(urljoin(page_url, image_url),
                                    headers={"User-Agent": "Mozilla/5.0", "Referer": page_url},
                                    limit=IMAGE_RESPONSE_LIMIT)
    if status != 200 or not headers.get("content-type", "").lower().startswith("image/"):
        raise ValueError("Сервер не вернул допустимое изображение.")
    return body


def _download_datacat_character_sync(character_id: str) -> str:
    uuid_part = character_id.split("_", 1)[0]
    file_path = _character_file_path()
    token = _datacat_session_token()
    character = _fetch_datacat_character(uuid_part, token)
    if not character:
        raise ValueError("DataCat не вернул карточку персонажа.")

    card = _datacat_card_from_character(character)
    page_url = _datacat_character_url(character_id)
    image_url = _datacat_image_url(character, card)
    image_body = _download_url_image_body(image_url, page_url)
    try:
        _write_chara_png(image_body, card, file_path)
    except BaseException:
        remove_download(file_path)
        raise
    logging.info("DataCat PNG с metadata сохранен: %s", file_path)
    return file_path

def _download_janitor_character_sync(character_id: str) -> str:
    logging.info("Старт скачивания Janitor character_id=%s", character_id)
    load_data()

    page_url = f"https://janitorai.com/characters/{character_id}"
    file_path = _character_file_path()
    browser = _launch()
    payloads: list[Any] = []

    try:
        context = _new_janitor_context(browser)
        if GLOBAL_COOKIES and not _janitor_state_path().is_file():
            logging.info("Добавляем cookies в Janitor browser context: %s параметров", len(GLOBAL_COOKIES))
            context.add_cookies(_browser_cookies())

        page = context.new_page()
        _login_janitor(page)
        _sync_context_cookies(context)

        page.on("response", lambda response: _capture_json_response(response, payloads, ("janitorai.com",)))
        logging.info("Открываем Janitor страницу через Chromium: %s", page_url)
        response = page.goto(page_url, wait_until="domcontentloaded", timeout=45_000)
        require_origin(page)
        if response and response.status in (401, 403):
            raise ValueError("Janitor AI отклонил доступ: проверь авторизацию или Cloudflare/WAF.")
        try:
            page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            logging.info("Janitor networkidle timeout, продолжаем с уже полученными данными")

        user_agent = page.evaluate("navigator.userAgent")
        state = page.evaluate(JANITOR_STATE_SCRIPT)
        raw_character = _extract_janitor_character(state, payloads)
        require_public(raw_character)
        if not _definition_text(raw_character):
            try:
                recovered = extract_definition(page, context, character_id.split("_", 1)[0])
                raw_character.update(recovered)
                raw_character["definition_source"] = "janitor_proxy_prompt"
            except ValueError as exc:
                logging.info("Скрытое определение Janitor недоступно: %s", exc)
            except Exception as exc:
                logging.warning("Не удалось запросить скрытое определение Janitor: %s", type(exc).__name__)
        if get_access_token(page, context):
            _save_janitor_state(context)
        card = _to_chara_card_v2(raw_character)
        image_url = _find_janitor_image(raw_character, state, payloads, character_id)
        if not image_url:
            raise ValueError("Не удалось найти изображение персонажа.")

        image_body = _download_image_body(context, page, image_url, page_url, user_agent)
        _write_chara_png(image_body, card, file_path)
        logging.info("Janitor PNG с metadata сохранен: %s", file_path)
        return file_path
    except BaseException:
        remove_download(file_path)
        raise
    finally:
        logging.info("Закрываем Chromium")
        browser.close()


def _download_candidate_sync(
    downloader: Callable[[str], str], character_id: str, source: DownloadSource,
) -> Candidate:
    path = downloader(character_id)
    try:
        if os.path.getsize(path) > IMAGE_RESPONSE_LIMIT:
            raise ValueError("Карточка слишком большая.")
        card = _read_card(path)
        definition_available = _card_definition_available(card.get("data", card))
        notice = None
        if source == DownloadSource.PROXY and definition_available and not has_verified_hidden_definition(path):
            notice = "Открытое определение получено с JanitorAI."
        _mark_download(DownloadResult(path, source, definition_available, notice))
        return Candidate(path, definition_available, notice)
    except BaseException:
        remove_download(path)
        raise


async def _download_candidate(
    downloader: Callable[[str], str], character_id: str, source: DownloadSource,
) -> Candidate:
    worker = asyncio.create_task(asyncio.to_thread(_download_candidate_sync, downloader, character_id, source))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        # A thread cannot be killed; retain the download slot until its file can be cleaned.
        try:
            candidate = await asyncio.shield(worker)
            remove_download(candidate.file_path)
        except Exception:
            pass
        raise


async def download_card(
    character_id: str, source: str = "jannyai", on_progress: ProgressCallback | None = None,
    *, isolated: bool = True,
) -> DownloadResult:
    if source.lower().replace(".com", "") not in {"janitorai", "jannyai"}:
        raise ValueError("Неподдерживаемый источник персонажа.")
    downloaders = {
        DownloadSource.DATACAT: partial(_download_candidate, _download_datacat_character_sync,
                                       character_id, DownloadSource.DATACAT),
        DownloadSource.JANNYAI: partial(_download_candidate, _download_janny_character_sync,
                                       character_id, DownloadSource.JANNYAI),
        DownloadSource.PROXY: partial(_download_candidate, _download_janitor_character_sync,
                                     character_id, DownloadSource.PROXY),
    }
    if isolated:
        from download_worker import run_isolated
        downloaders = {stage: partial(run_isolated, character_id, stage.value) for stage in downloaders}
    return await run_download_flow(downloaders, on_progress)


async def download_character(character_id: str, source: str = "jannyai") -> str:
    return (await download_card(character_id, source)).file_path


load_data()
