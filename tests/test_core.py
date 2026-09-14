"""Regression checks with a fake Anki host; never opens a collection or port."""
import importlib
import io
import json
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
package = types.ModuleType('reviewer_test_package')
package.__path__ = [str(ROOT)]
sys.modules[package.__name__] = package
aqt = types.ModuleType('aqt')
aqt.mw = Mock()
aqt.gui_hooks = Mock()
sys.modules['aqt'] = aqt
qt = types.ModuleType('aqt.qt')
qt.QAction = qt.QApplication = qt.QMessageBox = Mock()
sys.modules['aqt.qt'] = qt
utils = types.ModuleType('aqt.utils')
utils.qconnect = Mock()
sys.modules['aqt.utils'] = utils
media = importlib.import_module('reviewer_test_package.media')
backend = importlib.import_module('reviewer_test_package.backend')

class MediaTests(unittest.TestCase):
    def test_tts_slots_and_duplicates_keep_indexes(self):
        card = Mock()
        card.answer_av_tags.return_value = [types.SimpleNamespace(), types.SimpleNamespace(filename='b.mp3'), types.SimpleNamespace(filename='b.mp3')]
        files = media.audio_filenames(card, 'a')
        self.assertEqual(files, [None, 'b.mp3', 'b.mp3'])
        html = media.rewrite_media('[anki:play:a:1]', {'a': files})
        self.assertIn('/media/b.mp3', html)

    def test_unicode_and_escaped_media(self):
        html = media.rewrite_media('<img src="%E9%9B%A8%20%26%20%E9%9B%AA.png">[sound:雨.mp3]', {})
        self.assertIn('/media/%E9%9B%A8%20%26%20%E9%9B%AA.png', html)
        self.assertNotIn('%25E9', html)
        self.assertIn('anki-remote-audio', html)

    def test_range_requests(self):
        for header, expected in [('bytes=0-1', (0, 1)), ('bytes=5-', (5, 9)), ('bytes=-3', (7, 9)), ('bytes=2-50', (2, 9))]:
            self.assertEqual(media.byte_range(header, 10), expected)
        for header in ['bytes=20-', 'bytes=3-1', 'bytes=-0', 'bytes=0-1,4-5']:
            with self.assertRaises(ValueError): media.byte_range(header, 10)

    def test_local_css_fonts_and_images_are_rewritten(self):
        css = media.rewrite_css('@font-face {src:url("_font.woff2")} .card {background:url(bg%20image.png)}')
        self.assertIn('/media/_font.woff2', css)
        self.assertIn('/media/bg%20image.png', css)
        self.assertEqual(media.rewrite_css('url(data:image/png;base64,abc)'), 'url(data:image/png;base64,abc)')

    def test_media_paths_cannot_escape_collection(self):
        with tempfile.TemporaryDirectory() as directory:
            for name in ['../secret', '%2e%2e%5csecret', 'C%3Asecret', 'x%00.mp3', '/secret']:
                with self.assertRaises(ValueError): media.safe_media_file(directory, name)

class ReviewerTests(unittest.TestCase):
    def setUp(self):
        self.server = backend.RemoteReviewer.__new__(backend.RemoteReviewer)
        self.server._config = {'accessToken': 'test-token', 'cookieSecret': 'test-secret', 'requireAccessToken': True,
                               'audioFallbackFields': {}, 'rwkvNoteTypes': [], 'autoPlayAudioOnAnswer': True}
        self.server._revision = 'current'
        self.server._pending = False
        self.server.invoke_on_main = lambda fn: fn()

    def test_cookie_survives_restart_and_rejects_tampering(self):
        cookie = self.server.issue_session('test-token')
        handler = types.SimpleNamespace(headers={'Cookie': 'anki_remote_session=' + cookie})
        self.assertTrue(self.server.is_authenticated(handler))
        handler.headers['Cookie'] += 'x'
        self.assertFalse(self.server.is_authenticated(handler))
        with patch.object(backend.time, 'time', return_value=time.time() + 91 * 86400):
            handler.headers['Cookie'] = 'anki_remote_session=' + cookie
            self.assertFalse(self.server.is_authenticated(handler))

    def test_stale_revision_and_pending_are_rejected(self):
        with self.assertRaises(backend.ReviewError): self.server._guard('old')
        self.server._guard('current')
        self.server._pending = True
        with self.assertRaises(backend.ReviewError): self.server._guard('current')

    def test_answer_cannot_be_submitted_twice(self):
        reviewer = Mock()
        reviewer._answerButtonList.return_value = [(1, 'Again'), (3, 'Good')]
        self.server._active_card = Mock(return_value=(reviewer, Mock()))
        self.server._session_state_on_main = Mock(return_value={'status': 'loading'})
        self.server.answer(42, 3, 'current')
        with self.assertRaises(backend.ReviewError): self.server.answer(42, 3, 'current')
        reviewer._answerCard.assert_called_once_with(3)

    def test_fallback_is_explicit_and_ordered(self):
        card = Mock()
        card.answer_av_tags.side_effect = AttributeError()
        card.note_type.return_value = {'name': 'Custom'}
        card.note.return_value = {'Sentence': '[sound:sentence.mp3]', 'Word': '[sound:word.mp3]'}
        self.assertEqual(self.server._audio_filenames(card, 'a'), [])
        self.server._config['audioFallbackFields'] = {'Custom': {'a': ['Sentence', 'Word']}}
        self.assertEqual(self.server._audio_filenames(card, 'a'), ['sentence.mp3', 'word.mp3'])

    def test_actual_interval_preference_and_no_front_answer_media(self):
        reviewer = Mock(state='question')
        reviewer.card.id = 42
        reviewer.card.ord = 0
        reviewer.card.did = 1
        reviewer.card.note_type.return_value = {'name': 'Basic', 'css': ''}
        reviewer.card.question_av_tags.return_value = []
        reviewer.card.answer_av_tags.return_value = [types.SimpleNamespace(filename='answer.mp3')]
        reviewer.card.question.return_value = 'Question'
        reviewer.card.answer.return_value = 'Answer'
        reviewer._answerButtonList.return_value = [(3, 'Good')]
        host = Mock(state='review', reviewer=reviewer)
        host.col.conf = {'estTimes': False}
        host.col.decks.get.return_value = {'name': 'Example'}
        with patch.object(backend, 'mw', host):
            front = self.server.session_state()
            self.assertFalse(front['showIntervals'])
            self.assertEqual(front['answerAudio'], [])
            self.assertIsNone(front['rwkv'])
            reviewer.state = 'answer'
            host.col.conf['estTimes'] = True
            host.col.sched.describe_next_states.return_value = ['1m', '2d', '3d', '4d']
            self.assertEqual(self.server.session_state()['buttons'][0]['interval'], '3d')

    def test_cross_origin_rejected_before_review_action(self):
        handler = backend.RemoteReviewHandler.__new__(backend.RemoteReviewHandler)
        handler.path = '/api/answer'
        handler.headers = {'Origin': 'https://evil.example', 'Host': 'review.example', 'Content-Type': 'application/json'}
        handler._error = Mock()
        handler.reviewer = Mock()
        handler.do_POST()
        handler._error.assert_called_once()
        handler.reviewer.answer.assert_not_called()

if __name__ == '__main__': unittest.main()
