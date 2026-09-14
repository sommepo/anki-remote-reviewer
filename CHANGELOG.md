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
- Test MP3/OPUS and autoplay on real iOS Safari and Android Chrome over HTTPS.
- Visually check long cards, narrow screens, and representative note types.
- Test review/undo, desktop dialogs, and reconnect with a disposable collection.

Automated tests do not establish those device/runtime results. This package
is a beta for public testers, not a claim of stable compatibility.
