"""Bounded read-only WSGI API; run behind TLS with gunicorn."""
import hmac
import json
import os
import sqlite3
import time

import database


def application(environ, start_response):
    def respond(status, value):
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        start_response(status, [("Content-Type", "application/json; charset=utf-8"),
                                ("Content-Length", str(len(body))), ("Cache-Control", "no-store")])
        return [body]

    token = os.environ.get("HANZILAB_API_TOKEN", "")
    if not token or not hmac.compare_digest(environ.get("HTTP_AUTHORIZATION", ""), "Bearer " + token):
        return respond("401 Unauthorized", {"error": "Unauthorized"})
    if environ.get("REQUEST_METHOD") != "POST":
        return respond("405 Method Not Allowed", {"error": "Use POST"})
    connection = None
    try:
        size = int(environ.get("CONTENT_LENGTH") or 0)
        if not 0 < size <= 65536:
            return respond("413 Payload Too Large", {"error": "Invalid body size"})
        args = json.loads(environ["wsgi.input"].read(size))
        if not isinstance(args, dict):
            raise TypeError()
        operation = environ.get("PATH_INFO", "")
        connection = database._thread_connection()
        deadline = time.monotonic() + 8
        connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
        def string(key):
            value = args.get(key, "")
            if not isinstance(value, str) or len(value) > 256:
                raise ValueError()
            return value
        def limit(default, maximum):
            value = args.get("limit", default)
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError()
            return value
        if operation == "/v1/search":
            result = database.search_entries(string("query"), limit(50, 100))
        elif operation == "/v1/examples":
            result = database.get_examples(string("hanzi"), limit(6, 20), string("pinyin"))
        elif operation == "/v1/entries":
            words = args.get("words")
            if not isinstance(words, list) or len(words) > 900 or any(not isinstance(w, str) or len(w) > 256 for w in words):
                raise ValueError()
            result = database.get_entries_by_hanzi(words)
        elif operation == "/v1/stats":
            result = database.get_stats()
        else:
            return respond("404 Not Found", {"error": "Unknown operation"})
        return respond("200 OK", result)
    except (ValueError, TypeError, KeyError):
        return respond("400 Bad Request", {"error": "Invalid parameters"})
    except sqlite3.Error:
        return respond("503 Service Unavailable", {"error": "Dictionary busy; retry"})
    finally:
        if connection is not None:
            connection.set_progress_handler(None, 0)
