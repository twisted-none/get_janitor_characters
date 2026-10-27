"""Read a proxy-enabled Janitor character's model-facing definition."""

import base64
import binascii
import json
import re
import time
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4
from urllib.parse import unquote
from security_policy import require_origin, require_public
from download_errors import SourceError
import download_trace as trace


_FETCH_SCRIPT = """
async ({ path, token, method, body, requestId }) => {
  const headers = {
    Accept: path === "/generateAlpha" ? "text/event-stream" : "application/json",
    Authorization: `Bearer ${token}`,
    "x-app-version": "9.0.27",
  };
  if (body !== null) headers["Content-Type"] = "application/json";
  if (requestId) headers["x-request-id"] = requestId;
  const response = await fetch(path, {
    method, headers, credentials: "include",
    body: body === null ? undefined : JSON.stringify(body),
  });
  return { status: response.status, text: await response.text() };
}
"""

_TOKEN_SCRIPT = """
() => {
  for (const storage of [localStorage, sessionStorage]) {
    for (let i = 0; i < storage.length; i++) {
      const key = storage.key(i) || "";
      if (!key.includes("auth-token")) continue;
      try {
        const session = JSON.parse(storage.getItem(key) || "{}");
        if (session.access_token && (!session.expires_at || session.expires_at * 1000 > Date.now() + 30000)) {
          return session.access_token;
        }
      } catch {}
    }
  }
  return "";
}
"""


def _token_is_live(token: str) -> bool:
    try:
        payload = token.split(".", 2)[1]
        decoded = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        return int(json.loads(decoded).get("exp", 0)) > time.time() + 30
    except (IndexError, ValueError, TypeError, binascii.Error, UnicodeDecodeError):
        return False


def get_access_token(page: Any, context: Any) -> str:
    require_origin(page)
    token = str(page.evaluate(_TOKEN_SCRIPT) or "")
    if _token_is_live(token):
        return token
    for cookie in context.cookies("https://janitorai.com"):
        token = str(cookie.get("value") or "")
        if cookie.get("name") == "sb-access-token" and _token_is_live(token):
            return token
        if str(cookie.get("name") or "").endswith("-auth-token"):
            try:
                session = unquote(token)
                if session.startswith("base64-"):
                    encoded = session[7:]
                    session = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode()
                token = str(json.loads(session).get("access_token") or "")
                if _token_is_live(token):
                    return token
            except (ValueError, TypeError, binascii.Error, UnicodeDecodeError):
                continue
    return ""


@trace.traced('janitor_api')
def _api_json(page: Any, token: str, path: str, method: str = "GET", body: Any = None) -> dict[str, Any]:
    require_origin(page)
    if not path.startswith(("/mb/characters/", "/mb/chats", "/generateAlpha")) or ".." in path:
        raise ValueError("Недопустимый путь Janitor API")
    trace.emit('request', operation='janitor_api', method=method, endpoint=trace.endpoint('https://janitorai.com' + path))
    result = page.evaluate(_FETCH_SCRIPT, {
        "path": path, "token": token, "method": method, "body": body,
        "requestId": str(uuid4()) if path == "/generateAlpha" else "",
    })
    trace.emit('response', operation='janitor_api', http_status=result['status'], bytes=len(result['text']))
    if result['status'] == 401:
        raise SourceError('auth_required', http_status=401)
    if result['status'] == 403:
        raise SourceError('access_denied', http_status=403)
    if not 200 <= result["status"] < 300:
        raise SourceError("http_error", http_status=result["status"])
    if len(result["text"]) > 8_000_000:
        raise SourceError("response_too_large", "Ответ Janitor слишком большой.")
    try:
        payload = json.loads(result["text"])
    except json.JSONDecodeError as exc:
        raise SourceError("invalid_response", http_status=result["status"]) from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Janitor {path}: неверный формат ответа")
    return payload


