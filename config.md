# Configuration

Restart Anki after changes. Never share your installed `meta.json`.

- `port`: `28594`. Loopback HTTP listener; Windows listeners and exclusions
  are checked before binding. Keep the private HTTPS proxy pointed here.
- `requireAccessToken`: `true`. Disable only if every device/user able to
  reach the service is trusted to see card content and submit reviews. When
  disabled, only `127.0.0.1`/`localhost` and `*.ts.net` host names are accepted.
- `accessToken`: empty in distribution; generated on first startup. Changing
  it blocks new logins with the old token but does not revoke existing cookies.
- `cookieSecret`: generated privately. Clear it and restart to revoke all
  remembered browsers and card media links. Do not publish or reuse it.
- `autoplayAudio`: `true`. Follow each deck's autoplay option on the phone;
  `false` never autoplays (Replay still works). Replaces 0.2's
  `autoPlayAudioOnAnswer`, which is migrated automatically.
- `requestTimeoutSeconds`: `12`, constrained to 2–30 seconds. How long an
  action waits for desktop Anki to be ready (for example while the first card
  of a session loads) before reporting that Anki is busy.
- `debugLog`: `false`. Writes a timing log to `user_files/debug.log` in the
  add-on folder for diagnosing slow connections (Tools > Remote Reviewer: Show
  debug log). Records timings, sizes and route names only: no file names, card
  content or tokens. See the README.
- `rwkvNoteTypes`: `[]`. Exact note-type names whose answers show live
  retrievability in the bottom bar. Requires the RWKV fork's scheduler.
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
directly from AV tags without fallback mappings. Invalid values show a warning
at startup and disable only the affected feature.

# Optional trusted-LAN access

`lanAddress` and `lanSubnet` default to empty (disabled). Set an explicit private
IPv4 address assigned to the Anki computer and its subnet, then restart Anki.
The additional listener uses the same `port`; loopback/Tailscale stays enabled.
LAN mode requires token authentication, restricts clients to that subnet, and
accepts only the configured IP:port Host header. Add a similarly restricted OS
firewall rule. Never use a wildcard address or router port forwarding.

LAN HTTP is unencrypted, including tokens, session cookies, and card content.
Use only on a trusted LAN. Tailscale cookies retain their Secure flag; only the
explicit LAN socket issues HTTP-compatible cookies. If DHCP changes the host
address, update this configuration or reserve its address in your router.
