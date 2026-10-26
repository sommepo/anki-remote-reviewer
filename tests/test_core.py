"""Regression checks with a fake Anki host; never opens a real collection."""
import json
import socket
import sys
import tempfile
import time
import types
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fake_anki  # noqa: E402

PORT = None


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


PORT = free_port()
mw, backend = fake_anki.load_backend({"port": PORT, "accessToken": "test-token"})
media = sys.modules[backend.__package__ + ".media"]


class MediaTests(unittest.TestCase):
    def test_av_entries_keep_indexes_tts_and_duplicates(self):
        tags = [types.SimpleNamespace(field_text="まど", lang="ja_JP", speed=1.2), fake_anki.Tag("b.mp3"),
                fake_anki.Tag("b.mp3"), types.SimpleNamespace()]
        self.assertEqual(media.av_entries(tags), [
            {"tts": {"text": "まど", "lang": "ja_JP", "speed": 1.2}}, {"file": "b.mp3"}, {"file": "b.mp3"}, None])

    def test_range_requests(self):
        for header, expected in [("bytes=0-1", (0, 1)), ("bytes=5-", (5, 9)), ("bytes=-3", (7, 9)), ("bytes=2-50", (2, 9))]:
            self.assertEqual(media.byte_range(header, 10), expected)
        for header in ["bytes=20-", "bytes=3-1", "bytes=-0", "bytes=0-1,4-5"]:
            with self.assertRaises(ValueError):
                media.byte_range(header, 10)

    def test_paths_cannot_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "sub").mkdir()
            Path(directory, "sub", "ok.js").write_text("x")
            self.assertTrue(media.safe_asset(directory, "sub/ok.js").is_file())
            for name in ["../secret", "%2e%2e%5csecret", "C%3Asecret", "x%00.mp3", "/secret"]:
                with self.assertRaises(ValueError):
                    media.safe_media_file(directory, name)
            for name in ["../x", "sub/../../x", "sub//ok.js", "%2e%2e/x", "sub\\ok.js", "C:/x"]:
                with self.assertRaises(ValueError):
                    media.safe_asset(directory, name)

    def test_scripts_are_served_as_javascript(self):
        # Windows can map .js to text/plain, which nosniff refuses to run.
        self.assertTrue(media.content_type("_kanjiHover.js").startswith("text/javascript"))
        self.assertEqual(media.content_type("font.woff2"), "font/woff2")


class ConfigTests(unittest.TestCase):
    def test_legacy_autoplay_and_defaults(self):
        config, changed, problems = backend.load_config({"autoPlayAudioOnAnswer": False})
        self.assertTrue(changed)
        self.assertFalse(config["autoplayAudio"])
        self.assertNotIn("autoPlayAudioOnAnswer", config)
        self.assertTrue(config["accessToken"] and config["cookieSecret"])
        self.assertEqual(problems, [])

    def test_bad_values_disable_features_instead_of_crashing(self):
        config, _, problems = backend.load_config({"port": 8765, "lanAddress": "8.8.8.8", "lanSubnet": "8.8.8.0/24"})
        self.assertFalse(config["lanEnabled"])
        self.assertEqual(len(problems), 2)
        config, _, problems = backend.load_config({"lanAddress": "192.168.1.5", "lanSubnet": "192.168.1.0/24",
                                                   "requireAccessToken": False, "requestTimeoutSeconds": 999})
        self.assertTrue(config["lanEnabled"] and config["requireAccessToken"])
        self.assertEqual(config["requestTimeoutSeconds"], 30)
        self.assertEqual(problems, [])


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.server = backend.RemoteReviewer.__new__(backend.RemoteReviewer)
        self.server._config = {"accessToken": "test-token", "cookieSecret": "secret", "requireAccessToken": True}

    def test_cookie_survives_restart_and_rejects_tampering(self):
        cookie = self.server.issue_session("test-token")
        handler = types.SimpleNamespace(headers={"Cookie": "anki_remote_session=" + cookie})
        self.assertTrue(self.server.is_authenticated(handler))
        handler.headers["Cookie"] += "x"
        self.assertFalse(self.server.is_authenticated(handler))
        with patch.object(backend.time, "time", return_value=time.time() + 91 * 86400):
            handler.headers["Cookie"] = "anki_remote_session=" + cookie
            self.assertFalse(self.server.is_authenticated(handler))
        with self.assertRaises(backend.ReviewError):
            self.server.issue_session("wrong")

    def test_frame_tokens_are_separate_and_expire(self):
        token = self.server.frame_token()
        self.assertTrue(self.server.valid_frame_token(token))
        self.assertEqual(token, self.server.frame_token(), "stable within a day so the card frame persists")
        self.assertFalse(self.server.valid_frame_token(self.server.issue_session("test-token")))
        handler = types.SimpleNamespace(headers={"Cookie": "anki_remote_session=" + token})
        self.assertFalse(self.server.is_authenticated(handler), "a media token never signs in")
        with patch.object(backend.time, "time", return_value=time.time() + 10 * 86400):
            self.assertFalse(self.server.valid_frame_token(token))


