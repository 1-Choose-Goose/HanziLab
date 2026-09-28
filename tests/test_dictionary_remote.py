import io
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import database
import dictionary_remote
import dictionary_server
from scripts.dictionary_schema import create_dictionary_schema


class RemoteDictionaryTests(unittest.TestCase):
    @staticmethod
    def dictionary_bytes(path: Path) -> bytes:
        with closing(sqlite3.connect(path)) as connection, connection:
            create_dictionary_schema(connection)
        return path.read_bytes()

    def test_frozen_app_finds_configuration_in_bundled_resources(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            executable_root = root / "MacOS"
            resource_root = root / "Frameworks"
            resource_root.mkdir()
            bundled_config = resource_root / "dictionary-server.json"
            bundled_config.write_text("{}", encoding="utf-8")
            with (
                patch.object(dictionary_remote.sys, "frozen", True, create=True),
                patch.object(dictionary_remote, "EXECUTABLE_ROOT", executable_root),
                patch.object(dictionary_remote, "RESOURCE_ROOT", resource_root),
            ):
                self.assertEqual(
                    dictionary_remote.configuration_path(), bundled_config
                )

    def test_routes_all_dictionary_operations(self):
        with patch.object(dictionary_remote, "enabled", return_value=True), patch.object(dictionary_remote, "call", return_value=[]) as call:
            database.search_entries("学校")
            call.assert_called_with("search", query="学校", limit=50)
            database.get_examples("学校")
            call.assert_called_with("examples", hanzi="学校", limit=6, pinyin="")
            database.get_stats()
            call.assert_called_with("stats")
            database.warm_search_index()

    def test_bulk_requests_are_bounded_and_merged(self):
        with patch.object(dictionary_remote, "enabled", return_value=True), patch.object(dictionary_remote, "call", return_value={"学": {"id": 1}}) as call:
            result = database.get_entries_by_hanzi([str(i) for i in range(250)])
            self.assertEqual(call.call_count, 3)
            self.assertEqual(result["学"]["id"], 1)

    def request(self, body, path="/v1/search", token="test", method="POST"):
        raw = json.dumps(body).encode()
        status = []
        env = {"HTTP_AUTHORIZATION": "Bearer " + token, "REQUEST_METHOD": method,
               "CONTENT_LENGTH": str(len(raw)), "wsgi.input": io.BytesIO(raw), "PATH_INFO": path}
        with patch.dict(os.environ, HANZILAB_API_TOKEN="test"), patch.object(database, "_thread_connection"):
            result = dictionary_server.application(env, lambda value, headers: status.append(value))
        return status[0], json.loads(b"".join(result))

    def test_authentication_and_validation(self):
        self.assertEqual(self.request({}, token="wrong")[0], "401 Unauthorized")
        self.assertEqual(self.request({}, method="DELETE")[0], "405 Method Not Allowed")
        self.assertEqual(self.request({"query": "x" * 257})[0], "400 Bad Request")
        self.assertEqual(self.request({"query": "学", "limit": -1})[0], "400 Bad Request")
        self.assertEqual(self.request({}, path="/v1/sql")[0], "404 Not Found")

    def test_success_preserves_unicode_and_result(self):
        with patch.object(database, "search_entries", return_value=[{"hanzi": "学校"}]):
            status, value = self.request({"query": "学校"})
        self.assertEqual(status, "200 OK")
        self.assertEqual(value, [{"hanzi": "学校"}])

    def test_server_does_not_distribute_database_file(self):
        self.assertEqual(self.request({}, path="/v1/database", method="GET")[0], "405 Method Not Allowed")

    def test_download_is_validated_and_installed_atomically(self):
        class Response(io.BytesIO):
            def __init__(self, value: bytes):
                super().__init__(value)
                self.headers = {"Content-Length": str(len(value))}

        with tempfile.TemporaryDirectory() as folder:
            destination = Path(folder) / "data" / "hanzi.db"
            payload = self.dictionary_bytes(Path(folder) / "source.db")
            response = Response(payload)
            config = {"download": {"provider": "yandex_disk", "public_url": "https://disk.yandex.ru/d/test", "password": "secret"}}
            with patch.object(dictionary_remote, "configuration", return_value=config), patch.object(dictionary_remote, "_resolve_yandex_download", return_value="https://downloader.test/hanzi.db"), patch.object(dictionary_remote, "urlopen", return_value=response):
                result = dictionary_remote.download_dictionary(destination)
            self.assertEqual(result, destination)
            self.assertEqual(destination.read_bytes(), payload)
            self.assertFalse(destination.with_name("hanzi.db.download").exists())

    def test_download_rejects_header_only_database_and_preserves_installed_file(self):
        class Response(io.BytesIO):
            def __init__(self, value: bytes):
                super().__init__(value)
                self.headers = {"Content-Length": str(len(value))}

        with tempfile.TemporaryDirectory() as folder:
            destination = Path(folder) / "data" / "hanzi.db"
            destination.parent.mkdir()
            destination.write_bytes(b"existing dictionary")
            response = Response(b"SQLite format 3\x00payload")
            config = {"download": {"provider": "yandex_disk", "public_url": "https://disk.yandex.ru/d/test", "password": "secret"}}
            with patch.object(dictionary_remote, "configuration", return_value=config), patch.object(dictionary_remote, "_resolve_yandex_download", return_value="https://downloader.test/hanzi.db"), patch.object(dictionary_remote, "urlopen", return_value=response), self.assertRaisesRegex(RuntimeError, "повреждённый"):
                dictionary_remote.download_dictionary(destination)
            self.assertEqual(destination.read_bytes(), b"existing dictionary")
            self.assertFalse(destination.with_name("hanzi.db.download").exists())

    def test_download_resumes_an_existing_partial_file(self):
        class Response(io.BytesIO):
            status = 206

            def __init__(self, value: bytes):
                super().__init__(value)
                self.headers = {"Content-Length": str(len(value))}

        with tempfile.TemporaryDirectory() as folder:
            destination = Path(folder) / "data" / "hanzi.db"
            destination.parent.mkdir()
            partial = destination.with_name("hanzi.db.download")
            payload = self.dictionary_bytes(Path(folder) / "source.db")
            split_at = len(payload) // 2
            partial.write_bytes(payload[:split_at])
            response = Response(payload[split_at:])
            config = {"download": {"provider": "yandex_disk", "public_url": "https://disk.yandex.ru/d/test", "password": "secret"}}
            with patch.object(dictionary_remote, "configuration", return_value=config), patch.object(dictionary_remote, "_resolve_yandex_download", return_value="https://downloader.test/hanzi.db"), patch.object(dictionary_remote, "urlopen", return_value=response) as open_url:
                dictionary_remote.download_dictionary(destination)
            self.assertEqual(destination.read_bytes(), payload)
            self.assertEqual(
                open_url.call_args.args[0].get_header("Range"),
                f"bytes={split_at}-",
            )

    def test_download_reconnects_and_resumes_after_network_interruption(self):
        with tempfile.TemporaryDirectory() as payload_folder:
            payload = self.dictionary_bytes(Path(payload_folder) / "source.db")
        split_at = len(payload) // 2
        first_part = payload[:split_at]
        second_part = payload[split_at:]
        total = len(first_part) + len(second_part)

        class InterruptedResponse(io.BytesIO):
            status = 200

            def __init__(self):
                super().__init__(first_part)
                self.headers = {"Content-Length": str(total)}
                self.finished_data = False

            def read(self, size=-1):
                if self.finished_data:
                    raise TimeoutError("connection interrupted")
                value = super().read(size)
                self.finished_data = True
                return value

        class ResumedResponse(io.BytesIO):
            status = 206

            def __init__(self):
                super().__init__(second_part)
                self.headers = {
                    "Content-Length": str(len(second_part)),
                    "Content-Range": (
                        f"bytes {len(first_part)}-{total - 1}/{total}"
                    ),
                }

        with tempfile.TemporaryDirectory() as folder:
            destination = Path(folder) / "data" / "hanzi.db"
            config = {
                "download": {
                    "provider": "yandex_disk",
                    "public_url": "https://disk.yandex.ru/d/test",
                    "password": "secret",
                }
            }
            with (
                patch.object(dictionary_remote, "configuration", return_value=config),
                patch.object(
                    dictionary_remote,
                    "_resolve_yandex_download",
                    return_value="https://downloader.test/hanzi.db",
                ) as resolve,
                patch.object(
                    dictionary_remote,
                    "urlopen",
                    side_effect=[InterruptedResponse(), ResumedResponse()],
                ) as open_url,
                patch.object(dictionary_remote.time, "sleep"),
            ):
                dictionary_remote.download_dictionary(destination)

            self.assertEqual(destination.read_bytes(), payload)
            self.assertEqual(resolve.call_count, 2)
            self.assertEqual(open_url.call_count, 2)
            self.assertEqual(
                open_url.call_args_list[1].args[0].get_header("Range"),
                f"bytes={len(first_part)}-",
            )

    def test_json_requests_reuse_one_https_connection_per_worker(self):
        class Response:
            status = 200

            @staticmethod
            def read():
                return b'{"entries": 1}'

        class Connection:
            def __init__(self):
                self.requests = 0

            def request(self, *_args, **_kwargs):
                self.requests += 1

            @staticmethod
            def getresponse():
                return Response()

            @staticmethod
            def close():
                pass

        connection = Connection()
        context = object()
        dictionary_remote._THREAD_CONNECTION.holder = None
        with patch.object(
            dictionary_remote, "HTTPSConnection", return_value=connection
        ) as constructor:
            first = dictionary_remote._post_json(
                "https://example.test", "token", "stats", {}, context
            )
            second = dictionary_remote._post_json(
                "https://example.test", "token", "stats", {}, context
            )
        self.assertEqual(first, {"entries": 1})
        self.assertEqual(second, first)
        self.assertEqual(constructor.call_count, 1)
        self.assertEqual(connection.requests, 2)
        dictionary_remote._THREAD_CONNECTION.holder = None

    def test_json_request_can_target_a_separate_api_virtual_host(self):
        class Response:
            status = 200

            @staticmethod
            def read():
                return b'{"entries": 1}'

        class Connection:
            request_args = None

            def request(self, *args, **kwargs):
                self.request_args = (args, kwargs)

            @staticmethod
            def getresponse():
                return Response()

            @staticmethod
            def close():
                pass

        connection = Connection()
        dictionary_remote._THREAD_CONNECTION.holder = None
        with patch.object(
            dictionary_remote, "HTTPSConnection", return_value=connection
        ):
            dictionary_remote._post_json(
                "https://choose-goose.ru",
                "token",
                "stats",
                {},
                object(),
                "178.217.99.218",
            )

        self.assertEqual(
            connection.request_args[1]["headers"]["Host"], "178.217.99.218"
        )
        dictionary_remote._THREAD_CONNECTION.holder = None

    def test_network_failure_has_actionable_message(self):
        with patch.object(dictionary_remote, "configuration", return_value={"url": "https://example.test", "token": "test"}), patch.object(dictionary_remote, "_post_json", side_effect=TimeoutError), self.assertRaisesRegex(RuntimeError, "Проверьте подключение"):
            dictionary_remote.call("stats")


if __name__ == "__main__":
    unittest.main()
