# Configuration

Restart Anki after changes. Never share your installed `meta.json`.

- `port`: `28594`. Loopback HTTP listener; Windows listeners and exclusions
  are checked before binding. Keep the private HTTPS proxy pointed here.
- `requireAccessToken`: `true`. Disable only if every device/user able to
  reach the service is trusted to see card content and submit reviews.
- `accessToken`: empty in distribution; generated on first startup. Changing
  it blocks new logins with the old token but does not revoke existing cookies.
- `cookieSecret`: generated privately. Clear it and restart to revoke all
  remembered browsers. Do not publish or reuse it elsewhere.
- `autoPlayAudioOnAnswer`: `true`. Plays answer files in order. Browser policy
  can require a tap on Replay audio.
- `requestTimeoutSeconds`: `12`, constrained to 2–30 seconds. A timed-out
  request is cancelled if not started. If already running, reconnect to check
  its outcome before retrying.
- `rwkvNoteTypes`: `[]`. Exact note-type names whose answers display live
  retrievability. Requires the RWKV fork's scheduler integration.
- `audioFallbackFields`: `{}`. For forks lacking AV tags. Lists note fields
  in exact template playback order for question (`q`) and answer (`a`).

Optional example (merge these keys into your existing configuration):

```json
{
  "rwkvNoteTypes": ["My note type"],
  "audioFallbackFields": {
    "My note type": {"q": [], "a": ["Word audio", "Sentence audio"]}
  }
}
```

These are examples, not requirements. Ordinary Anki determines audio order
directly from AV tags without fallback mappings.
