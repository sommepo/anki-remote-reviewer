# Anki Remote Reviewer

Review cards from your phone while desktop Anki does the scheduling.

**0.2.0-beta.1 · Windows host · Private-network use**

An independent Anki add-on with a small browser interface. Start a deck, reveal
answers, grade cards, undo a review, and play card audio. Your desktop collection
remains the source of truth. Standard scheduling and an installed RWKV scheduler
continue to run in Anki. AnkiConnect is not required.

## Install

1. Back up your collection through Anki before trying the beta.
2. Choose **Tools → Add-ons → Install from file**, select the `.ankiaddon`,
   and restart Anki. Keep your profile open and computer awake.
3. Choose **Tools → Remote Reviewer: Show connection details** to copy your
   access token. The reviewer listens on `127.0.0.1:28594` only.
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
   select a deck, and choose **Study now**. No review needs to be active first.

See [Tailscale Serve documentation](https://tailscale.com/docs/reference/tailscale-cli/serve)
for prerequisites. Do not use Funnel or expose this server directly to the
public internet. Sharing the add-on does not require publishing your collection.

## Everyday use

- **Space** reveals; **1–4** answers; **R** replays answer audio.
- Answer audio plays sequentially on a web answer flip. Changing cards cancels
  the remaining audio. Replay and individual buttons remain if autoplay fails.
- Dark mode is the default. The Light/Dark choice is remembered per browser.
  Answer buttons stay near the bottom while scrolling on mobile.
- Intervals follow Anki's next-review-time preference (`estTimes`) and the
  current reviewer's scheduling states.
- Sign-in lasts 90 days across Anki restarts. Cookie deletion, a different
  browser/origin, expiration, or resetting the signing secret requires login.
- All clients share desktop Anki's reviewer. Use one device at a time. Stale
  and repeated actions are rejected; independent review sessions are unsupported.

## Configuration

Use **Tools → Add-ons → Anki Remote Reviewer → Config**, then restart Anki.
See the [configuration reference](config.md) for optional token authentication,
answer autoplay, and RWKV/audio mappings. Fresh installations contain no
personal note types or media fields. RWKV is opt-in per note type, answer-only,
and computed by the installed scheduler, not read from a note field.

## Compatibility and beta limits

Developed against a Windows Anki 26.5-derived RWKV fork. The integration uses
modern Anki reviewer APIs. Stock versions and real iOS/Android devices still
need release testing; broad compatibility is not claimed. Automatic port and
reservation checks currently support Windows hosts only.

Supported: static template HTML/CSS, local images/fonts, and browser-supported
audio files. Not supported: arbitrary template JavaScript, `pycmd`, native TTS,
type-answer comparisons, video commands in sound tags, or external resources.
Script-driven note types and desktop add-on widgets can look different. MP3
is widely usable; OPUS support depends on the browser/device. No transcoding
is included. There is no offline review, editing, or sync interface.

Desktop modal dialogs and operations can pause reviewing. Resolve them in Anki
and reconnect. Anki may return to the deck picker when a deck is finished.

## Troubleshooting

- **No audio:** try Replay audio and check the media file exists. TTS requires
  desktop Anki. Forks without AV tags need explicit fields in playback order.
- **Can't connect:** check Anki is open and Tailscale Serve status. Restart
  after an update, then reload. The connection label tooltip shows the version.
- **Port conflict:** the add-on reports conflicts and leaves other services
  alone. Update `port` and the proxy together. Ports 8765 and 8766 are reserved
  for AnkiConnect and an OCR bridge.

## Development and release

No npm dependencies or frontend build step. With Python 3.10+ and Node.js:

```powershell
python tests/test_core.py
node --test tests/audio.test.cjs
python tools/package.py
```

Tests use a fake Anki host; no real cards are reviewed. The packaging allowlist
excludes `meta.json`, collection media, caches, and local previews. `dist/`
contains the installable add-on, source ZIP, and SHA-256 checksums. See
[release notes](CHANGELOG.md) for the outstanding beta test checklist.

Independent project, not affiliated with Ankitects. Original code is under
the [MIT License](LICENSE). Anki and Tailscale have their own licenses.
