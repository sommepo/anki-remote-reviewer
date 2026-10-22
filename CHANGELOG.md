# 0.3.0

Cards now behave like desktop Anki.

- Cards are the exact HTML desktop Anki shows (including add-on content from
  `card_will_show`) and run with Anki's own reviewer script in a sandboxed
  frame: template JavaScript, `<script src>` media files, MathJax, image
  occlusion, `onUpdateHook`/`onShownHook`, platform classes (`mobile`,
  `iphone`, `android`) and night-mode classes all work. Tested with the Lapis
  note type and kanji-hover templates.
- Fixed: cards could render very small or unstyled on phones because template
  CSS was rewritten into a shadow root.
- Fixed: the first answer reveal of a session often failed on the RWKV fork
  ("Anki is not ready to reveal yet"). Actions now wait until desktop Anki is
  ready instead of failing.
- Fixed: the reviewer refused to start after restarting Anki while old
  connections to its port were still closing.
- Type-in answers, `pycmd` replay buttons, `ans`/`ease` commands and TTS tags
  work. Keys the app does not use reach the card (Lapis ← → and C).
- Audio follows the deck's autoplay and replay-question options;
  `autoPlayAudioOnAnswer` becomes `autoplayAudio` (migrated automatically).
- Faster: instant updates by long-polling instead of 2-second polling, actions
  return the next card in one round trip, cached and compressed assets, media
  served without waiting on Anki's main thread, answer audio prefetched.
- New: deck list with due counts, remaining counts, flags, mark, bury,
  suspend, undo of any desktop action, AnkiWeb sync button, system theme,
  home-screen app mode.
- Invalid configuration shows a warning and disables only that feature
  instead of stopping the add-on.
- Tests: fake-fork integration tests over HTTP and a headless-Chrome
  end-to-end test.

# 0.2.0-beta.1

- Working Decks, replay, theme, and reconnect controls; no machine-specific UI.
- Sequential, cancellable answer audio with clear autoplay errors.
- Correct `estTimes` preference and live scheduling-state interval labels.
- AV indexes and duplicates preserved; explicit fork fallback mappings.
- HTTP byte ranges and audio content types for browser playback.
- Stale-revision guards, transition handling, queued-request cancellation.
- Persistent browser sign-in and automatic reconnection.
- Optional RWKV display per note type, shown only on the answer.
- Passive card rendering, request-origin checks, content-security policy,
  media path validation, and private no-store media responses.
- Regression tests and reproducible packages excluding secrets.

## Before calling this stable

- Test actual stock Anki versions and record which versions pass.
- Test audio, autoplay and template scripts on real iOS Safari and Android
  Chrome over HTTPS.
- Test review/undo, desktop dialogs, and reconnect with a disposable collection.

Automated tests do not establish those device/runtime results.
