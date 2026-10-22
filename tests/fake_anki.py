"""A small fake Anki host for tests and the dev server.

It mimics what the add-on relies on, including the RWKV fork's behaviour of
ignoring input until the first card of a session has rendered.
"""
from __future__ import annotations

import importlib
import queue
import sys
import tempfile
import threading
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLAY = '<a class="replay-button soundLink" href=# onclick="pycmd(\'play:{side}:{index}\'); return false;">&#9654;</a>'


class Hook(list):
    def remove(self, callback):
        if callback in self:
            super().remove(callback)

    def __call__(self, *args):
        for callback in list(self):
            callback(*args)


class Filter(Hook):
    def __call__(self, value, *args):
        for callback in list(self):
            value = callback(value, *args)
        return value


class TaskManager:
    """A single 'GUI' thread, like Qt's main loop."""

    def __init__(self):
        self._queue = queue.Queue()
        threading.Thread(target=self._loop, name="fake-gui", daemon=True).start()

    def _loop(self):
        while True:
            self._queue.get()()

    def run_on_main(self, callback):
        self._queue.put(callback)

    def later(self, seconds, callback):
        timer = threading.Timer(seconds, lambda: self._queue.put(callback))
        timer.daemon = True
        timer.start()


class Tag:
    def __init__(self, filename=None, text=None):
        if filename:
            self.filename = filename
        else:
            self.field_text, self.lang, self.speed = text, "ja_JP", 1.0


class Note(dict):
    def __init__(self, fields, tags=()):
        super().__init__(fields)
        self.tags = list(tags)

    def has_tag(self, tag):
        return tag in self.tags


class Card:
    def __init__(self, card_id, did, front, back, q_audio=(), a_audio=()):
        self.id, self.did, self.ord = card_id, did, 0
        self._front, self._back = front, back
        self._q_audio, self._a_audio = list(q_audio), list(a_audio)
        self._note = Note({"Front": front})
        self.flag = 0

    def question(self):
        return "<style>.card{font-size:24px}</style>" + self._front

    def answer(self):
        # Anki renumbers front-side sounds as answer tags on the back.
        return self.question().replace("[anki:play:q:", "[anki:play:a:") + '<hr id="answer">' + self._back

    def question_av_tags(self):
        return self._q_audio

    def answer_av_tags(self):
        return self._q_audio + self._a_audio

    def autoplay(self):
        return True

    def replay_question_audio_on_answer_side(self):
        return False

    def user_flag(self):
        return self.flag

    def note(self, reload=False):
        return self._note

    def note_type(self):
        return {"name": "Fake", "css": ""}