class LanTests(unittest.TestCase):
    def handler(self, lan):
        handler = backend.RemoteReviewHandler.__new__(backend.RemoteReviewHandler)
        handler.server = types.SimpleNamespace(server_address=("192.168.1.248", 28594),
            allowed_network=backend.ipaddress.ip_network("192.168.1.0/24") if lan else None)
        handler.headers = {"Host": "192.168.1.248:28594", "X-Forwarded-Proto": "http"}
        return handler

    def test_secure_cookie_only_relaxed_on_explicit_lan_socket(self):
        self.assertIn("Secure;", self.handler(False)._session_cookie_flags())
        self.assertNotIn("Secure;", self.handler(True)._session_cookie_flags())

    def test_without_token_only_loopback_or_tailscale_hosts(self):
        handler = self.handler(False)
        handler.reviewer = types.SimpleNamespace(_config={"requireAccessToken": False})
        for host, ok in [("127.0.0.1:28594", True), ("localhost:28594", True), ("pc.tail1234.ts.net", True),
                         ("rebind.evil.example:28594", False)]:
            handler.headers["Host"] = host
            self.assertEqual(handler._valid_host(), ok, host)

    def test_addon_urls_in_cards_use_the_signed_frame_path(self):
        text = '<img src="/_addons/bg/user_files/a.png"><div style="background:url(/_addons/bg/b.png)">'
        self.assertEqual(backend.ADDON_URL.sub("/c/T/_addons/", text).count("/c/T/_addons/"), 2)

    def test_lan_rejects_unexpected_host_and_outside_clients(self):
        handler = self.handler(True)
        self.assertTrue(handler._valid_lan_host())
        handler.headers["Host"] = "untrusted.example:28594"
        self.assertFalse(handler._valid_lan_host())
        server = backend.ReviewerHTTPServer.__new__(backend.ReviewerHTTPServer)
        server.allowed_network = backend.ipaddress.ip_network("192.168.1.0/24")
        self.assertTrue(server.verify_request(None, ("192.168.1.50", 1)))
        self.assertFalse(server.verify_request(None, ("10.0.0.1", 1)))


