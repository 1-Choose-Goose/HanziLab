import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import database
import dictionary_remote
import dictionary_server


class RemoteDictionaryTests(unittest.TestCase):
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

    def test_database_download_route_streams_sqlite_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "hanzi.db"
            path.write_bytes(b"SQLite format 3\x00payload")
            status = []
            env = {
                "HTTP_AUTHORIZATION": "Bearer test",
                "REQUEST_METHOD": "GET",
                "PATH_INFO": "/v1/database",
            }
            with patch.dict(os.environ, HANZILAB_API_TOKEN="test"), patch.object(database, "DB_PATH", path):
                result = dictionary_server.application(
                    env, lambda value, headers: status.append((value, dict(headers)))
                )
                body = b"".join(result)
            self.assertEqual(status[0][0], "200 OK")
            self.assertEqual(status[0][1]["Content-Length"], str(len(body)))
            self.assertTrue(body.startswith(b"SQLite format 3\x00"))

    def test_download_is_validated_and_installed_atomically(self):
        class Response(io.BytesIO):
            def __init__(self, value: bytes):
                super().__init__(value)
                self.headers = {"Content-Length": str(len(value))}

        with tempfile.TemporaryDirectory() as folder:
            destination = Path(folder) / "data" / "hanzi.db"
            response = Response(b"SQLite format 3\x00payload")
            with patch.object(dictionary_remote, "configuration", return_value={"url": "https://example.test", "token": "test"}), patch.object(dictionary_remote, "urlopen", return_value=response):
                result = dictionary_remote.download_dictionary(destination)
            self.assertEqual(result, destination)
            self.assertEqual(destination.read_bytes(), b"SQLite format 3\x00payload")
            self.assertFalse(destination.with_name("hanzi.db.download").exists())

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

    def test_network_failure_has_actionable_message(self):
        with patch.object(dictionary_remote, "configuration", return_value={"url": "https://example.test", "token": "test"}), patch.object(dictionary_remote, "_post_json", side_effect=TimeoutError), self.assertRaisesRegex(RuntimeError, "Проверьте подключение"):
            dictionary_remote.call("stats")


if __name__ == "__main__":
    unittest.main()