class Reviewer:
    def __init__(self, mw):
        self.mw, self.state, self.card, self.typedAnswer = mw, None, None, None
        self._v3 = types.SimpleNamespace(states=["s1", "s2", "s3", "s4"])
        self._question_rendered = self._answer_rendered = False
        self.blocked_until = 0.0
        self.render_delay = 0.15
        self.first_card_block = 0.6

    def show(self):
        # The fork reloads its web view and may redraw the first card.
        self.blocked_until = time.monotonic() + self.first_card_block
        self.nextCard()

    def nextCard(self):
        cards = self.mw.col.queue
        if not cards:
            self.mw.moveToState("overview")
            return
        self.card = cards[0]
        self._show_question()

    def _show_question(self):
        self.state, self._question_rendered, self.typedAnswer = "question", False, None
        self.mw.gui_hooks.card_will_show(self._munge(self.card.question(), "q"), self.card, "reviewQuestion")
        card = self.card
        self.mw.taskman.later(self.render_delay, lambda: self._question_did_render(card))

    def _question_did_render(self, card):
        if self.card is card and self.state == "question":
            self._question_rendered = True
            self.mw.gui_hooks.reviewer_did_show_question(card)

    def _munge(self, text, side):
        for index, _ in enumerate(getattr(self.card, f"{'question' if side == 'q' else 'answer'}_av_tags")()):
            text = text.replace(f"[anki:play:{side}:{index}]", PLAY.format(side=side, index=index))
        return text.replace("[[type]]", '<input type=text id=typeans onkeypress="_typeAnsPress();">')

    def _review_actions_are_blocked(self):
        return time.monotonic() < self.blocked_until

    def _current_question_is_rendered(self):
        return self.state == "question" and self._question_rendered

    def _answer_actions_are_blocked(self):
        return False

    def _showAnswer(self):
        if self._review_actions_are_blocked() or not self._current_question_is_rendered():
            return  # silently ignored, like the fork
        self.state, self._answer_rendered = "answer", False
        typed = f"<div id=typed>{self.typedAnswer}</div>" if self.typedAnswer else ""
        self.mw.gui_hooks.card_will_show(self._munge(self.card.answer(), "a") + typed, self.card, "reviewAnswer")
        card = self.card
        self.mw.taskman.later(self.render_delay, lambda: self._answer_did_render(card))

    def _answer_did_render(self, card):
        if self.card is card and self.state == "answer":
            self._answer_rendered = True
            self.mw.gui_hooks.reviewer_did_show_answer(card)

    def _answerCard(self, ease):
        if self.state != "answer" or not self._answer_rendered:
            return
        self.state = "transition"
        self.mw.answers.append((self.card.id, ease))
        self.mw.col.undo_label = "Undo Answer Card"
        self.mw.col.queue.pop(0)
        if ease == 1:
            self.mw.col.queue.insert(min(2, len(self.mw.col.queue)), self.card)
        self.mw.taskman.later(0.05, self.nextCard)

    def _answerButtonList(self):
        return ((1, "Again"), (2, "Hard"), (3, "Good"), (4, "Easy"))

    def _remaining(self):
        n = len(self.mw.col.queue)
        return f"<span class=new-count>0</span> + <span class=learn-count>0</span> + <span class=review-count><u>{n}</u></span>"

    def toggle_mark_on_current_note(self):
        tags = self.card.note().tags
        tags.remove("marked") if "marked" in tags else tags.append("marked")
        self.mw.gui_hooks.operation_did_execute(None, None)

    def set_flag_on_current_card(self, flag):
        self.card.flag = 0 if self.card.flag == flag else flag
        self.mw.gui_hooks.operation_did_execute(None, None)

    def bury_current_card(self):
        self.mw.col.queue.pop(0)
        self.state = "transition"
        self.mw.taskman.later(0.05, self.nextCard)

    suspend_current_card = bury_current_card


class Collection:
    def __init__(self, media_dir, cards):
        self.queue = list(cards)
        self.conf = {"estTimes": True}
        self.media = types.SimpleNamespace(dir=lambda: str(media_dir))
        self.decks = types.SimpleNamespace(
            get=lambda did, default=True: {"id": did, "name": "Japanese"} if did in (1, 2) else None,
            select=lambda did: None,
            current=lambda: {"id": 1},
            name=lambda did: {1: "Japanese", 2: "Japanese::Mining"}.get(did, "Default"),
        )
        child = types.SimpleNamespace(deck_id=2, name="Mining", children=[], new_count=0, learn_count=0,
                                      review_count=len(cards), collapsed=False, filtered=False)
        parent = types.SimpleNamespace(deck_id=1, name="Japanese", children=[child], new_count=0, learn_count=0,
                                       review_count=len(cards), collapsed=False, filtered=False)
        self.sched = types.SimpleNamespace(
            deck_due_tree=lambda: types.SimpleNamespace(children=[parent]),
            describe_next_states=lambda states: ["<1m", "6m", "1d", "4d"],
        )
        self.undo_label = ""

    def startTimebox(self):
        pass

    def undo_status(self):
        return types.SimpleNamespace(undo=self.undo_label)


def sample_cards():
    hover = '<div id="kanjiHover"><div id="word">{w}</div></div><script src="_hover.js"></script>'
    return [
        Card(101, 2, hover.format(w="部屋") + "[anki:play:q:0]", "<div>room</div>[anki:play:a:1]",
             q_audio=[Tag("word heya.wav")], a_audio=[Tag("sentence.wav")]),
        Card(102, 2, hover.format(w="窓") + "[[type]]", "<div>window</div>", q_audio=[Tag(text="まど")]),
        Card(103, 2, r"<div>plain card \(x^2\)</div>", "<div>answer</div>"),
    ]


HOVER_JS = """
(function () {
  var root = document.getElementById('kanjiHover');
  if (!root) return;
  root.innerHTML = root.innerHTML.replace(/([\\u4E00-\\u9FAF])(?![^<]*>)/g, '<span class="kanjiHoverTarget">$1</span>');
  var tip = document.createElement('div');
  tip.className = 'hoverDiv';
  tip.style.cssText = 'display:none;position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);background:#1e1a1e;color:#fff;padding:8px;border-radius:6px';
  document.body.appendChild(tip);
  Array.prototype.forEach.call(document.getElementsByClassName('kanjiHoverTarget'), function (el) {
    el.addEventListener('mouseenter', function () { tip.textContent = 'Kanji: ' + el.textContent; tip.style.display = 'block'; });
    el.addEventListener('mouseleave', function () { tip.style.display = 'none'; });
  });
  window.hoverRuns = (window.hoverRuns || 0) + 1;
  try { localStorage.setItem('hoverRuns', String(window.hoverRuns)); } catch (e) { window.storageError = String(e); }
})();
"""


