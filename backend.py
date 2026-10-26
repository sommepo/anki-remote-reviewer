"""Remote reviewer for the open Anki profile.

A small web app is served over loopback (and optionally one trusted LAN
address). Cards are the exact HTML desktop Anki renders, shown in a sandboxed
frame that runs Anki's own reviewer script, so templates, scripts and add-on
card content behave as they do on the desktop.
"""

from __future__ import annotations

import gzip
import hashlib
import hmac
import ipaddress
import json
import os
import platform
import re
import secrets
import sys
import threading
import time
import traceback
from concurrent.futures import Future, TimeoutError
from functools import partial
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, quote, urlsplit

from aqt import gui_hooks, mw
from aqt.qt import QApplication, QMessageBox

from .media import SOUND, av_entries, byte_range, content_type, safe_asset, safe_media_file
from .ports import check_port

ADDON_NAME = __package__
ROOT = Path(__file__).parent
STATIC_ROOT = ROOT / "web"
VERSION = "0.3.1"
MAX_BODY_BYTES = 64 * 1024
TRUSTED_DEVICE_SECONDS = 90 * 24 * 60 * 60
FRAME_TOKEN_DAYS = 7
LONG_POLL_SECONDS = 25
PENDING_SECONDS = 10
DEFAULT_PORT = 28594
RESERVED_PORTS = (8765, 8766)
PRIVATE_NETWORKS = tuple(ipaddress.ip_network(n) for n in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))
REVIEW_KINDS = {"question": "reviewQuestion", "answer": "reviewAnswer"}
APP_FILES = {"/": "index.html", "/index.html": "index.html", "/app.css": "app.css",
             "/app.js": "app.js", "/frame.js": "frame.js"}
ANKI_ASSET_TYPES = {'.js', '.css', '.woff', '.woff2', '.ttf', '.otf', '.svg', '.png', '.json'}
ADDON_ASSET_TYPES = {'.js', '.css', '.woff', '.woff2', '.ttf', '.otf', '.svg', '.png', '.jpg', '.jpeg', '.gif',
                     '.webp', '.avif', '.mp3', '.ogg', '.opus', '.m4a', '.wav', '.mp4', '.webm'}
ADDON_URL = re.compile(r"(?<=[\s\"'(=])/_addons/")
ASSET_CSP = "sandbox; default-src 'none'"
DEBUG_LOG = ROOT / "user_files" / "debug.log"
DEBUG_LOG_BYTES = 1_000_000


class ReviewError(Exception):
    """An expected error that is safe to return to the remote reviewer."""


class NotReady(Exception):
    """Desktop Anki is mid-transition; the action can be retried shortly."""


class DebugLog:
    """Opt-in timing log for diagnosing slow connections.

    Records only route names, status codes, sizes, file extensions and timings.
    Never request paths, file names, card content, cookies or tokens.
    """

    def __init__(self, path: Path, enabled: bool) -> None:
        self.path = path
        self.enabled = enabled
        self._lock = threading.Lock()

    def write(self, kind: str, **fields: Any) -> None:
        if not self.enabled:
            return
        parts = [time.strftime("%Y-%m-%d %H:%M:%S"), kind]
        parts += [f"{key}={value}" for key, value in fields.items() if value is not None and value != ""]
        try:
            with self._lock:
                self.path.parent.mkdir(exist_ok=True)
                if self.path.is_file() and self.path.stat().st_size > DEBUG_LOG_BYTES:
                    os.replace(self.path, self.path.with_name(self.path.name + ".1"))
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(" ".join(parts) + "\n")
        except OSError:
            pass


def ms(seconds: float) -> int:
    return round(seconds * 1000)


def short_value(value: Any, limit: int = 40) -> str:
    """A log-safe token: letters, digits and a few separators only."""
    return re.sub(r"[^A-Za-z0-9_.:/+-]", "", str(value))[:limit]


def exception_summary() -> str:
    """Exception type and the last few code locations; no message (it may hold names)."""
    kind, _, tb = sys.exc_info()
    frames = traceback.extract_tb(tb)[-3:] if tb else []
    where = ",".join(f"{Path(f.filename).name}:{f.lineno}:{f.name}" for f in frames)
    return f"{kind.__name__ if kind else 'Unknown'}@{where}"


def route_name(path: str) -> tuple[str, str]:
    """(route, file extension) for the log, never the full path."""
    if path in APP_FILES:
        return "app:" + APP_FILES[path], ""
    if path.startswith("/c/"):
        name = path[3:].partition("/")[2]
        if not name:
            return "frame", ""
        route = "addon-asset" if name.startswith("_addons/") else "media"
        return route, short_value(Path(name).suffix.lower(), 6)
    if path.startswith("/_anki/"):
        return "anki-asset", short_value(Path(path).suffix.lower(), 6)
    if path.startswith("/api/") and re.fullmatch(r"/api/[a-z-]{1,20}", path):
        return path, ""
    return "other", ""


def validate_lan(address: str, subnet: str) -> tuple[str, str]:
    ip = ipaddress.IPv4Address(address)
    network = ipaddress.IPv4Network(subnet, strict=True)
    if ip not in network or not any(network.subnet_of(n) for n in PRIVATE_NETWORKS):
        raise ValueError('LAN access needs a private IPv4 address inside a private subnet.')
    return str(ip), str(network)


