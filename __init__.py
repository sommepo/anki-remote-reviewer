"""Anki entry point. Importing helpers does not start a server."""
import atexit
from aqt import mw
from aqt.qt import QAction
from aqt.utils import qconnect
from .backend import RemoteReviewer

remote_reviewer = RemoteReviewer()
atexit.register(remote_reviewer.stop)
action = QAction('Remote Reviewer: Show connection details', mw)
qconnect(action.triggered, remote_reviewer.show_connection_details)
mw.form.menuTools.addAction(action)
debug_action = QAction('Remote Reviewer: Show debug log', mw)
qconnect(debug_action.triggered, remote_reviewer.show_debug_log)
mw.form.menuTools.addAction(debug_action)