def silent_wav(seconds=0.2, rate=8000):
    import io
    import wave
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(1)
        out.setframerate(rate)
        out.writeframes(bytes([128]) * int(seconds * rate))
    return buffer.getvalue()


def install(config=None, cards=None):
    """Install fake aqt modules and return (mw, package name)."""
    media_dir = Path(tempfile.mkdtemp(prefix="arr-media-"))
    (media_dir / "_hover.js").write_text(HOVER_JS, encoding="utf-8")
    for name in ("word heya.wav", "sentence.wav"):
        (media_dir / name).write_bytes(silent_wav())
    (media_dir / "secret.txt").write_text("nope", encoding="utf-8")
    (media_dir / "STEINS;GATE0 1.webp").write_bytes(b"RIFF" + bytes(4) + b"WEBP")

    hooks = types.SimpleNamespace(**{name: Hook() for name in (
        "reviewer_did_show_question", "reviewer_did_show_answer", "reviewer_will_end", "profile_will_close",
        "profile_did_open", "state_did_change", "operation_did_execute", "main_window_did_init",
        "sync_will_start", "sync_did_finish")})
    hooks.card_will_show = Filter()

    mw = types.SimpleNamespace()
    mw.gui_hooks = hooks
    mw.taskman = TaskManager()
    mw.col = Collection(media_dir, cards if cards is not None else sample_cards())
    mw.state = "deckBrowser"
    mw.answers = []
    mw.reviewer = Reviewer(mw)
    mw.pm = types.SimpleNamespace(sync_auth=lambda: True)
    stored = {"config": dict(config or {})}
    mw.addonManager = types.SimpleNamespace(
        getConfig=lambda name: dict(stored["config"]),
        writeConfig=lambda name, value: stored.update(config=dict(value)),
        getWebExports=lambda module: None,
        addonsFolder=lambda module: str(ROOT.parent / module),
    )
    mw.prepare_card_text_for_display = lambda text: text

    def move_to_state(state):
        old, mw.state = mw.state, state
        if old == "review" and state != "review":
            hooks.reviewer_will_end()
        hooks.state_did_change(state, old)
        if state == "review":
            mw.reviewer.show()

    mw.moveToState = move_to_state

    def undo():
        if mw.answers:
            card_id, _ = mw.answers.pop()
            mw.col.undo_label = "Undo Answer Card" if mw.answers else ""
            card = next(c for c in sample_cards() if c.id == card_id)
            mw.col.queue.insert(0, card)
            mw.reviewer.state = "transition"
            mw.taskman.later(0.05, mw.reviewer.nextCard)

    mw.undo = undo
    mw.on_sync_button_clicked = lambda: (hooks.sync_will_start(), mw.taskman.later(0.3, hooks.sync_did_finish))

    aqt = types.ModuleType("aqt")
    aqt.mw, aqt.gui_hooks = mw, hooks
    qt = types.ModuleType("aqt.qt")
    qt.QAction = qt.QApplication = qt.QMessageBox = types.SimpleNamespace(
        warning=lambda *a: None, information=lambda *a: None, clipboard=lambda: None)
    utils = types.ModuleType("aqt.utils")
    utils.qconnect = lambda *a: None
    utils.aqt_data_folder = lambda: str(ROOT / "tests" / "no-anki-data")
    sys.modules.update({"aqt": aqt, "aqt.qt": qt, "aqt.utils": utils})
    for name in [n for n in sys.modules if n.startswith("reviewer_under_test")]:
        del sys.modules[name]
    package = types.ModuleType("reviewer_under_test")
    package.__path__ = [str(ROOT)]
    sys.modules[package.__name__] = package
    return mw, package.__name__


def load_backend(config=None, cards=None, anki_data=None):
    mw, package = install(config, cards)
    if anki_data:
        sys.modules["aqt.utils"].aqt_data_folder = lambda: str(anki_data)
    backend = importlib.import_module(package + ".backend")
    return mw, backend