def load_config(raw: dict[str, Any]) -> tuple[dict[str, Any], bool, list[str]]:
    """Return (config, changed, problems). Problems disable the affected feature."""
    config = dict(raw or {})
    problems: list[str] = []
    before = json.dumps(config, sort_keys=True)
    if not isinstance(config.get("accessToken"), str) or not config.get("accessToken"):
        config["accessToken"] = secrets.token_urlsafe(32)
    if not isinstance(config.get("cookieSecret"), str) or not config.get("cookieSecret"):
        config["cookieSecret"] = secrets.token_urlsafe(48)
    if not isinstance(config.get("requireAccessToken"), bool):
        config["requireAccessToken"] = True
    if "autoPlayAudioOnAnswer" in config:
        # 0.2 only autoplayed answers; 0.3 follows the deck's autoplay option.
        legacy = config.pop("autoPlayAudioOnAnswer")
        config.setdefault("autoplayAudio", legacy is not False)
    if not isinstance(config.get("autoplayAudio"), bool):
        config["autoplayAudio"] = True
    if not isinstance(config.get("port"), int) or isinstance(config.get("port"), bool):
        config["port"] = DEFAULT_PORT
    if not isinstance(config.get("requestTimeoutSeconds"), int) or isinstance(config.get("requestTimeoutSeconds"), bool):
        config["requestTimeoutSeconds"] = 12
    if not isinstance(config.get("rwkvNoteTypes"), list):
        config["rwkvNoteTypes"] = []
    if not isinstance(config.get("audioFallbackFields"), dict):
        config["audioFallbackFields"] = {}
    for key in ("lanAddress", "lanSubnet"):
        if not isinstance(config.get(key), str):
            config[key] = ""
    if not isinstance(config.get("debugLog"), bool):
        config["debugLog"] = False
    changed = json.dumps(config, sort_keys=True) != before
    config["requestTimeoutSeconds"] = max(2, min(30, config["requestTimeoutSeconds"]))
    if not 1024 <= config["port"] <= 65535 or config["port"] in RESERVED_PORTS:
        problems.append(f'Port {config["port"]} is not allowed. Choose 1024–65535, excluding 8765/8766.')
    config["lanEnabled"] = False
    if config["lanAddress"]:
        try:
            validate_lan(config["lanAddress"], config["lanSubnet"])
            config["lanEnabled"] = True
            if not config["requireAccessToken"]:
                config["requireAccessToken"] = True
                changed = True
        except ValueError as error:
            problems.append(f'LAN access is disabled: {error}')
    return config, changed, problems