def parse_definition(system_content: str, character_name: str) -> dict[str, str]:
    result: dict[str, str] = {}
    persona = re.search(r"<([^<>/]+)'s Persona>(.*?)</\1's Persona>", system_content, re.DOTALL | re.IGNORECASE)
    if not persona and character_name:
        escaped = re.escape(character_name)
        persona = re.search(rf"<{escaped}>(.*?)</{escaped}>", system_content, re.DOTALL)
    if persona:
        result["personality"] = persona.group(persona.lastindex or 1).strip()
    for field, tag in (("scenario", "Scenario"), ("example_dialogs", "example_dialogs")):
        match = re.search(rf"<{tag}>(.*?)</{tag}>", system_content, re.DOTALL | re.IGNORECASE)
        if match:
            result[field] = match.group(1).strip()
    if not result.get("personality"):
        raise SourceError('definition_missing', 'Janitor не вернул определение персонажа.')
    return result


@trace.traced('proxy_definition')
def extract_definition(page: Any, context: Any, character_id: str) -> dict[str, str]:
    token = get_access_token(page, context)
    if not token:
        raise SourceError("auth_required", "Для скрытого определения нужна авторизованная сессия Janitor")
    character = _api_json(page, token, f"/mb/characters/{character_id}")
    require_public(character)
    if character.get("allow_proxy") is not True:
        raise SourceError("proxy_disabled", "Автор персонажа отключил внешний прокси")
    chat = _api_json(page, token, "/mb/chats", "POST", {"character_id": character_id})
    chat_id, user_id = chat.get("id"), chat.get("user_id")
    if not chat_id or not user_id:
        raise ValueError("Janitor не вернул идентификаторы нового чата")
    chat_info = _api_json(page, token, f"/mb/chats/{chat_id}")
    messages = chat_info.get("chatMessages") or []
    first = messages[0] if isinstance(messages, list) and messages and isinstance(messages[0], dict) else None
    chat_messages = []
    if first:
        chat_messages.append({
            "character_id": character_id, "chat_id": chat_id,
            "created_at": first.get("created_at"), "id": first.get("id"),
            "is_bot": True, "is_main": True, "message": first.get("message"),
        })
    first_id = first.get("id") if first else None
    chat_messages.append({
        "chat_id": chat_id, "created_at": datetime.now(timezone.utc).isoformat(),
        "id": first_id + 1 if isinstance(first_id, int) else 1,
        "is_bot": False, "is_main": True, "message": "hi",
    })
    profile = {"id": user_id, "name": "User", "user_name": "User"}
    body = {
        "chat": {"character_id": character_id, "id": chat_id, "summary": "", "user_id": user_id},
        "chatMessages": chat_messages,
        "clientPlatform": "web",
        "forcedPromptGenerationCacheRefetch": dict.fromkeys(("character", "chat", "profile", "script"), False),
        "generateMode": "NEW", "generateType": "CHAT",
        "profile": profile,
        "profiles": [{**profile, "type": "profile"}],
        "userConfig": {
            "api": "openai", "open_ai_mode": "proxy",
            "open_ai_reverse_proxy": "", "reverseProxyKey": "",
            "openAIKey": None, "openAiModel": "gpt-4",
            "open_ai_jailbreak_prompt": "", "llm_prompt": "", "claudeApiKey": None,
            "generation_settings": {
                "context_length": 16384, "max_new_token": 500, "temperature": 0.9,
                "enable_reasoning": True, "enable_reasoning_chat": False,
                "enable_thinking": True, "prefill_enabled": False, "prefill_text": "",
            },
        },
    }
    response = _api_json(page, token, "/generateAlpha", "POST", body)
    response_messages = response.get("messages")
    if not isinstance(response_messages, list):
        raise SourceError('definition_missing', 'Janitor не вернул сообщения модели.')
    system = next((message.get("content", "") for message in response_messages
                   if isinstance(message, dict) and message.get("role") == "system"), "")
    if not isinstance(system, str):
        raise SourceError('invalid_response', 'Janitor вернул неподдерживаемый формат системного текста.')
    definition = parse_definition(system, str(character.get("chat_name") or character.get("name") or ""))
    if first and isinstance(first.get("message"), str):
        definition["first_message"] = first["message"]
    return definition
