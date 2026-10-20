"""HTTPS downloads with pinned public IPs, bounded bodies and safe redirects."""

import http.client
import ipaddress
import socket
import ssl
from time import monotonic
from urllib.parse import urljoin, urlsplit


MEDIA_DOMAINS = ("janitorai.com", "jannyai.com", "datacat.run", "supabase.co", "supabase.in", "r2.dev")
SITE_DOMAINS = MEDIA_DOMAINS + ("cloudflare.com", "gstatic.com", "googleapis.com", "google.com")


def validate_url(url: str, domains=MEDIA_DOMAINS) -> tuple[str, list[str]]:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if (parsed.scheme != "https" or not host or parsed.username or parsed.password
            or parsed.port not in (None, 443) or "\\" in url
            or any(ord(char) < 33 for char in url)):
        raise ValueError("Недопустимый адрес загрузки.")
    if not any(host == domain or host.endswith("." + domain) for domain in domains):
        raise ValueError("Домен загрузки не разрешён.")
    addresses = list(dict.fromkeys(item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)))
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise ValueError("Внутренние и служебные адреса запрещены.")
    return host, addresses


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, address, timeout):
        super().__init__(host, timeout=timeout, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        sock = socket.create_connection((self.address, 443), self.timeout)
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


def fetch(url: str, *, method="GET", headers=None, body=None, limit=2_000_000,
          timeout=60.0, redirects=4, domains=MEDIA_DOMAINS, return_redirects=False):
    deadline = monotonic() + timeout
    current_headers = dict(headers or {})
    current_headers = {key: value for key, value in current_headers.items() if key.lower() != "accept-encoding"}
    current_headers["Accept-Encoding"] = "identity"
    for hop in range(redirects + 1):
        host, addresses = validate_url(url, domains)
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError("Время загрузки истекло.")
        connection = _PinnedHTTPS(host, addresses[0], min(15, remaining))
        try:
            parsed = urlsplit(url)
            target = (parsed.path or "/") + (("?" + parsed.query) if parsed.query else "")
            connection.request(method, target, body=body, headers=current_headers)
            response = connection.getresponse()
            response_headers = {}
            for key, value in response.getheaders():
                key = key.lower()
                if key == "set-cookie" and key in response_headers:
                    response_headers[key] += "\n" + value
                else:
                    response_headers[key] = value
            if response.status in (301, 302, 303, 307, 308):
                if return_redirects and response_headers.get("location"):
                    validate_url(urljoin(url, response_headers["location"]), domains)
                    return response.status, response_headers, b"", url
                if hop == redirects or not response_headers.get("location"):
                    raise ValueError("Перенаправление загрузки запрещено.")
                destination = urljoin(url, response_headers["location"])
                if urlsplit(destination).netloc != parsed.netloc:
                    current_headers = {key: value for key, value in current_headers.items()
                                       if key.lower() not in {"authorization", "cookie", "x-session-token", "proxy-authorization"}}
                if response.status == 303 or (response.status in (301, 302) and method == "POST"):
                    method, body = "GET", None
                url = destination
                continue
            if response_headers.get("content-encoding", "identity").lower() not in {"identity", ""}:
                raise ValueError("Сжатый ответ загрузки не разрешён.")
            if int(response_headers.get("content-length", "0")) > limit:
                raise ValueError("Ответ слишком большой.")
            chunks, size = [], 0
            while True:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise TimeoutError("Время загрузки истекло.")
                if connection.sock is not None:
                    connection.sock.settimeout(min(15, remaining))
                chunk = response.read1(min(65536, limit + 1 - size))
                if not chunk:
                    return response.status, response_headers, b"".join(chunks), url
                size += len(chunk)
                if size > limit:
                    raise ValueError("Ответ слишком большой.")
                chunks.append(chunk)
        finally:
            connection.close()
    raise ValueError("Слишком много перенаправлений.")