class RemoteReviewer:
    def __init__(self) -> None:
        self._server: ThreadingHTTPServer | None = None
        self._lan_server: ThreadingHTTPServer | None = None
        self._changes = threading.Condition()
        self._seq = 1
        self._instance = secrets.token_hex(4)
        self._revision = secrets.token_hex(8)
        self._view_key: tuple[int, str] | None = None
        self._pending_since = 0.0
        self._captured: dict[str, tuple[int, str]] = {}
        self._media_dir: str | None = None
        self._syncing = False
        self._sync_started = 0.0
        self._last_sync: int | None = None
        self.assets = AssetCache()
        self._timing = threading.local()
        raw = mw.addonManager.getConfig(ADDON_NAME) or {}
        self._config, changed, problems = load_config(raw)
        self.debug = DebugLog(DEBUG_LOG, self._config["debugLog"])
        self._log_startup()
        if changed:
            mw.addonManager.writeConfig(ADDON_NAME, {k: v for k, v in self._config.items() if k != "lanEnabled"})
        if problems:
            QMessageBox.warning(mw, "Remote Reviewer", "\n\n".join(problems))
        if not any(p.startswith("Port") for p in problems):
            self._start_server()
        gui_hooks.reviewer_did_show_question.append(self._card_shown_question)
        gui_hooks.reviewer_did_show_answer.append(self._card_shown_answer)
        gui_hooks.reviewer_will_end.append(self._view_ended)
        gui_hooks.profile_will_close.append(self._profile_closing)
        gui_hooks.profile_did_open.append(self._profile_opened)
        gui_hooks.state_did_change.append(self._notify)
        gui_hooks.operation_did_execute.append(self._notify)
        gui_hooks.main_window_did_init.append(self._capture_last)
        for name, callback in (("sync_will_start", self._sync_began), ("sync_did_finish", self._sync_finished)):
            if hasattr(gui_hooks, name):
                getattr(gui_hooks, name).append(callback)
        self._capture_last()

    # ----- debug timing -----

    def _log_startup(self) -> None:
        if not self.debug.enabled:
            return
        try:
            from aqt import appVersion as anki_version
        except ImportError:
            anki_version = "unknown"
        self.debug.write("start", version=VERSION, anki=short_value(anki_version), python=platform.python_version(),
                         os=short_value(platform.platform(terse=True)), timeout=self._config["requestTimeoutSeconds"],
                         lan=self._config["lanEnabled"])

    def reset_timing(self) -> None:
        self._timing.__dict__.update(queue=0.0, run=0.0, wait=0.0, calls=0)

    def timing(self) -> dict[str, float]:
        if not hasattr(self._timing, "calls"):
            self.reset_timing()
        return self._timing.__dict__

    # ----- change tracking (hooks run on the GUI thread) -----

    def _notify(self, *args: Any) -> None:
        with self._changes:
            self._seq += 1
            self._changes.notify_all()

    def _shown(self, card: Any, side: str) -> None:
        key = (card.id, side)
        with self._changes:
            if key != self._view_key:
                self._view_key = key
                self._revision = secrets.token_hex(8)
            self._pending_since = 0.0
        self._notify()

    def _card_shown_question(self, card: Any) -> None:
        self._shown(card, "question")

    def _card_shown_answer(self, card: Any) -> None:
        self._shown(card, "answer")

    def _view_ended(self, *args: Any) -> None:
        with self._changes:
            self._view_key = None
            self._revision = secrets.token_hex(8)
            self._pending_since = 0.0
        self._notify()

    def _profile_opened(self) -> None:
        self._capture_last()
        self._media_dir = mw.col.media.dir() if mw.col else None
        self._notify()

    def _profile_closing(self) -> None:
        self._media_dir = None
        self._captured.clear()
        self._view_ended()

    def _capture_last(self) -> None:
        # Run after every other card_will_show filter so the captured text is
        # exactly what the desktop reviewer displays.
        gui_hooks.card_will_show.remove(self._capture_card_text)
        gui_hooks.card_will_show.append(self._capture_card_text)

    def _capture_card_text(self, text: str, card: Any, kind: str) -> str:
        if kind in REVIEW_KINDS.values():
            self._captured[kind] = (card.id, text)
        return text

    def _sync_began(self, *args: Any) -> None:
        self._syncing, self._sync_started = True, time.time()
        self.debug.write("sync", event="start")
        self._notify()

    def _sync_finished(self, *args: Any) -> None:
        if self._syncing:
            self.debug.write("sync", event="finish", seconds=round(time.time() - self._sync_started, 1))
        self._syncing, self._last_sync = False, int(time.time())
        self._notify()

    def _is_syncing(self) -> bool:
        if self._syncing and time.time() - self._sync_started > 180:
            self._syncing = False  # sync_did_finish never fired
        return self._syncing

    def _is_pending(self) -> bool:
        return bool(self._pending_since) and time.monotonic() - self._pending_since < PENDING_SECONDS

    def _set_pending(self) -> None:
        with self._changes:
            self._pending_since = time.monotonic()
        self._notify()

    def wait_for_change(self, since: int, timeout: float = LONG_POLL_SECONDS) -> bool:
        """Block an HTTP thread until state changes; True if it did."""
        started = time.monotonic()
        try:
            return self._wait_for_change(since, started + timeout)
        finally:
            self.timing()["wait"] += time.monotonic() - started

    def _wait_for_change(self, since: int, deadline: float) -> bool:
        with self._changes:
            while self._seq == since:
                remaining = deadline - time.monotonic()
                if self._pending_since:
                    # Wake when a stalled action expires so clients leave "loading".
                    remaining = min(remaining, self._pending_since + PENDING_SECONDS - time.monotonic() + 0.05)
                if remaining <= 0:
                    return self._seq != since or (bool(self._pending_since) and not self._is_pending())
                self._changes.wait(remaining)
            return True

    # ----- server lifecycle -----

    @property
    def port(self) -> int:
        return self._config["port"]

    def _start_server(self) -> None:
        handler = partial(RemoteReviewHandler, reviewer=self)
        try:
            check_port(self.port)
            self._server = ReviewerHTTPServer(("127.0.0.1", self.port), handler)
        except OSError as error:
            QMessageBox.warning(mw, "Remote Reviewer", f"Could not start the reviewer on port {self.port}.\n\n{error}")
            return
        threading.Thread(target=self._server.serve_forever, name="Anki Remote Reviewer", daemon=True).start()
        if self._config["lanEnabled"]:
            try:
                self._lan_server = ReviewerHTTPServer((self._config["lanAddress"], self.port), handler)
                self._lan_server.allowed_network = ipaddress.ip_network(self._config["lanSubnet"])
                threading.Thread(target=self._lan_server.serve_forever, name="Anki Remote Reviewer LAN", daemon=True).start()
            except OSError as error:
                self._lan_server = None
                QMessageBox.warning(mw, "Remote Reviewer LAN", f"LAN access could not start; loopback remains available.\n\n{error}")

    def stop(self) -> None:
        with self._changes:
            self._changes.notify_all()
        for server in (self._lan_server, self._server):
            if server is not None:
                server.shutdown()
                server.server_close()
        self._lan_server = self._server = None

    def show_connection_details(self) -> None:
        QApplication.clipboard().setText(self._config["accessToken"])
        lan = (f"\nTrusted LAN: http://{self._config['lanAddress']}:{self.port}" if self._lan_server else "")
        status = "Listening" if self._server else "Not running (see the earlier warning)"
        QMessageBox.information(
            mw,
            "Remote Reviewer",
            f"Remote Reviewer {VERSION}\n{status} on this computer:\n"
            f"http://127.0.0.1:{self.port}{lan}\n\n"
            "The access token has been copied to your clipboard. Publish the "
            "loopback URL with Tailscale Serve; never expose it to the internet.",
        )

    def show_debug_log(self) -> None:
        if not self.debug.enabled:
            QMessageBox.information(
                mw, "Remote Reviewer",
                "The debug log is off.\n\nTo turn it on: Tools > Add-ons > Anki Remote Reviewer > Config, "
                'set "debugLog" to true, then restart Anki. Use the reviewer on your phone until it is slow, '
                "then come back here.")
            return
        from aqt.utils import openFolder
        self.debug.path.parent.mkdir(exist_ok=True)
        openFolder(str(self.debug.path.parent))

    def invoke_on_main(self, callback: Callable[[], Any]) -> Any:
        """Run all collection and reviewer access on Anki's GUI thread."""
        future: Future = Future()
        marks = {"queued": time.monotonic()}

        def run() -> None:
            if not future.set_running_or_notify_cancel():
                return
            marks["started"] = time.monotonic()
            try:
                future.set_result(callback())
            except BaseException as error:
                future.set_exception(error)
            finally:
                marks["finished"] = time.monotonic()

        mw.taskman.run_on_main(run)
        timing = self.timing()
        try:
            return future.result(timeout=self._config["requestTimeoutSeconds"])
        except TimeoutError:
            future.cancel()
            self.debug.write("anki-busy", waited_ms=ms(time.monotonic() - marks["queued"]),
                             started="started" in marks)
            raise ReviewError("Anki is busy. Check the desktop window, then try again.")
        finally:
            # queue: waiting for Anki's main thread; run: time spent inside Anki.
            now = time.monotonic()
            started = marks.get("started", now)
            timing["queue"] += started - marks["queued"]
            timing["run"] += marks.get("finished", now) - started
            timing["calls"] += 1

    # ----- authentication -----

    def _sign(self, purpose: str, expires_at: int) -> str:
        message = f"{purpose}:{expires_at}" if purpose else str(expires_at)
        return hmac.new(self._config["cookieSecret"].encode(), message.encode(), "sha256").hexdigest()

    def _verify(self, purpose: str, value: str) -> bool:
        match = re.fullmatch(r"([0-9]{1,12})\.([0-9a-f]{64})", value or "")
        if match is None:
            return False
        expected = self._sign(purpose, int(match[1]))
        return int(match[1]) >= int(time.time()) and hmac.compare_digest(match[2], expected)

    def issue_session(self, supplied_token: str) -> str:
        if not self._config["requireAccessToken"]:
            return ""
        if not hmac.compare_digest(supplied_token.encode(), self._config["accessToken"].encode()):
            raise ReviewError("Invalid access token")
        expires_at = int(time.time()) + TRUSTED_DEVICE_SECONDS
        return f"{expires_at}.{self._sign('', expires_at)}"

    def is_authenticated(self, handler: BaseHTTPRequestHandler) -> bool:
        if not self._config["requireAccessToken"]:
            return True
        header_token = handler.headers.get("X-Remote-Reviewer-Token", "")
        if header_token and hmac.compare_digest(header_token.encode(), self._config["accessToken"].encode()):
            return True
        match = re.search(r"(?:^|;\s*)anki_remote_session=([^;]+)", handler.headers.get("Cookie", ""))
        return match is not None and self._verify("", match[1])

    def frame_token(self) -> str:
        # Day-aligned so the card frame (and its globals) survives for a day.
        day = 86400
        expires_at = (int(time.time()) // day + FRAME_TOKEN_DAYS + 1) * day
        return f"{expires_at}.{self._sign('frame', expires_at)}"

    def valid_frame_token(self, token: str) -> bool:
        return self._verify("frame", token)

    # ----- state -----

    def session_state(self) -> dict[str, Any]:
        return self.invoke_on_main(self._session_state_on_main)

    def _session_state_on_main(self) -> dict[str, Any]:
        if mw.col is None:
            raise ReviewError("No Anki profile is open")
        self._media_dir = mw.col.media.dir()
        with self._changes:
            expired = bool(self._pending_since) and not self._is_pending()
            if expired:
                self._pending_since = 0.0
        if expired:
            self._notify()  # wake other clients still showing "loading"
        with self._changes:
            seq, revision, pending = self._seq, self._revision, self._is_pending()
        undo_label = self._undo_label()
        common = {
            "version": VERSION, "instance": self._instance, "seq": seq, "revision": revision,
            "frameToken": self.frame_token(), "syncing": self._is_syncing(), "lastSync": self._last_sync,
            "canUndo": bool(undo_label), "undoLabel": undo_label,
            "timeoutSeconds": self._config["requestTimeoutSeconds"],
            "debugLog": self.debug.enabled,
        }
        reviewer = mw.reviewer
        if mw.state != "review" or reviewer is None:
            return {**common, "status": "idle", "decks": self._deck_list(), "currentDeckId": mw.col.decks.current()["id"]}
        card = reviewer.card
        if pending or card is None or reviewer.state not in REVIEW_KINDS:
            return {**common, "status": "loading"}
        side = reviewer.state
        return {
            **common,
            "status": side,
            "cardId": card.id,
            "ord": card.ord,
            "deck": mw.col.decks.name(card.did),
            "html": self._card_html(reviewer, card, side),
            "audio": {"q": self._audio(card, "q"), "a": self._audio(card, "a")},
            "autoplay": self._config["autoplayAudio"] and self._safe(card.autoplay, True),
            "replayQuestion": self._safe(card.replay_question_audio_on_answer_side, True),
            "flag": self._safe(card.user_flag, 0),
            "marked": self._safe(lambda: card.note(reload=True).has_tag("marked"), False),
            "counts": self._counts(reviewer),
            "rwkv": self._rwkv_value(reviewer, card) if side == "answer" else "",
            "buttons": self._buttons(reviewer) if side == "answer" else [],
        }

    @staticmethod
    def _safe(callback: Callable[[], Any], default: Any) -> Any:
        try:
            return callback()
        except Exception:
            return default

    def _undo_label(self) -> str:
        return self._safe(lambda: mw.col.undo_status().undo or "", "")

    def _deck_list(self) -> list[dict[str, Any]]:
        decks: list[dict[str, Any]] = []

        def walk(node: Any, path: list[str]) -> None:
            for child in node.children:
                decks.append({
                    "id": child.deck_id, "name": child.name, "depth": len(path),
                    "new": child.new_count, "learn": child.learn_count, "review": child.review_count,
                    "collapsed": child.collapsed, "hasChildren": bool(child.children),
                    "filtered": child.filtered,
                })
                walk(child, path + [child.name])

        walk(mw.col.sched.deck_due_tree(), [])
        return decks

    def _card_html(self, reviewer: Any, card: Any, side: str) -> str:
        kind = REVIEW_KINDS[side]
        captured = self._captured.get(kind)
        if captured and captured[0] == card.id:
            text = captured[1]
        else:
            text = card.question() if side == "question" else card.answer()
            munge = getattr(reviewer, "_mungeQA", None)
            text = munge(text) if callable(munge) else mw.prepare_card_text_for_display(text)
            text = gui_hooks.card_will_show(text, card, kind)
        # Add-on files (e.g. card backgrounds) are only served via the signed frame path.
        return ADDON_URL.sub(f"/c/{self.frame_token()}/_addons/", text)

    def _audio(self, card: Any, side: str) -> list[Any]:
        try:
            tags = card.question_av_tags() if side == "q" else card.answer_av_tags()
            entries = av_entries(tags)
        except (AttributeError, TypeError):
            entries = []
        if entries:
            return entries
        # Explicit opt-in for forks without AV tags. Never guess template order
        # from note order: it can play the wrong pronunciation.
        fields = self._config["audioFallbackFields"].get(card.note_type()["name"], {}).get(side, [])
        note = card.note()
        return [{"file": m[1]} for name in fields if name in note for m in SOUND.finditer(note[name])]

    def _counts(self, reviewer: Any) -> dict[str, Any] | None:
        # _remaining() includes fork overrides and respects the dueCounts option.
        html = self._safe(reviewer._remaining, "")
        found = re.findall(r"class=(new|learn|review)-count>\s*(<u>)?\s*(\d+)", html or "")
        if len(found) != 3:
            return None
        return {"new": int(found[0][2]), "learn": int(found[1][2]), "review": int(found[2][2]),
                "current": next((kind for kind, underline, _ in found if underline), None)}

    def _buttons(self, reviewer: Any) -> list[dict[str, Any]]:
        show_intervals = bool(mw.col.conf.get("estTimes", True))
        labels = self._safe(lambda: mw.col.sched.describe_next_states(reviewer._v3.states), []) if show_intervals else []
        return [
            {"ease": ease, "label": label, **({"interval": labels[ease - 1]} if ease <= len(labels) else {})}
            for ease, label in reviewer._answerButtonList()
        ]

    def _rwkv_value(self, reviewer: Any, card: Any) -> str:
        if card.note_type()["name"] not in self._config["rwkvNoteTypes"]:
            return ""
        try:
            from aqt import rwkv_scheduler

            rows = rwkv_scheduler.rwkv_card_info_rows(
                reviewer=reviewer, card=card, fallback_source="Remote reviewer", include_after_review=False,
            )
            return next((value for label, value in rows if label == "RWKV computed R"), "Unavailable")
        except Exception:
            return "Unavailable"

    # ----- actions -----

    def _guard(self, revision: Any) -> None:
        if self._is_pending():
            raise ReviewError("Anki is still finishing the previous action.")
        if not revision or revision != self._revision:
            raise ReviewError("The review changed. Showing the current card.")

    def _reviewer_card(self, card_id: int, side: str) -> Any:
        """Return the reviewer showing card_id on side, NotReady while it is transitioning."""
        reviewer = mw.reviewer
        if mw.state != "review" or reviewer is None:
            raise ReviewError("There is no active review")
        if reviewer.card is None or reviewer.state not in REVIEW_KINDS:
            raise NotReady()
        if reviewer.card.id != card_id or reviewer.state != side:
            raise ReviewError("The card changed. Showing the current card.")
        blocked = getattr(reviewer, "_review_actions_are_blocked", None)
        if callable(blocked) and blocked():
            raise NotReady()
        if side == "question":
            rendered = getattr(reviewer, "_current_question_is_rendered", None)
            if callable(rendered) and not rendered():
                raise NotReady()
        else:
            answer_blocked = getattr(reviewer, "_answer_actions_are_blocked", None)
            if (callable(answer_blocked) and answer_blocked()) or not getattr(reviewer, "_answer_rendered", True):
                raise NotReady()
        return reviewer

    def _perform(self, revision: Any, action: Callable[[], bool | None], settle: bool = True) -> dict[str, Any]:
        """Run action on the GUI thread once desktop Anki is ready, then return the settled state.

        The fork's reviewer silently ignores input while it loads its web view or
        redraws the first card of a session; waiting here makes the first action
        behave like every later one instead of failing.
        """
        deadline = time.monotonic() + self._config["requestTimeoutSeconds"]

        def attempt() -> bool:
            self._guard(revision)
            try:
                done = action()
            except BaseException:
                with self._changes:
                    self._pending_since = 0.0
                raise
            if done is False:
                with self._changes:
                    self._pending_since = 0.0
                raise NotReady()
            return True

        while True:
            try:
                self.invoke_on_main(attempt)
                break
            except NotReady:
                if time.monotonic() > deadline:
                    raise ReviewError("Desktop Anki is still getting the card ready. Try again in a moment.")
                time.sleep(0.08)
        if settle:
            with self._changes:
                self._changes.wait_for(lambda: not self._is_pending(), max(0.5, deadline - time.monotonic()))
        return self.session_state()

    def record_client_timings(self, body: dict[str, Any]) -> dict[str, Any]:
        """Timings measured on the phone, so network time can be told apart from Anki time."""
        if not self.debug.enabled:
            return {"ok": True}
        info = body.get("info")
        if isinstance(info, dict):
            self.debug.write("client-info", **{short_value(k, 20): short_value(str(v).replace(" ", "_"), 160)
                                               for k, v in list(info.items())[:12]})
        entries = body.get("entries")
        for entry in entries[:200] if isinstance(entries, list) else []:
            if not isinstance(entry, dict):
                continue
            fields = {}
            for key, value in list(entry.items())[:12]:
                key = short_value(key, 12)
                if isinstance(value, bool) or not isinstance(value, (int, float, str)):
                    continue
                fields[key] = round(value, 1) if isinstance(value, float) else short_value(value)
            self.debug.write("client", **fields)
        return {"ok": True}

    def start_deck(self, deck_id: Any, revision: Any) -> dict[str, Any]:
        if type(deck_id) is not int:
            raise ReviewError("Choose a deck")

        def start() -> None:
            if mw.col is None or mw.col.decks.get(deck_id, default=False) is None:
                raise ReviewError("Deck was not found")
            if mw.state == "review":
                raise ReviewError("A review is already running")
            mw.col.decks.select(deck_id)
            mw.col.startTimebox()
            self._set_pending()
            mw.moveToState("review")
            if mw.state != "review":
                with self._changes:
                    self._pending_since = 0.0
                raise ReviewError("Congratulations! This deck has nothing due right now.")

        return self._perform(revision, start)

    def reveal(self, card_id: int, revision: Any, typed: str | None) -> dict[str, Any]:
        def show_answer() -> bool:
            reviewer = self._reviewer_card(card_id, "question")
            reviewer.typedAnswer = typed or ""
            self._set_pending()
            reviewer._showAnswer()
            return reviewer.state != "question"

        return self._perform(revision, show_answer)

    def answer(self, card_id: int, ease: int, revision: Any) -> dict[str, Any]:
        def answer_card() -> bool:
            reviewer = self._reviewer_card(card_id, "answer")
            if ease not in [item[0] for item in reviewer._answerButtonList()]:
                raise ReviewError("That answer button is not available for this card")
            self._set_pending()
            reviewer._answerCard(ease)
            return reviewer.state != "answer"

        return self._perform(revision, answer_card)

    def card_action(self, card_id: int, side: str, action: str, flag: int, revision: Any) -> dict[str, Any]:
        if side not in REVIEW_KINDS:
            raise ReviewError("Invalid card side")

        def run() -> None:
            reviewer = self._reviewer_card(card_id, side)
            if action == "mark":
                reviewer.toggle_mark_on_current_note()
            elif action == "flag" and type(flag) is int and 0 <= flag <= 7:
                reviewer.set_flag_on_current_card(flag)
            elif action in ("bury", "suspend"):
                self._set_pending()
                getattr(reviewer, f"{action}_current_card")()
            else:
                raise ReviewError("Unknown card action")

        return self._perform(revision, run, settle=action in ("bury", "suspend"))

    def undo(self, revision: Any) -> dict[str, Any]:
        def undo_last() -> None:
            if not self._undo_label():
                raise ReviewError("There is nothing to undo.")
            if mw.state == "review":
                self._set_pending()
            mw.undo()

        return self._perform(revision, undo_last)

    def stop_review(self, revision: Any) -> dict[str, Any]:
        def stop() -> None:
            if mw.state == "review":
                mw.moveToState("deckBrowser")

        return self._perform(revision, stop, settle=False)

    def start_sync(self) -> dict[str, Any]:
        """Start a normal AnkiWeb sync, exactly like pressing Sync on the desktop."""
        def start() -> dict[str, Any]:
            if mw.col is None:
                raise ReviewError("No Anki profile is open")
            if not self._is_syncing():
                media_syncer = getattr(mw, "media_syncer", None)
                if media_syncer is not None and media_syncer.is_syncing():
                    raise ReviewError("Anki is still syncing media. Try again in a moment.")
                sync_auth = getattr(mw.pm, "sync_auth", None)
                if callable(sync_auth) and not sync_auth():
                    raise ReviewError("Desktop Anki is not signed in to AnkiWeb.")
                trigger = getattr(mw, "on_sync_button_clicked", None) or getattr(mw, "onSync")
                self._sync_began()
                trigger()
            return self._session_state_on_main()

        return self.invoke_on_main(start)

    # ----- files (served without touching the GUI thread) -----

    def media_file(self, encoded_name: str) -> Path:
        if not self._media_dir:
            self._media_dir = self.invoke_on_main(lambda: mw.col.media.dir() if mw.col else None)
        if not self._media_dir:
            raise ReviewError("No Anki profile is open")
        try:
            return safe_media_file(self._media_dir, encoded_name)
        except ValueError as error:
            raise ReviewError("Not found") from error

    @staticmethod
    def anki_asset(encoded_path: str) -> Path:
        try:
            from aqt.utils import aqt_data_folder
            path = safe_asset(Path(aqt_data_folder()) / "web", encoded_path)
        except (ImportError, ValueError) as error:
            raise ReviewError("Not found") from error
        if path.suffix.lower() not in ANKI_ASSET_TYPES:
            raise ReviewError("Not found")
        return path

    @staticmethod
    def addon_asset(module: str, encoded_path: str) -> Path:
        # Mirrors Anki's media server: only paths an add-on explicitly exports.
        pattern = mw.addonManager.getWebExports(module) if re.fullmatch(r"[\w.-]+", module) else None
        if not pattern:
            raise ReviewError("Not found")
        try:
            path = safe_asset(mw.addonManager.addonsFolder(module), encoded_path)
            relative = path.relative_to(Path(mw.addonManager.addonsFolder(module)).resolve()).as_posix()
        except ValueError as error:
            raise ReviewError("Not found") from error
        if not re.fullmatch(pattern, relative) or path.suffix.lower() not in ADDON_ASSET_TYPES:
            raise ReviewError("Not found")
        return path


class AssetCache:
    """In-memory bytes, gzip and ETag for static files, keyed by mtime and size."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[Path, tuple[tuple[int, int], bytes, bytes | None, str]] = {}

    def get(self, path: Path, transform: Callable[[bytes], bytes] | None = None) -> tuple[bytes, bytes | None, str]:
        stat = path.stat()
        stamp = (stat.st_mtime_ns, stat.st_size)
        with self._lock:
            entry = self._entries.get(path)
        if entry is None or entry[0] != stamp:
            data = path.read_bytes()
            if transform:
                data = transform(data)
            compressed = gzip.compress(data, 6, mtime=0) if len(data) > 1024 and content_type(path.name).startswith(("text/", "application/json", "image/svg")) else None
            etag = '"' + hashlib.sha256(data).hexdigest()[:24] + '"'
            entry = (stamp, data, compressed, etag)
            with self._lock:
                self._entries[path] = entry
        return entry[1], entry[2], entry[3]


class ReviewerHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False
    allowed_network = None

    def verify_request(self, request: Any, client_address: Any) -> bool:
        return self.allowed_network is None or ipaddress.ip_address(client_address[0]) in self.allowed_network


# Backwards-compatible name used by earlier tests and tools.
LoopbackHTTPServer = ReviewerHTTPServer

APP_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
    "media-src 'self' data: blob:; font-src 'self' data:; connect-src 'self'; frame-src 'self'; "
    "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)
# The card frame is an opaque-origin sandbox: card scripts run as freely as on
# the desktop but cannot read the app, its cookie, or call the review API.
FRAME_SANDBOX = "allow-scripts allow-popups allow-popups-to-escape-sandbox allow-modals allow-forms"
FRAME_CSP = f"sandbox {FRAME_SANDBOX}; default-src * data: blob: 'unsafe-inline' 'unsafe-eval'; frame-ancestors 'self'"


class RemoteReviewHandler(BaseHTTPRequestHandler):
    server_version = "AnkiRemoteReviewer"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def __init__(self, *args: Any, reviewer: RemoteReviewer, **kwargs: Any) -> None:
        self.reviewer = reviewer
        super().__init__(*args, **kwargs)

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(LONG_POLL_SECONDS + 15)

    def log_message(self, _format: str, *_args: Any) -> None:
        # Deliberately avoid recording request paths, cookies, or tokens.
        # The opt-in debug log (debugLog) records route names and timings only.
        return

    # ----- timing -----

    def do_GET(self) -> None:  # noqa: N802
        self._timed("GET", self._handle_get)

    def do_POST(self) -> None:  # noqa: N802
        self._timed("POST", self._handle_post)

    def _timed(self, method: str, handle: Callable[[], None]) -> None:
        self._request_started = time.monotonic()
        self._status, self._length, self._note, self._gzip = 0, 0, "", False
        self.reviewer.reset_timing()
        self._requests = getattr(self, "_requests", 0) + 1
        try:
            handle()
        finally:
            debug = self.reviewer.debug
            path = urlsplit(self.path).path
            if debug.enabled and path != "/api/debug":
                route, ext = route_name(path)
                timing = self.reviewer.timing()
                if route == "/api/state" and "since=" in urlsplit(self.path).query:
                    route = "/api/state:poll"
                debug.write(
                    "req", method=method, route=route, ext=ext, status=self._status, kb=round(self._length / 1024, 1),
                    total_ms=ms(time.monotonic() - self._request_started),
                    anki_queue_ms=ms(timing["queue"]) if timing["calls"] else None,
                    anki_run_ms=ms(timing["run"]) if timing["calls"] else None,
                    poll_wait_ms=ms(timing["wait"]) if timing["wait"] else None,
                    gzip=self._gzip or None, range=bool(self.headers.get("Range")) or None,
                    via="lan" if self._is_lan() else "loopback", conn_req=self._requests, note=self._note)

    # ----- routing -----

    def _handle_get(self) -> None:
        self._headers_sent = False
        url = urlsplit(self.path)
        path = url.path
        try:
            if not self._valid_host():
                raise ReviewError("Use the configured address for this reviewer")
            if path in APP_FILES:
                transform = (lambda data: data.replace(b"{{VERSION}}", VERSION.encode())) if APP_FILES[path] == "index.html" else None
                self._serve_file(STATIC_ROOT / APP_FILES[path], "no-cache", transform=transform)
            elif path.startswith("/c/"):
                token, _, name = path[3:].partition("/")
                if not self.reviewer.valid_frame_token(token):
                    self._error(HTTPStatus.FORBIDDEN, "This card link has expired. Reload the page.")
                elif not name:
                    self._serve_file(STATIC_ROOT / "frame.html", "no-cache", csp=FRAME_CSP)
                elif name.startswith("_addons/"):
                    module, _, rest = name[len("_addons/"):].partition("/")
                    self._serve_file(self.reviewer.addon_asset(module, rest), "private, max-age=3600", cors=True, csp=ASSET_CSP)
                else:
                    self._serve_file(self.reviewer.media_file(name), "private, max-age=3600", cors=True, ranges=True,
                                     csp=ASSET_CSP)
            elif path.startswith("/_anki/"):
                # Desktop Anki's own public web files (reviewer.js, MathJax).
                self._serve_file(self.reviewer.anki_asset(path[len("/_anki/"):]), "public, max-age=86400", cors=True, csp=ASSET_CSP)
            elif path == "/api/state":
                if not self.reviewer.is_authenticated(self):
                    raise ReviewError("Authentication required")
                query = parse_qs(url.query)
                since = query.get("since", [""])[0]
                if since.isascii() and since.isdigit() and self.reviewer.wait_for_change(int(since)) is False:
                    self._json({"unchanged": True, "seq": int(since)})
                else:
                    self._json(self.reviewer.session_state())
            elif path == "/favicon.ico":
                self._send(HTTPStatus.NO_CONTENT, b"", {})
            else:
                self._error(HTTPStatus.NOT_FOUND, "Not found")
        except ReviewError as error:
            status = HTTPStatus.NOT_FOUND if str(error) == "Not found" else HTTPStatus.FORBIDDEN
            self._error(status, str(error))
        except ConnectionError:
            self._note = "client-disconnected"
            self.close_connection = True
        except Exception:
            self._note = exception_summary()
            self._fail("Anki is unavailable. Check the desktop window and reconnect.")

    def _handle_post(self) -> None:
        self._headers_sent = False
        path = urlsplit(self.path).path
        try:
            body = self._read_json_body()
            if not self._valid_host():
                raise ReviewError("Use the configured address for this reviewer")
            origin = self.headers.get("Origin")
            if origin and urlsplit(origin).netloc != self.headers.get("Host"):
                raise ReviewError("Cross-origin requests are not allowed")
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                raise ReviewError("JSON content type is required")
            if path == "/api/login":
                session = self.reviewer.issue_session(str(body.get("token", "")))
                self._json({"ok": True}, {"Set-Cookie": (
                    f"anki_remote_session={session}; Max-Age={TRUSTED_DEVICE_SECONDS}; " + self._session_cookie_flags())})
                return
            if not self.reviewer.is_authenticated(self):
                raise ReviewError("Authentication required")
            reviewer, revision = self.reviewer, body.get("revision")
            if path == "/api/start":
                result = reviewer.start_deck(body.get("deckId"), revision)
            elif path == "/api/reveal":
                typed = body.get("typed")
                result = reviewer.reveal(self._card_id(body), revision, typed if isinstance(typed, str) else None)
            elif path == "/api/answer":
                result = reviewer.answer(self._card_id(body), int(body.get("ease", 0)), revision)
            elif path == "/api/card-action":
                result = reviewer.card_action(self._card_id(body), str(body.get("side", "")), str(body.get("action", "")),
                                              body.get("flag", 0), revision)
            elif path == "/api/undo":
                result = reviewer.undo(revision)
            elif path == "/api/stop":
                result = reviewer.stop_review(revision)
            elif path == "/api/sync":
                result = reviewer.start_sync()
            elif path == "/api/debug":
                result = reviewer.record_client_timings(body)
            else:
                self._error(HTTPStatus.NOT_FOUND, "Not found")
                return
            self._json(result)
        except ReviewError as error:
            self._error(HTTPStatus.FORBIDDEN if str(error) in ("Authentication required", "Invalid access token")
                        else HTTPStatus.CONFLICT, str(error))
        except (TypeError, ValueError):
            self._error(HTTPStatus.BAD_REQUEST, "Invalid request body")
        except ConnectionError:
            self._note = "client-disconnected"
            self.close_connection = True
        except Exception:
            self._note = exception_summary()
            self._fail("Anki could not complete this request. Check the desktop window and reconnect.")

    # ----- helpers -----

    def _is_lan(self) -> bool:
        return getattr(getattr(self, "server", None), "allowed_network", None) is not None

    def _valid_lan_host(self) -> bool:
        if not self._is_lan():
            return True
        host, port = self.server.server_address
        return self.headers.get("Host") == f"{host}:{port}"

    def _valid_host(self) -> bool:
        if not self._valid_lan_host():
            return False
        if self.reviewer._config["requireAccessToken"] or self._is_lan():
            return True
        # Without a token, refuse DNS-rebinding hosts: only loopback or Tailscale names.
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].lower().strip("[]")
        return host in ("127.0.0.1", "localhost", "::1") or host.endswith(".ts.net")

    def _session_cookie_flags(self) -> str:
        # Only the explicit, subnet-restricted LAN socket permits HTTP cookies.
        # Forwarded headers cannot downgrade the HTTPS/Tailscale login cookie.
        return ("" if self._is_lan() else "Secure; ") + "HttpOnly; SameSite=Strict; Path=/"

    def _read_json_body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length < 1 or length > MAX_BODY_BYTES:
            self.close_connection = True
            raise ReviewError("Invalid request body")
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ReviewError("Invalid JSON") from error
        if not isinstance(body, dict):
            raise ReviewError("Invalid JSON")
        return body

    @staticmethod
    def _card_id(body: dict[str, Any]) -> int:
        card_id = body.get("cardId")
        if type(card_id) is not int:
            raise ReviewError("A current card ID is required")
        return card_id

    def _accepts_gzip(self) -> bool:
        return "gzip" in self.headers.get("Accept-Encoding", "")

    def _serve_file(self, path: Path, cache: str, *, transform: Callable[[bytes], bytes] | None = None,
                    csp: str | None = None, cors: bool = False, ranges: bool = False) -> None:
        if not path.is_file():
            raise ReviewError("Not found")
        headers = {"Content-Type": content_type(path.name), "Cache-Control": cache, "Vary": "Accept-Encoding"}
        if csp:
            headers["Content-Security-Policy"] = csp
        if cors:
            headers["Access-Control-Allow-Origin"] = "*"
        if ranges:
            # Media: stream from disk with byte ranges (audio seeking, iOS).
            stat = path.stat()
            etag = f'"{stat.st_mtime_ns:x}-{stat.st_size:x}"'
            headers.update({"ETag": etag, "Accept-Ranges": "bytes"})
            if self.headers.get("If-None-Match") == etag:
                self._send(HTTPStatus.NOT_MODIFIED, b"", headers)
                return
            size = stat.st_size
            try:
                start, end = byte_range(self.headers.get("Range"), size)
            except ValueError:
                self._send(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE, b"", {"Content-Range": f"bytes */{size}"})
                return
            partial_content = bool(self.headers.get("Range"))
            if partial_content:
                headers["Content-Range"] = f"bytes {start}-{end}/{size}"
            try:
                handle = path.open("rb")
            except OSError as error:
                raise ReviewError("Not found") from error
            with handle:
                self._start(HTTPStatus.PARTIAL_CONTENT if partial_content else HTTPStatus.OK, max(0, end - start + 1), headers)
                handle.seek(start)
                remaining = end - start + 1
                while remaining > 0 and (chunk := handle.read(min(256 * 1024, remaining))):
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
                if remaining:
                    self.close_connection = True  # file shrank; the promised length is wrong
            return
        data, compressed, etag = self.reviewer.assets.get(path, transform)
        headers["ETag"] = etag
        if self.headers.get("If-None-Match") == etag:
            self._send(HTTPStatus.NOT_MODIFIED, b"", headers)
            return
        if compressed is not None and self._accepts_gzip():
            data, headers["Content-Encoding"] = compressed, "gzip"
        self._send(HTTPStatus.OK, data, headers)

    def _json(self, value: Any, extra_headers: dict[str, str] | None = None, status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers = {"Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store", "Vary": "Accept-Encoding"}
        if len(data) > 1400 and self._accepts_gzip():
            data, headers["Content-Encoding"] = gzip.compress(data, 5, mtime=0), "gzip"
        self._send(status, data, {**headers, **(extra_headers or {})})

    def _error(self, status: HTTPStatus, message: str) -> None:
        if not getattr(self, "_note", "x"):
            self._note = short_value(message.replace(" ", "_"), 60)
        self._json({"error": message}, status=status)

    def _fail(self, message: str) -> None:
        # After headers have gone out a second response would corrupt the stream.
        if getattr(self, "_headers_sent", False):
            self.close_connection = True
            return
        try:
            self._error(HTTPStatus.SERVICE_UNAVAILABLE, message)
        except ConnectionError:
            self.close_connection = True

    def _start(self, status: HTTPStatus, length: int, headers: dict[str, str]) -> None:
        self._headers_sent = True
        self._status, self._length = int(status), length
        self._gzip = headers.get("Content-Encoding") == "gzip"
        if self.reviewer.debug.enabled and hasattr(self, "_request_started"):
            # Lets the phone subtract Anki's time from the round trip it measured.
            timing = self.reviewer.timing()
            headers = {**headers, "Server-Timing": f"total;dur={ms(time.monotonic() - self._request_started)}, "
                       f"anki;dur={ms(timing['queue'] + timing['run'])}, wait;dur={ms(timing['wait'])}"}
            if headers.get("Access-Control-Allow-Origin") == "*":
                headers["Timing-Allow-Origin"] = "*"
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        if status not in (HTTPStatus.NO_CONTENT, HTTPStatus.NOT_MODIFIED):
            self.send_header("Content-Length", str(length))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if "Content-Security-Policy" not in headers:
            self.send_header("Content-Security-Policy", APP_CSP)
            self.send_header("X-Frame-Options", "DENY")
        self.end_headers()

    def _send(self, status: HTTPStatus, data: bytes, headers: dict[str, str]) -> None:
        self._start(status, len(data), headers)
        if data:
            self.wfile.write(data)
