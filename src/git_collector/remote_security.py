"""Проверка URL и изоляция Git от неявных транспортов и credentials."""

from __future__ import annotations

import asyncio
import ipaddress
import os
import re
import socket
from urllib.parse import urlsplit, urlunsplit

from .errors import GitCollectorError


def normalize_remote_url(location: str, *, allow_http: bool = False) -> str:
    """Принять clone URL без credentials и вернуть стабильный ключ кэша."""
    try:
        if any(ord(c) <= 32 or ord(c) == 127 for c in location) or "\\" in location:
            raise ValueError
        parsed = urlsplit(location)
        if parsed.scheme not in (("https", "http") if allow_http else ("https",)):
            raise ValueError
        if (not parsed.hostname or parsed.username is not None
                or parsed.password is not None or "?" in location or "#" in location):
            raise ValueError
        host = parsed.hostname.encode("idna").decode("ascii").lower().rstrip(".")
        # Только DNS-имена и канонические IP: исключаем неоднозначные URL libcurl.
        if not re.fullmatch(r"[a-z0-9.-]+", host):
            raise ValueError
        path = parsed.path.rstrip("/")
        if not path or any(part in ("", ".", "..") for part in path[1:].split("/")):
            raise ValueError
        if "%" in path or not re.fullmatch(r"/[A-Za-z0-9_./~@+-]+", path):
            raise ValueError
        port = parsed.port
        authority = host
        if port is not None and port != (443 if parsed.scheme == "https" else 80):
            if port < 1:
                raise ValueError
            authority += f":{port}"
        return urlunsplit((parsed.scheme, authority, path, "", ""))
    except (ValueError, UnicodeError):
        raise ValueError("repository must be a credential-free HTTPS clone URL (HTTP requires allow_http)") from None


async def resolve_public_host(url: str) -> str:
    """Проверить все адреса DNS и закрепить их для последующего HTTP-запроса."""
    parsed = urlsplit(url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        records = await asyncio.get_running_loop().getaddrinfo(
            parsed.hostname, port, type=socket.SOCK_STREAM,
        )
        addresses = sorted({record[4][0] for record in records})
        ips = [ipaddress.ip_address(ip) for ip in addresses]
        if not ips or any(
            not ip.is_global or ip.is_multicast or ip.is_reserved
            or getattr(ip, "is_site_local", False) for ip in ips
        ):
            raise ValueError
    except (OSError, ValueError):
        raise GitCollectorError("remote host must resolve only to public IP addresses") from None
    encoded = ",".join(f"[{ip}]" if ":" in ip else ip for ip in addresses)
    return f"{parsed.hostname}:{port}:{encoded}"


def isolated_git_env(home: str) -> dict[str, str]:
    """Сохранить только окружение запуска ОС; отключить config, prompts и trace."""
    allowed = {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMPDIR", "COMSPEC", "PATHEXT"}
    env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    env.update({
        "HOME": home, "USERPROFILE": home, "XDG_CONFIG_HOME": home,
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never",
        "GIT_ALLOW_PROTOCOL": "https:http", "GIT_HTTP_MAX_REQUESTS": "1",
        "GIT_HTTP_MAX_RETRIES": "0", "LC_ALL": "C",
    })
    return env


def safe_git_options() -> list[str]:
    """Запретить побочные команды, перенаправления и скрытую аутентификацию."""
    settings = (
        "credential.helper=", "credential.interactive=false", "core.hooksPath=",
        "http.followRedirects=false", "http.proxy=", "http.sslVerify=true",
        "http.maxRequests=1", "http.maxRetries=0", "protocol.allow=never",
        "protocol.https.allow=always", "protocol.http.allow=always",
        "fetch.recurseSubmodules=false", "gc.auto=0", "maintenance.auto=false",
    )
    return [arg for setting in settings for arg in ("-c", setting)]
