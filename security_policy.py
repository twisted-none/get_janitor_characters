"""Boundaries for authenticated pages, exported fields and browser traffic."""

import logging
from urllib.parse import urlsplit

from safe_http import SITE_DOMAINS, fetch
from download_errors import SourceError, describe_error


def require_origin(page, expected="https://janitorai.com"):
    actual = urlsplit(str(page.url))
    wanted = urlsplit(expected)
    if (actual.scheme, actual.hostname, actual.port) != (wanted.scheme, wanted.hostname, wanted.port):
        raise SourceError("unsafe_url", "Страница перенаправлена на недопустимый домен.")


def require_public(character):
    for key in ("is_private", "isPrivate", "private"):
        if character.get(key) is True:
            raise SourceError("private_card", "Приватные карточки через общую сессию недоступны.")
    for key in ("is_public", "isPublic", "public"):
        if character.get(key) is False:
            raise SourceError("private_card", "Приватные карточки через общую сессию недоступны.")
    visibility = str(character.get("visibility", "")).lower()
    if visibility and visibility != "public":
        raise SourceError("private_card", "Приватные карточки через общую сессию недоступны.")
    if visibility == "public" or any(character.get(key) is True for key in ("is_public", "isPublic", "public")):
        return
    if any(character.get(key) is False for key in ("is_private", "isPrivate", "private")):
        return
    raise SourceError("visibility_unknown", "Не удалось подтвердить публичность карточки.")


RAW_FIELDS = {
    "name", "title", "description", "personality", "definition", "scenario", "first_mes", "firstMessage",
    "first_message", "mes_example", "message_example", "example_dialogs", "exampleDialogue",
    "definition_source", "is_public", "visibility", "showdefinition",
    "characterDefinition", "character_definition", "hiddenDefinition", "hidden_definition",
    "privateDefinition", "private_definition", "persona",
}
CARD_FIELDS = {
    "name", "description", "personality", "scenario", "first_mes", "mes_example", "creator_notes",
    "system_prompt", "post_history_instructions", "alternate_greetings", "tags", "creator", "character_version",
    "character_book",
}


def clean_card(card):
    data = card.get("data", card)
    result = {key: value for key, value in data.items() if key in CARD_FIELDS}
    extensions = data.get("extensions", {})
    if isinstance(extensions, dict):
        result["extensions"] = {}
        for source, fields in {
            "janitorai": RAW_FIELDS,
            "datacat": {"id", "sourceKind", "creatorId", "creatorName", "definition_available"},
            "download_bot": {"source", "definition_status", "lorebooks", "lorebooks_missing"},
        }.items():
            if isinstance(extensions.get(source), dict):
                result["extensions"][source] = {key: value for key, value in extensions[source].items() if key in fields}
    return {"spec": "chara_card_v2", "spec_version": "2.0", "data": result}


def protect_context(context):
    # Fulfil through pinned HTTPS rather than allowing Chromium a second DNS lookup.
    budget = [0, 0]

    def route_request(route):
        try:
            budget[0] += 1
            if budget[0] > 200:
                raise SourceError("response_too_large", "Слишком много запросов страницы.")
            request = route.request
            headers = request.all_headers()
            status, response_headers, body, _ = fetch(
                request.url, method=request.method, headers=headers, body=request.post_data_buffer,
                domains=SITE_DOMAINS,
                limit=25_000_000 if request.resource_type == "image" else (8_000_000 if request.resource_type in {"script", "document"} else 2_000_000),
                timeout=30, redirects=0, return_redirects=True,
            )
            budget[1] += len(body)
            if budget[1] > 64_000_000:
                raise SourceError("response_too_large", "Страница слишком большая.")
            response_headers = {key: value for key, value in response_headers.items()
                                if key not in {"transfer-encoding", "connection", "content-length"}}
            route.fulfill(status=status, headers=response_headers, body=body)
        except Exception as exc:
            logging.debug("Browser request refused: %s", type(exc).__name__)
            request = route.request
            critical = request.is_navigation_request()
            if not critical and request.resource_type in {"fetch", "xhr"}:
                host = urlsplit(request.url).hostname or ""
                critical = any(host == domain or host.endswith("." + domain)
                               for domain in ("janitorai.com", "jannyai.com", "datacat.run"))
            if critical:
                context._download_failure = describe_error(exc)
            route.abort()

    context.route("**/*", route_request)
    context.route_web_socket("**/*", lambda websocket: websocket.close())
    return context
