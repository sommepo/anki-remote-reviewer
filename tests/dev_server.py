"""Serve the reviewer against a fake Anki for browser testing.

    python tests/dev_server.py [port]

Uses desktop Anki's own web assets when found (set ANKI_DATA to an aqt data
folder to override); otherwise the card frame runs its built-in fallback.
"""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fake_anki  # noqa: E402

port = int(sys.argv[1]) if len(sys.argv) > 1 else 28610
installed = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/Anki/app_packages/_aqt/data"
anki_data = os.environ.get("ANKI_DATA") or (str(installed) if installed.is_dir() else None)
mw, backend = fake_anki.load_backend(
    {"port": port, "accessToken": "dev-token", "requireAccessToken": True}, anki_data=anki_data)
reviewer = backend.RemoteReviewer()
if reviewer._server is None:
    sys.exit(f"Could not start on port {port}; choose another free port.")
print(f"Fake Anki reviewer on http://127.0.0.1:{port}  (token: dev-token, Anki assets: {anki_data or 'fallback'})", flush=True)
while True:
    time.sleep(3600)
