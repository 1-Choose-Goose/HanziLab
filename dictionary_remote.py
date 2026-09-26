"""HTTPS transport for the shared, read-only dictionary."""
from __future__ import annotations

import json
import os
import re
import shutil
import ssl
import sys
import threading
import time
from functools import lru_cache
from http.client import HTTPException, HTTPSConnection
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import (
    HTTPCookieProcessor,
    HTTPSHandler,
    Request,
    build_opener,
    urlopen,
)

import certifi

from app_paths import RESOURCE_ROOT


SOURCE_ROOT = Path(__file__).resolve().parent
EXECUTABLE_ROOT = Path(sys.executable).resolve().parent
_THREAD_CONNECTION = threading.local()


def configuration_path() -> Path:
    roots = (
        (EXECUTABLE_ROOT, RESOURCE_ROOT)
        if getattr(sys, "frozen", False)
        else (SOURCE_ROOT,)
    )
    return next(
        (root / "dictionary-server.json" for root in roots if (root / "dictionary-server.json").is_file()),
        roots[0] / "dictionary-server.json",
    )


@lru_cache(maxsize=1)
def _load_configuration(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def configuration() -> dict:
    if os.environ.get("HANZILAB_DICTIONARY_LOCAL") == "1":
        return {}
    path = configuration_path()
    return _load_configuration(path) if path.exists() else {}


@lru_cache(maxsize=2)
def _ssl_context(ca_file: str) -> ssl.SSLContext:
    return ssl.create_default_context(cafile=ca_file or certifi.where())


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
    ca_file = (
        str(configuration_path().parent / config["ca_file"])
        if config.get("ca_file")
        else ""
    )
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


def _yandex_api(opener, method: str, parameters: dict, public_url: str) -> dict:
    body = quote(json.dumps(parameters, ensure_ascii=False, separators=(",", ":"))).encode()
    request = Request(
        "https://disk.yandex.ru/public/api/" + method,
        data=body,
        headers={
            "Content-Type": "text/plain",
            "User-Agent": "Mozilla/5.0 HanziLab",
            "X-Requested-With": "XMLHttpRequest",
            "X-Retpath-Y": public_url,
        },
    )
    try:
        with opener.open(request, timeout=30) as response:
            return json.load(response)
    except HTTPError as error:
        try:
            return json.loads(error.read())
        except (json.JSONDecodeError, UnicodeDecodeError) as decode_error:
            raise OSError(f"Яндекс Диск вернул HTTP {error.code}") from decode_error


def _page_value(page: str, name: str) -> str:
    match = re.search(rf'"{re.escape(name)}":"([^"\\]*(?:\\.[^"\\]*)*)"', page)
    if not match:
        raise OSError("Яндекс Диск вернул страницу без данных файла")
    return json.loads('"' + match.group(1) + '"')


def _resolve_yandex_download(public_url: str, password: str, context) -> str:
    parsed_url = urlsplit(public_url)
    if parsed_url.scheme != "https" or parsed_url.hostname not in {"disk.yandex.ru", "disk.yandex.com"}:
        raise RuntimeError("Некорректная ссылка на Яндекс Диск")
    cookies = CookieJar()
    opener = build_opener(HTTPCookieProcessor(cookies), HTTPSHandler(context=context))
    page_request = Request(public_url, headers={"User-Agent": "Mozilla/5.0 HanziLab"})
    with opener.open(page_request, timeout=30) as response:
        page = response.read().decode("utf-8")
    parameters = {
        "hash": _page_value(page, "hash"),
        "password": password,
        "short_url": urlsplit(public_url).path,
        "sk": _page_value(page, "sk"),
    }
    unlocked = _yandex_api(opener, "check-password", parameters, public_url)
    if unlocked.get("wrongSk") and unlocked.get("newSk"):
        parameters["sk"] = unlocked["newSk"]
        unlocked = _yandex_api(opener, "check-password", parameters, public_url)
    token = unlocked.get("token")
    if not token:
        raise OSError("Не удалось открыть защищённую ссылку Яндекс Диска")

    # The web client stores this short-lived token before requesting a download URL.
    parameters = {"hash": parameters["hash"], "sk": parameters["sk"], "passToken": token}
    resolved = _yandex_api(opener, "download-url", parameters, public_url)
    if resolved.get("wrongSk") and resolved.get("newSk"):
        parameters["sk"] = resolved["newSk"]
        resolved = _yandex_api(opener, "download-url", parameters, public_url)
    download_url = (resolved.get("data") or {}).get("url")
    if not isinstance(download_url, str) or not download_url.startswith("https://"):
        raise OSError("Яндекс Диск не выдал ссылку на скачивание")
    return download_url


def download_dictionary(destination: Path, progress=None, cancelled=None) -> Path:
    """Atomically download the shared SQLite database for offline use."""
    config = configuration()
    download = config.get("download") or {}
    public_url = download.get("public_url", "")
    password = download.get("password", "")
    if download.get("provider") != "yandex_disk" or not public_url or not password:
        raise RuntimeError("Источник загрузки словаря не настроен")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".download")
    context = _ssl_context("")
    last_error: Exception | None = None

    for attempt in range(10):
        if cancelled and cancelled():
            raise RuntimeError("Загрузка отменена")
        offset = temporary.stat().st_size if temporary.exists() else 0
        try:
            # Yandex download links are short-lived. Resolve a fresh one for
            # every reconnect, then continue from the last byte on disk.
            download_url = _resolve_yandex_download(public_url, password, context)
            headers = {"User-Agent": "HanziLab"}
            if offset:
                headers["Range"] = f"bytes={offset}-"
            request = Request(download_url, headers=headers)
            with urlopen(request, timeout=120, context=context) as response:
                partial = bool(
                    offset and getattr(response, "status", 200) == 206
                )
                if not partial:
                    offset = 0
                remaining = int(response.headers.get("Content-Length") or 0)
                content_range = response.headers.get("Content-Range", "")
                range_total = re.search(r"/(\d+)$", content_range)
                total = (
                    int(range_total.group(1))
                    if range_total
                    else offset + remaining if remaining else 0
                )
                needed = max(total - offset, remaining)
                if needed and shutil.disk_usage(destination.parent).free < needed + 256 * 1024 * 1024:
                    raise RuntimeError(
                        "Недостаточно места: для локального словаря нужно около 2,5 ГБ."
                    )
                downloaded = offset
                with temporary.open("ab" if partial else "wb") as stream:
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
                raise OSError("Файл словаря загружен не полностью")
            with temporary.open("rb") as stream:
                if stream.read(16) != b"SQLite format 3\x00":
                    temporary.unlink(missing_ok=True)
                    raise RuntimeError(
                        "Яндекс Диск вернул повреждённый файл словаря."
                    )
            temporary.replace(destination)
            return destination
        except RuntimeError as error:
            if str(error) in {
                "Загрузка отменена",
                "Недостаточно места: для локального словаря нужно около 2,5 ГБ.",
                "Яндекс Диск вернул повреждённый файл словаря.",
            }:
                raise
            last_error = error
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as error:
            last_error = error

        if attempt < 9:
            delay = min(2 ** attempt, 15)
            for _ in range(delay * 4):
                if cancelled and cancelled():
                    raise RuntimeError("Загрузка отменена")
                time.sleep(0.25)

    raise RuntimeError(
        "Загрузка несколько раз прервалась. Уже скачанная часть сохранена — повторите попытку позже."
    ) from last_error


def check_dictionary_download() -> None:
    """Verify credentials and the SQLite header without downloading the database."""
    config = configuration()
    download = config.get("download") or {}
    public_url = download.get("public_url", "")
    password = download.get("password", "")
    if download.get("provider") != "yandex_disk" or not public_url or not password:
        raise RuntimeError("Источник загрузки словаря не настроен")
    context = _ssl_context("")
    download_url = _resolve_yandex_download(public_url, password, context)
    request = Request(
        download_url,
        headers={"User-Agent": "HanziLab", "Range": "bytes=0-15"},
    )
    try:
        with urlopen(request, timeout=30, context=context) as response:
            if response.read(16) != b"SQLite format 3\x00":
                raise RuntimeError("Яндекс Диск вернул повреждённый файл словаря.")
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as error:
        raise RuntimeError(
            "Не удалось проверить словарь на Яндекс Диске."
        ) from error
