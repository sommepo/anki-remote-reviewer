# Anki Remote Reviewer

Review cards from your phone while desktop Anki does the scheduling.

**0.3.0 · Windows host · Private-network use · Beta**

[Download Anki Remote Reviewer 0.3.0 (.ankiaddon)](https://github.com/sommepo/anki-remote-reviewer/releases/download/v0.3.0/anki-remote-reviewer-0.3.0.ankiaddon)

An independent Anki add-on with a small browser interface. Pick a deck, reveal
answers, grade cards, undo, flag, mark, bury, suspend, sync and play card audio.
Standard scheduling and an installed RWKV scheduler keep running in Anki.
AnkiConnect is not required.

Cards look and behave as they do on the desktop: the phone shows the exact HTML
desktop Anki renders and runs it with Anki's own reviewer script, so template
JavaScript works — hover/tap kanji lookups, Lapis's pitch accent, dictionary
switching and image zoom, `onShownHook`, MathJax, image occlusion, type-in
answers, `{{tts}}` and add-on content added to cards.

## Install

1. Back up your collection through Anki before trying the beta.
2. Choose **Tools → Add-ons → Install from file**, select the `.ankiaddon`,
   and restart Anki. Keep your profile open and computer awake.
3. Choose **Tools → Remote Reviewer: Show connection details** to copy your
   access token. The reviewer listens on `127.0.0.1:28594` (plus an optional
   trusted-LAN address you configure).
4. Configure a private HTTPS reverse proxy on the same computer. With Tailscale
   connected on both computer and phone, inspect existing Serve routes first:

   ```powershell
   tailscale serve status
   tailscale serve --bg --https=443 http://127.0.0.1:28594
   ```

   This assumes that HTTPS route is unused. Preserve existing routes; choose
   another unused HTTPS port if needed. The reviewer expects the root path of
   its own origin, not a `/reviewer` subdirectory.
5. Open the HTTPS URL reported by Tailscale on your phone. Enter the token once,
   then tap a deck. Add it to your home screen for a full-screen app.

See [Tailscale Serve documentation](https://tailscale.com/docs/reference/tailscale-cli/serve)
for prerequisites. Do not use Funnel or expose this server directly to the
public internet. A trusted-LAN option without Tailscale is described in the
[configuration reference](config.md).

## Everyday use

- **Space/Enter** reveals or answers Good; **1–4** answer; **R** replays audio;
  **Ctrl+Z** undoes; **\*** marks, **-** buries, **@** suspends. Other keys go to
  the card, as on the desktop (for example Lapis's ← → and C).
- The ⋯ menu has replay, mark, flags, bury, suspend and the Light/Dark switch
  (it follows your phone's theme until you choose).
- Audio follows the deck's autoplay and "replay question" options; TTS tags use
  the phone's speech voices. If the browser blocks autoplay, use Replay audio.
- The deck list shows new/learning/review counts; the review screen shows the
  remaining counts and interval labels like the desktop.
- Changes made on the desktop appear on the phone immediately.
- Sign-in lasts 90 days across Anki restarts.
- All clients share desktop Anki's reviewer. Use one device at a time; stale
  and repeated actions are rejected.

## Configuration

Use **Tools → Add-ons → Anki Remote Reviewer → Config**, then restart Anki.
See the [configuration reference](config.md) for token authentication,
autoplay, trusted-LAN access and RWKV/audio mappings. Fresh installations
contain no personal note types or media fields.

## How cards run

Card HTML runs in a sandboxed frame with its own origin. Template scripts can do
what they do on the desktop — read the card, fetch from the web, open links in
a new tab — but cannot read the app, its sign-in cookie, or answer cards except
through Anki's own `pycmd` commands (`ans`, `ease1`–`ease4`, `play:q|a:N`), as on
the desktop. Add-on files used by cards (`/_addons/…`) are served only through
the same signed card link, for file types used in cards. `localStorage` is kept per browser; `sessionStorage` and page
globals last for the review session, like desktop Anki's card page.

Card media is fetched with a signed link that expires after at most eight
days, so card scripts and fonts load without the sign-in cookie. Anyone holding
a current link can read media files until it expires; links are never shown in
the interface, and clearing `cookieSecret` revokes them. Card scripts can also
send key presses, but only the ones `pycmd` already allows (reveal, answer,
replay); undo, bury, suspend and mark need a real tap or keypress in the app.

Not supported: desktop add-ons that inject scripts into the reviewer page
itself (rather than into card HTML), `pycmd` commands other than those above,
voice recording, and video playback through Anki's player (`[sound:]` videos play
their audio only). Fonts must be in a format the phone's browser supports.

## Compatibility

Developed against a Windows Anki 26.x RWKV fork and uses modern Anki reviewer
APIs (23.10+). When Anki's own `reviewer.js` cannot be found, a built-in
fallback runs card scripts without MathJax or image occlusion. Stock versions
and every phone browser have not been release-tested. Automatic port checks
support Windows hosts only.

Desktop modal dialogs can pause reviewing. Resolve them in Anki; the phone
reconnects on its own.

## Troubleshooting

- **No audio:** tap Replay audio in the ⋯ menu and check the media file exists.
  Forks without AV tags need explicit fields in playback order.
- **Can't connect:** check Anki is open and `tailscale serve status`. Restart
  Anki after updating the add-on, then reload the page.
- **Slow over Tailscale (or anywhere):** turn on the debug log (below), use the
  reviewer until it is slow, then send the log file.
- **Port conflict:** the add-on reports conflicts and leaves other services
  alone. Update `port` and the proxy together. Ports 8765 and 8766 are reserved
  for AnkiConnect and an OCR bridge.

### Debug log

Off by default. To turn it on: Tools > Add-ons > Anki Remote Reviewer > Config,
set `"debugLog": true`, and restart Anki. Review on the phone until it feels
slow, then choose Tools > Remote Reviewer: Show debug log to open the folder
containing `debug.log`.

Each line is one request with timings, so you can see where the time goes:

- `req` lines come from Anki. `anki_queue_ms` is time waiting for Anki's main
  window to be free; `anki_run_ms` is time spent inside Anki;
  `poll_wait_ms` is normal idle waiting for the next change.
- `client` lines come from the phone. `rtt` is the full round trip it saw,
  `srv` the part spent in the add-on, and `net` the rest: the network,
  including Tailscale. High `net` with low `srv` points at the connection
  (for example a Tailscale relay; check `tailscale ping`), not at Anki.
  `src=card`/`src=page` lines time card pictures, audio and scripts.

The log contains only route names, file extensions, sizes, status codes and
timings, plus the phone's browser and connection type. It never contains file
names, card content, cookies or the access token. It keeps about 1 MB, plus one
older file. Turn it off again when you are done.

## Development and release

No npm dependencies or frontend build step. With Python 3.10+ and Node.js 22+:

```powershell
python tests/test_core.py
node --test tests/audio.test.cjs
python tests/dev_server.py 28610          # fake Anki; token: dev-token
node tests/e2e.mjs http://127.0.0.1:28610 # needs Chrome or Edge
python tools/package.py
```

The Python tests and the dev server use a fake Anki host that mimics the RWKV
fork's reviewer; no real cards are reviewed. The end-to-end test drives a
phone-sized headless Chrome through sign-in, template scripts, hover, type-in
answers, keys, flags and undo. Restart the dev server before each e2e run. The
packaging allowlist excludes `meta.json`, collection media and caches. `dist/`
contains the installable add-on, source ZIP and SHA-256 checksums. See the
[release notes](CHANGELOG.md).

Independent project, not affiliated with Ankitects. Original code is under
the [MIT License](LICENSE). Anki and Tailscale have their own licenses; the
add-on serves Anki's reviewer files from your own Anki installation and does
not redistribute them.