class ServerTests(unittest.TestCase):
    """The real HTTP server and reviewer logic against the fake fork reviewer."""

    @classmethod
    def setUpClass(cls):
        with patch.object(backend, "check_port", lambda port: None):
            cls.reviewer = backend.RemoteReviewer()
        cls.base = f"http://127.0.0.1:{PORT}"
        cls.cookie = ""

    @classmethod
    def tearDownClass(cls):
        cls.reviewer.stop()

    def request(self, path, body=None, headers=None, auth=True):
        headers = {**({"Cookie": self.cookie} if auth and self.cookie else {}), **(headers or {})}
        if body is not None:
            headers.setdefault("Content-Type", "application/json")
        request = urllib.request.Request(self.base + path, data=None if body is None else json.dumps(body).encode(),
                                         headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as error:
            return error.code, dict(error.headers), error.read()

    def api(self, path, body=None):
        status, _, data = self.request(path, body)
        return status, json.loads(data)

    def login(self):
        status, headers, _ = self.request("/api/login", {"token": "test-token"}, auth=False)
        self.assertEqual(status, 200)
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        type(self).cookie = headers["Set-Cookie"].split(";")[0]

    def test_review_flow(self):
        self.assertEqual(self.request("/api/state", auth=False)[0], 403)
        self.login()
        status, state = self.api("/api/state")
        self.assertEqual((status, state["status"]), (200, "idle"))
        self.assertEqual([d["name"] for d in state["decks"]], ["Japanese", "Mining"])

        # Cross-origin and stale requests never reach the reviewer.
        status, _, _ = self.request("/api/start", {"deckId": 2, "revision": state["revision"]},
                                    headers={"Origin": "https://evil.example"})
        self.assertEqual(status, 409)
        self.assertEqual(self.api("/api/start", {"deckId": 2, "revision": "old"})[0], 409)

        # The first card of a session is ignored by the fork for a moment;
        # starting and revealing straight away must still work first time.
        status, state = self.api("/api/start", {"deckId": 2, "revision": state["revision"]})
        self.assertEqual((status, state["status"]), (200, "question"))
        self.assertIn("_hover.js", state["html"])
        self.assertIn("play:q:0", state["html"], "card text is what desktop Anki shows")
        self.assertEqual(state["audio"]["q"], [{"file": "word heya.wav"}])
        self.assertTrue(mw.reviewer._review_actions_are_blocked())
        status, answer = self.api("/api/reveal", {"cardId": state["cardId"], "revision": state["revision"]})
        self.assertEqual((status, answer["status"]), (200, "answer"))
        self.assertEqual([b["interval"] for b in answer["buttons"]], ["<1m", "6m", "1d", "4d"])
        self.assertEqual(answer["counts"], {"new": 0, "learn": 0, "review": 3, "current": "review"})

        # Repeating a stale action is rejected rather than answering twice.
        self.assertEqual(self.api("/api/reveal", {"cardId": state["cardId"], "revision": state["revision"]})[0], 409)
        status, nxt = self.api("/api/answer", {"cardId": answer["cardId"], "ease": 3, "revision": answer["revision"]})
        self.assertEqual((status, nxt["cardId"]), (200, 102))
        self.assertEqual(self.api("/api/answer", {"cardId": answer["cardId"], "ease": 3, "revision": answer["revision"]})[0], 409)
        self.assertEqual(mw.answers, [(101, 3)])
        self.assertTrue(nxt["canUndo"])
        self.assertEqual(nxt["audio"]["q"][0]["tts"]["text"], "まど")

        # Typed answers reach the desktop reviewer.
        status, typed = self.api("/api/reveal", {"cardId": 102, "revision": nxt["revision"], "typed": "まど"})
        self.assertIn("<div id=typed>まど</div>", typed["html"])

        # Long-poll returns as soon as the state changes.
        seq = typed["seq"]
        mw.taskman.later(0.3, lambda: mw.gui_hooks.operation_did_execute(None, None))
        started = time.monotonic()
        status, polled = self.api(f"/api/state?since={seq}")
        self.assertLess(time.monotonic() - started, 5)
        self.assertGreater(polled["seq"], seq)

        # Card actions and undo.
        status, flagged = self.api("/api/card-action", {"cardId": 102, "side": "answer", "action": "flag", "flag": 2,
                                                        "revision": polled["revision"]})
        self.assertEqual(flagged["flag"], 2)
        status, undone = self.api("/api/undo", {"revision": flagged["revision"]})
        self.assertEqual((undone["status"], undone["cardId"]), ("question", 101))
        status, idle = self.api("/api/stop", {"revision": undone["revision"]})
        self.assertEqual(idle["status"], "idle")

    def test_debug_log_records_timings_but_nothing_private(self):
        if not self.cookie:
            self.login()
        self.assertFalse(self.api("/api/state")[1]["debugLog"], "off by default")
        with tempfile.TemporaryDirectory() as directory:
            original = self.reviewer.debug
            self.reviewer.debug = backend.DebugLog(Path(directory, "user_files", "debug.log"), True)
            try:
                status, headers, body = self.request("/api/state")
                self.assertTrue(json.loads(body)["debugLog"])
                self.assertIn("anki;dur=", headers["Server-Timing"])
                token = self.reviewer.frame_token()
                self.assertEqual(self.request(f"/c/{token}/word%20heya.wav", auth=False)[0], 200)
                self.request(f"/c/{token}/missing%20secret.png", auth=False)
                status, _ = self.api("/api/debug", {"info": {"ua": "Test Phone 1.0", "net": "4g"}, "entries": [
                    {"r": "/api/answer", "rtt": 2400, "hdr": 2390, "srv": 90, "net": 2300.4, "st": 200},
                    {"r": "media", "ext": "mp3", "ms": 1800, "kb": 512, "evil": "a b\nreq forged=1"}, "junk"]})
                self.assertEqual(status, 200)
                log = self.reviewer.debug.path.read_text()
            finally:
                self.reviewer.debug = original
        self.assertIn("route=/api/state", log)
        self.assertRegex(log, r"anki_queue_ms=\d+ anki_run_ms=\d+")
        self.assertIn("route=media ext=.wav status=200", log)
        self.assertIn("route=media ext=.png status=404", log)
        self.assertIn("client-info ua=Test_Phone_1.0 net=4g", log)
        self.assertIn("client r=/api/answer rtt=2400 hdr=2390 srv=90 net=2300.4 st=200", log)
        self.assertEqual(len([line for line in log.splitlines() if line.split(" ")[2] == "client"]), 2)
        self.assertNotIn("forged", log.replace("evil=abreqforged1", ""))
        for private in ("heya", "secret", "test-token", token, self.cookie.split("=", 1)[1]):
            self.assertNotIn(private, log)

    def test_wait_for_change_times_out_and_wakes_on_expired_action(self):
        seq = self.reviewer._seq
        self.assertFalse(self.reviewer.wait_for_change(seq, timeout=0.2))
        self.reviewer._pending_since = time.monotonic() - backend.PENDING_SECONDS + 0.2
        started = time.monotonic()
        self.assertTrue(self.reviewer.wait_for_change(seq, timeout=5))
        self.assertLess(time.monotonic() - started, 2)
        self.reviewer._pending_since = 0.0

    def test_frame_and_media(self):
        if not self.cookie:
            self.login()
        token = self.reviewer.frame_token()
        status, headers, body = self.request(f"/c/{token}/", auth=False)
        self.assertEqual(status, 200)
        self.assertIn("sandbox allow-scripts", headers["Content-Security-Policy"])
        self.assertNotIn("allow-same-origin", headers["Content-Security-Policy"])
        self.assertIn(b"/_anki/js/reviewer.js", body)
        status, headers, body = self.request(f"/c/{token}/_hover.js", auth=False)
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/javascript"))
        self.assertEqual(headers["Access-Control-Allow-Origin"], "*")
        self.assertEqual(self.request(f"/c/{token}/_hover.js", auth=False, headers={"If-None-Match": headers["ETag"]})[0], 304)
        status, headers, body = self.request(f"/c/{token}/word%20heya.wav", auth=False, headers={"Range": "bytes=0-3"})
        self.assertEqual((status, body), (206, b"RIFF"))
        # ';' is part of the file name, not URL path parameters (STEINS;GATE screenshots).
        for name in ("STEINS;GATE0%201.webp", "STEINS%3BGATE0%201.webp"):
            status, headers, body = self.request(f"/c/{token}/{name}", auth=False)
            self.assertEqual((status, headers["Content-Type"], body[8:12]), (200, "image/webp", b"WEBP"), name)
        self.assertEqual(self.request(f"/c/{token}/..%2Fsecret.txt", auth=False)[0], 404)
        self.assertEqual(self.request("/c/123.abc/_hover.js", auth=False)[0], 403)
        self.assertEqual(self.request("/_anki/js/../../secret", auth=False)[0], 404)

    def test_static_files_are_compressed_and_cached(self):
        status, headers, body = self.request("/app.js", headers={"Accept-Encoding": "gzip"}, auth=False)
        self.assertEqual((status, headers.get("Content-Encoding")), (200, "gzip"))
        self.assertEqual(self.request("/app.js", headers={"If-None-Match": headers["ETag"]}, auth=False)[0], 304)
        status, headers, body = self.request("/", auth=False)
        self.assertIn(f'content="{backend.VERSION}"'.encode(), body)
        self.assertEqual(headers["X-Frame-Options"], "DENY")


if __name__ == "__main__":
    unittest.main()
