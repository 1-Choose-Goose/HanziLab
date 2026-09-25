"""HTTPS transport for the shared, read-only dictionary."""
from __future__ import annotations

import json
import os
import shutil
import ssl
import sys
import threading
from functools import lru_cache
from http.client import HTTPException, HTTPSConnection
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

ROOT = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
_THREAD_CONNECTION = threading.local()


@lru_cache(maxsize=1)
def _load_configuration(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def configuration() -> dict:
    if os.environ.get("HANZILAB_DICTIONARY_LOCAL") == "1":
        return {}
    path = ROOT / "dictionary-server.json"
    return _load_configuration(path) if path.exists() else {}


@lru_cache(maxsize=2)
def _ssl_context(ca_file: str) -> ssl.SSLContext:
    return ssl.create_default_context(cafile=ca_file or None)


def enabled() -> bool:
    return bool(configuration().get("url"))


def _post_json(url: str, token: str, operation: str, parameters: dict, context):
    parsed = urlsplit(url)
    key = (parsed.hostname, parsed.port or 443, id(context))
    holder = getattr(_THREAD_CONNECTION, "holder", None)
    if holder is None or holder[0] != key:
        if holder is not None:
            holder[1].close()
        connection = HTTPSConnection(
            parsed.hostname, parsed.port or 443, timeout=12, context=context
        )
        holder = (key, connection)
        _THREAD_CONNECTION.holder = holder
    connection = holder[1]
    path = parsed.path.rstrip("/") + "/v1/" + operation
    body = json.dumps(parameters).encode("utf-8")
    connection.request(
        "POST",
        path,
        body=body,
        headers={
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
            "Authorization": "Bearer " + token,
        },
    )
    response = connection.getresponse()
    payload = response.read()
    if response.status != 200:
        raise OSError(f"Dictionary server returned HTTP {response.status}")
    return json.loads(payload)


def call(operation: str, **parameters):
    config = configuration()
    url = config["url"].rstrip("/")
    if not url.startswith("https://"):
        raise RuntimeError("Сервер словаря должен использовать HTTPS")
    ca_file = str(ROOT / config["ca_file"]) if config.get("ca_file") else ""
    context = _ssl_context(ca_file)
    for attempt in range(2):
        try:
            return _post_json(url, config["token"], operation, parameters, context)
        except (HTTPException, URLError, TimeoutError, OSError, ValueError) as error:
            holder = getattr(_THREAD_CONNECTION, "holder", None)
            if holder is not None:
                holder[1].close()
            _THREAD_CONNECTION.holder = None
            if attempt:
                raise RuntimeError("Сервер словаря недоступен. Проверьте подключение к интернету и повторите запрос.") from error


def download_dictionary(destination: Path, progress=None, cancelled=None) -> Path:
    """Atomically download the shared SQLite database for offline use."""
    config = configuration()
    url = config["url"].rstrip("/")
    if not url.startswith("https://"):
        raise RuntimeError("Сервер словаря должен использовать HTTPS")
    ca_file = str(ROOT / config["ca_file"]) if config.get("ca_file") else ""
    context = _ssl_context(ca_file)
    request = Request(url + "/v1/database", headers={"Authorization": "Bearer " + config["token"]})
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".download")
    try:
        with urlopen(request, timeout=30, context=context) as response:
            total = int(response.headers.get("Content-Length") or 0)
            if total and shutil.disk_usage(destination.parent).free < total + 256 * 1024 * 1024:
                raise RuntimeError("Недостаточно места: для локального словаря нужно около 2,5 ГБ.")
            downloaded = 0
            with temporary.open("wb") as stream:
                while True:
                    if cancelled and cancelled():
                        raise RuntimeError("Загрузка отменена")
                    block = response.read(2 * 1024 * 1024)
                    if not block:
                        break
                    stream.write(block)
                    downloaded += len(block)
                    if progress and total:
                        progress(downloaded, total)
        if total and temporary.stat().st_size != total:
            raise RuntimeError("Файл словаря загружен не полностью.")
        with temporary.open("rb") as stream:
            if stream.read(16) != b"SQLite format 3\x00":
                raise RuntimeError("Сервер вернул повреждённый файл словаря.")
        temporary.replace(destination)
        return destination
    except RuntimeError:
        temporary.unlink(missing_ok=True)
        raise
    except (URLError, TimeoutError, OSError, ValueError) as error:
        temporary.unlink(missing_ok=True)
        raise RuntimeError("Не удалось скачать словарь. Проверьте интернет и свободное место.") from error
