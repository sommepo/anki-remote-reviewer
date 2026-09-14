"""A constrained, loopback-only mobile reviewer for the active Anki profile."""

from __future__ import annotations

from concurrent.futures import Future, TimeoutError
import hmac
import json
import mimetypes
import re
import secrets
import threading
import time
from functools import partial
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, unquote, urlparse

from aqt import mw, gui_hooks
from aqt.qt import QApplication, QMessageBox
from .media import audio_filenames, rewrite_media, rewrite_css, safe_media_file, byte_range, media_url, SOUND
from .ports import check_port


ADDON_NAME = __package__
ROOT = Path(__file__).parent
STATIC_ROOT = ROOT / "web"
MAX_BODY_BYTES = 16 * 1024
TRUSTED_DEVICE_SECONDS = 90 * 24 * 60 * 60
VERSION = "0.2.0-beta.1"


class ReviewError(Exception):
    """An expected error that is safe to return to the remote reviewer."""


class RemoteReviewer:
    def __init__(self) -> None:
        self._server: ThreadingHTTPServer | None = None
        self._server_thread: threading.Thread | None = None
        self._revision = secrets.token_hex(16)
        self._pending = False
        self._config = self._load_config()
        self._start_server()
        gui_hooks.reviewer_did_show_question.append(self._changed)
        gui_hooks.reviewer_did_show_answer.append(self._changed)
        gui_hooks.reviewer_will_end.append(self._changed)
        gui_hooks.profile_will_close.append(self._changed)

    def _changed(self, *args):
        self._revision = secrets.token_hex(16)
        self._pending = False

    def _load_config(self) -> dict[str, Any]:
        config = mw.addonManager.getConfig(ADDON_NAME) or {}
        changed = False
        if not config.get("accessToken"):
            config["accessToken"] = secrets.token_urlsafe(32)
            changed = True
        if not config.get("cookieSecret"):
            config["cookieSecret"] = secrets.token_urlsafe(48)
            changed = True
        if not isinstance(config.get("requireAccessToken"), bool):
            config["requireAccessToken"] = True
            changed = True
        if not isinstance(config.get("autoPlayAudioOnAnswer"), bool):
            config["autoPlayAudioOnAnswer"] = True
            changed = True
        if not isinstance(config.get("port"), int):
            config["port"] = 28594
            changed = True
        if not isinstance(config.get("requestTimeoutSeconds"), int):
            config["requestTimeoutSeconds"] = 12
            changed = True
        config.setdefault("rwkvNoteTypes", [])
        config.setdefault("audioFallbackFields", {})
        if not 1024 <= config['port'] <= 65535 or config['port'] in (8765, 8766):
            raise ValueError('Choose an unreserved reviewer port from 1024–65535; 8765/8766 are reserved.')
        config['requestTimeoutSeconds'] = max(2, min(30, config['requestTimeoutSeconds']))
        if changed:
            mw.addonManager.writeConfig(ADDON_NAME, config)
        return config

    @property
    def port(self) -> int:
        return self._config["port"]

    def _start_server(self) -> None:
        handler = partial(RemoteReviewHandler, reviewer=self)
        try:
            check_port(self.port)
            self._server = LoopbackHTTPServer(("127.0.0.1", self.port), handler)
        except OSError as error:
            QMessageBox.warning(
                mw,
                "Remote Reviewer",
                f"Could not start the loopback reviewer on port {self.port}.\n\n{error}",
            )
            return
        self._server_thread = threading.Thread(
            target=self._server.serve_forever,
            name="Anki Remote Reviewer",
            daemon=True,
        )
        self._server_thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def show_connection_details(self) -> None:
        token = self._config["accessToken"]
        QApplication.clipboard().setText(token)
        QMessageBox.information(
            mw,
            "Remote Reviewer",
            f"Remote Reviewer {VERSION}\nListening on this computer:\n"
            f"http://127.0.0.1:{self.port}\n\n"
            "The access token has been copied to your clipboard. Publish this "
            "loopback URL through Tailscale Serve; do not expose AnkiConnect.",
        )

    def invoke_on_main(self, callback: Callable[[], Any]) -> Any:
        """Run all Anki collection/scheduler access on Anki's GUI thread."""
        future = Future()

        def run() -> None:
            if not future.set_running_or_notify_cancel():
                return
            try:
                future.set_result(callback())
            except Exception as error:
                future.set_exception(error)

        mw.taskman.run_on_main(run)
        try:
            return future.result(timeout=self._config['requestTimeoutSeconds'])
        except TimeoutError:
            future.cancel()
            raise ReviewError('Anki is busy. Reconnect to check the current card before retrying.')

    def issue_session(self, supplied_token: str) -> str:
        if not self._config["requireAccessToken"]:
            return ""
        expected = self._config["accessToken"]
        if not hmac.compare_digest(supplied_token.encode('utf-8'), expected.encode('utf-8')):
            raise ReviewError("Invalid access token")
        expires_at = int(time.time()) + TRUSTED_DEVICE_SECONDS
        signature = hmac.new(
            self._config["cookieSecret"].encode("utf-8"),
            str(expires_at).encode("ascii"),
            "sha256",
        ).hexdigest()
        return f"{expires_at}.{signature}"

    def is_authenticated(self, handler: BaseHTTPRequestHandler) -> bool:
        if not self._config["requireAccessToken"]:
            return True
        header_token = handler.headers.get("X-Remote-Reviewer-Token", "")
        if header_token and hmac.compare_digest(header_token.encode('utf-8'), self._config["accessToken"].encode('utf-8')):
            return True
        cookie_header = handler.headers.get("Cookie", "")
        match = re.search(r"(?:^|;\s*)anki_remote_session=([^;]+)", cookie_header)
        if match is None:
            return False
        try:
            expires_at_text, supplied_signature = match.group(1).rsplit(".", 1)
            if not re.fullmatch(r'[0-9]{1,12}', expires_at_text) or not re.fullmatch(r'[0-9a-f]{64}', supplied_signature):
                return False
            expires_at = int(expires_at_text)
        except (TypeError, ValueError):
            return False
        expected_signature = hmac.new(
            self._config["cookieSecret"].encode("utf-8"),
            expires_at_text.encode("ascii"),
            "sha256",
        ).hexdigest()
        return expires_at >= int(time.time()) and hmac.compare_digest(supplied_signature, expected_signature)

    def deck_names(self) -> list[str]:
        def read() -> list[str]:
            if mw.col is None:
                raise ReviewError("No Anki profile is open")
            return [item.name for item in mw.col.decks.all_names_and_ids()]

        return self.invoke_on_main(read)

    def start_deck(self, deck_name: str, revision: str) -> dict[str, Any]:
        def start() -> dict[str, Any]:
            self._guard(revision)
            if not deck_name or mw.col is None:
                raise ReviewError("Choose a valid deck while an Anki profile is open")
            deck = mw.col.decks.by_name(deck_name)
            if deck is None:
                raise ReviewError("Deck was not found")
            mw.col.decks.select(deck["id"])
            mw.onOverview()
            self._pending = True
            mw.moveToState("review")
            return self._session_state_on_main()

        return self.invoke_on_main(start)

    def session_state(self) -> dict[str, Any]:
        return self.invoke_on_main(self._session_state_on_main)

    def _session_state_on_main(self) -> dict[str, Any]:
        common = {'version': VERSION, 'revision': self._revision}
        if mw.col is None:
            raise ReviewError("No Anki profile is open")
        if mw.state != "review" or mw.reviewer is None:
            self._pending = False
            return {**common, "status": "idle", "decks": [item.name for item in mw.col.decks.all_names_and_ids()]}
        reviewer = mw.reviewer
        card = reviewer.card
        if self._pending or card is None or reviewer.state not in ('question', 'answer'):
            return {**common, 'status': 'loading'}

        state = reviewer.state
        buttons = reviewer._answerButtonList() if state == "answer" else []
        show_intervals = bool(mw.col.conf.get('estTimes', True))
        labels = mw.col.sched.describe_next_states(reviewer._v3.states) if show_intervals and state == 'answer' else []
        audio_tags = {
            "q": self._audio_filenames(card, 'q'),
            "a": self._audio_filenames(card, 'a'),
        }
        return {
            **common,
            'canUndo': bool(reviewer._answeredIds),
            'cardClass': f'card{card.ord + 1}',
            "status": state,
            "cardId": card.id,
            "deck": self._deck_name(card.did),
            "questionHtml": rewrite_css(rewrite_media(self._card_question(card), audio_tags)),
            "answerHtml": rewrite_css(rewrite_media(self._card_answer(card), audio_tags)) if state == "answer" else "",
            "css": rewrite_css(card.note_type()["css"]),
            "rwkv": self._rwkv_value(reviewer, card) if state == "answer" else None,
            "showIntervals": show_intervals,
            "autoPlayAudioOnAnswer": self._config["autoPlayAudioOnAnswer"],
            "answerAudio": [media_url(filename) for filename in audio_tags['a'] if filename] if state == 'answer' else [],
            "buttons": [
                {
                    "ease": ease,
                    "label": label,
                    **({'interval': labels[ease - 1]} if show_intervals and ease <= len(labels) else {}),
                }
                for ease, label in buttons
            ],
        }

    def reveal(self, expected_card_id: int, revision: str) -> dict[str, Any]:
        def show_answer() -> dict[str, Any]:
            self._guard(revision)
            reviewer, card = self._active_card(expected_card_id, "question")
            self._pending = True
            reviewer._showAnswer()
            if reviewer.state == 'question':
                self._pending = False
                raise ReviewError('Anki is not ready to reveal yet. Check the desktop window.')
            return self._session_state_on_main()

        return self.invoke_on_main(show_answer)

    def answer(self, expected_card_id: int, ease: int, revision: str) -> dict[str, Any]:
        def answer_card() -> dict[str, Any]:
            self._guard(revision)
            reviewer, card = self._active_card(expected_card_id, "answer")
            if ease not in [item[0] for item in reviewer._answerButtonList()]:
                raise ReviewError("That answer button is not available for this card")
            self._revision = secrets.token_hex(16)
            self._pending = True
            reviewer._answerCard(ease)
            if reviewer.state == 'answer' and self._pending:
                self._pending = False
                raise ReviewError('Anki did not accept this answer. Check the desktop window.')
            return self._session_state_on_main()

        return self.invoke_on_main(answer_card)

    def undo(self, revision: str) -> dict[str, Any]:
        def undo_review() -> dict[str, Any]:
            self._guard(revision)
            if mw.state != 'review' or not mw.reviewer._answeredIds:
                raise ReviewError('No review is available to undo.')
            self._revision = secrets.token_hex(16)
            self._pending = True
            mw.undo()
            return self._session_state_on_main()

        return self.invoke_on_main(undo_review)

    def stop_review(self, revision: str) -> dict[str, Any]:
        def stop() -> dict[str, Any]:
            self._guard(revision)
            if mw.state == "review":
                mw.moveToState("overview")
            return self._session_state_on_main()

        return self.invoke_on_main(stop)

    def _guard(self, revision):
        if self._pending or not revision or revision != self._revision:
            raise ReviewError('The review changed. Refresh the current card before trying again.')

    def _active_card(self, expected_card_id: int | None, expected_state: str) -> tuple[Any, Any]:
        if mw.state != "review" or mw.reviewer is None or mw.reviewer.card is None:
            raise ReviewError("There is no active review card")
        reviewer = mw.reviewer
        card = reviewer.card
        if reviewer.state != expected_state:
            raise ReviewError("The card changed; refresh before trying again")
        if expected_card_id is not None and card.id != expected_card_id:
            raise ReviewError("The card changed; refresh before trying again")
        return reviewer, card

    @staticmethod
    def _card_question(card: Any) -> str:
        return card.question() if getattr(card, "question", None) is not None else card._getQA()["q"]

    @staticmethod
    def _card_answer(card: Any) -> str:
        return card.answer() if getattr(card, "answer", None) is not None else card._getQA()["a"]

    @staticmethod
    def _deck_name(deck_id: int) -> str:
        deck = mw.col.decks.get(deck_id)
        return deck["name"] if deck is not None else "Unknown deck"

    def _rwkv_value(self, reviewer: Any, card: Any) -> str:
        if card.note_type()["name"] not in self._config['rwkvNoteTypes']:
            return ""
        try:
            from aqt import rwkv_scheduler

            rows = rwkv_scheduler.rwkv_card_info_rows(
                reviewer=reviewer,
                card=card,
                fallback_source="Remote reviewer",
                include_after_review=False,
            )
            return next((value for label, value in rows if label == "RWKV computed R"), "Unavailable")
        except Exception:
            return "Unavailable"

    def _audio_filenames(self, card, side):
        files = audio_filenames(card, side)
        if files:
            return files
        fields = self._config['audioFallbackFields'].get(card.note_type()['name'], {}).get(side, [])
        return [m[1] for name in fields for m in SOUND.finditer(card.note()[name])]


    def media_file(self, encoded_name: str) -> Path:
        def resolve() -> Path:
            if mw.col is None:
                raise ReviewError("No Anki profile is open")
            try:
                return safe_media_file(mw.col.media.dir(), encoded_name)
            except ValueError as error:
                raise ReviewError(str(error)) from error

        return self.invoke_on_main(resolve)


class LoopbackHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False


class RemoteReviewHandler(BaseHTTPRequestHandler):
    server_version = "AnkiRemoteReviewer/1.0"

    def __init__(self, *args: Any, reviewer: RemoteReviewer, **kwargs: Any) -> None:
        self.reviewer = reviewer
        super().__init__(*args, **kwargs)

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, _format: str, *_args: Any) -> None:
        # Deliberately avoid recording request paths, cookies, or tokens.
        return

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path.startswith("/media/"):
            self._require_auth_then(self._serve_media, path.removeprefix("/media/"))
        elif path == "/api/state":
            self._require_auth_then(lambda: self._json(self.reviewer.session_state()))
        elif path == "/api/decks":
            self._require_auth_then(lambda: self._json({"decks": self.reviewer.deck_names()}))
        elif path in {"/", "/index.html"}:
            self._serve_static("index.html", "text/html; charset=utf-8")
        elif path == "/app.css":
            self._serve_static("app.css", "text/css; charset=utf-8")
        elif path == "/app.js":
            self._serve_static("app.js", "application/javascript; charset=utf-8")
        else:
            self._error(HTTPStatus.NOT_FOUND, "Not found")

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            origin = self.headers.get('Origin')
            if origin and urlparse(origin).netloc != self.headers.get('Host'):
                raise ReviewError('Cross-origin requests are not allowed')
            if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                raise ReviewError('JSON content type is required')
            body = self._read_json_body()
            if path == "/api/login":
                session = self.reviewer.issue_session(str(body.get("token", "")))
                self._json(
                    {"ok": True},
                    extra_headers={
                        "Set-Cookie": (
                            f"anki_remote_session={session}; Max-Age={TRUSTED_DEVICE_SECONDS}; "
                            "Secure; HttpOnly; SameSite=Strict; Path=/"
                        )
                    },
                )
                return
            if not self.reviewer.is_authenticated(self):
                raise ReviewError("Authentication required")
            if path == "/api/start":
                self._json(self.reviewer.start_deck(str(body.get("deck", "")), body.get('revision')))
            elif path == "/api/reveal":
                self._json(self.reviewer.reveal(self._optional_card_id(body), body.get('revision')))
            elif path == "/api/answer":
                self._json(self.reviewer.answer(self._optional_card_id(body), int(body.get("ease", 0)), body.get('revision')))
            elif path == "/api/undo":
                self._json(self.reviewer.undo(body.get('revision')))
            elif path == "/api/stop":
                self._json(self.reviewer.stop_review(body.get('revision')))
            else:
                self._error(HTTPStatus.NOT_FOUND, "Not found")
        except ReviewError as error:
            self._error(HTTPStatus.FORBIDDEN, str(error))
        except (TypeError, ValueError):
            self._error(HTTPStatus.BAD_REQUEST, "Invalid request body")
        except Exception:
            self._error(HTTPStatus.SERVICE_UNAVAILABLE, 'Anki could not complete this request. Check the desktop window and reconnect.')

    def _require_auth_then(self, callback: Callable[..., None], *args: Any) -> None:
        try:
            if not self.reviewer.is_authenticated(self):
                raise ReviewError("Authentication required")
            callback(*args)
        except ReviewError as error:
            self._error(HTTPStatus.FORBIDDEN, str(error))

        except Exception:
            self._error(HTTPStatus.SERVICE_UNAVAILABLE, 'Anki is unavailable. Check the desktop window and reconnect.')

    def _read_json_body(self) -> dict[str, Any]:
        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length < 1 or content_length > MAX_BODY_BYTES:
            raise ReviewError("Invalid request body")
        try:
            body = json.loads(self.rfile.read(content_length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ReviewError("Invalid JSON") from error
        if not isinstance(body, dict):
            raise ReviewError("Invalid JSON")
        return body

    @staticmethod
    def _optional_card_id(body: dict[str, Any]) -> int | None:
        card_id = body.get("cardId")
        if type(card_id) is not int:
            raise ReviewError('A current card ID is required')
        return card_id

    def _serve_media(self, encoded_name: str) -> None:
        media = self.reviewer.media_file(encoded_name)
        content_type = {'.opus': 'audio/ogg', '.mp3': 'audio/mpeg', '.m4a': 'audio/mp4'}.get(media.suffix.lower()) or mimetypes.guess_type(media.name)[0] or 'application/octet-stream'
        size = media.stat().st_size
        try:
            start, end = byte_range(self.headers.get('Range'), size)
        except ValueError:
            self.send_response(416)
            self.send_header('Content-Range', f'bytes */{size}')
            self.end_headers()
            return
        self.send_response(206 if self.headers.get('Range') else 200)
        self.send_header("Content-Type", content_type)
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Content-Length', str(max(0, end - start + 1)))
        if self.headers.get('Range'):
            self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
        self.send_header("Cache-Control", "private, no-store")
        self.send_header('Content-Security-Policy', "sandbox; default-src 'none'")
        self._security_headers()
        self.end_headers()
        with media.open("rb") as handle:
            handle.seek(start)
            remaining = end - start + 1
            while remaining > 0 and (chunk := handle.read(min(64 * 1024, remaining))):
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def _serve_static(self, filename: str, content_type: str) -> None:
        file_path = STATIC_ROOT / filename
        if not file_path.is_file():
            self._error(HTTPStatus.NOT_FOUND, "Not found")
            return
        contents = file_path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(contents)))
        self.send_header("Cache-Control", "no-store")
        self._security_headers()
        self.end_headers()
        self.wfile.write(contents)

    def _json(self, value: Any, extra_headers: dict[str, str] | None = None) -> None:
        contents = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(contents)))
        self.send_header("Cache-Control", "no-store")
        if extra_headers:
            for name, header_value in extra_headers.items():
                self.send_header(name, header_value)
        self._security_headers()
        self.end_headers()
        self.wfile.write(contents)

    def _error(self, status: HTTPStatus, message: str) -> None:
        contents = json.dumps({"error": message}).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(contents)))
        self._security_headers()
        self.end_headers()
        self.wfile.write(contents)

    def _security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header('Cache-Control', 'no-store')
        if not self.path.startswith('/media/'):
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; media-src 'self' blob:; font-src 'self' data:; object-src 'none'; frame-src 'none'; base-uri 'none'; frame-ancestors 'none'")
