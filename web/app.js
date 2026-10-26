(() => {
  "use strict";
  const VERSION = document.querySelector('meta[name="app-version"]').content;
  const $ = id => document.getElementById(id);
  const ui = {
    decksButton: $("decks-button"), title: $("title"), connection: $("connection"), sync: $("sync"),
    undo: $("undo"), more: $("more"), menu: $("menu"), themeItem: $("theme-item"),
    notice: $("notice"), noticeText: $("notice-text"), noticeButton: $("notice-button"),
    login: $("login"), loginForm: $("login-form"), token: $("token"),
    decks: $("decks"), deckList: $("deck-list"), deckEmpty: $("deck-empty"),
    review: $("review"), card: $("card"), loading: $("loading"),
    bottom: $("bottom"), counts: $("counts"), rwkv: $("rwkv"), actions: $("actions"),
  };

  let state = null;            // last rendered server state
  let busy = false;            // an action request is in flight
  let connected = false;
  let needsLogin = false;
  let stale = false;           // the add-on was updated; this page must reload
  let loadingTimer = 0;
  let pollController = null;
  let wake = null;             // resolves the poll loop's wait
  let syncRequested = false, syncFlashUntil = 0;
  let deckSignature = "", actionsSignature = "";
  const expanded = new Map();  // deck id -> expanded (overrides Anki's collapsed flag)
  const frame = { token: "", ready: false, pending: null, shownKey: "", shownHtml: "" };
  const typedRequests = new Map();
  let typedId = 0;
  const idleWaiters = [];

  function setBusy(value) {
    busy = value;
    if (!busy) idleWaiters.splice(0).forEach(resolve => resolve());
    controls();
  }

  // Polls wait while an action is in flight; its response is the newer state.
  function whenIdle() {
    return busy ? new Promise(resolve => idleWaiters.push(resolve)) : Promise.resolve();
  }

  const store = {
    get(key) { try { return localStorage.getItem(key); } catch { return null; } },
    set(key, value) { try { value === null ? localStorage.removeItem(key) : localStorage.setItem(key, value); } catch {} },
  };
  // ----- opt-in timing report ("debugLog" in the add-on's config) -----
  // Sends only route names, file extensions, sizes and timings to the add-on's
  // debug log — never file names, card content or tokens.
  const debug = {
    entries: [], timer: 0, sentInfo: false, observing: false,
    get on() { return !!state?.debugLog; },
    add(entry) {
      if (!this.on) return;
      this.entries.push({ at: new Date().toTimeString().slice(0, 8), ...entry });
      if (this.entries.length > 300) this.entries.splice(0, this.entries.length - 300);
      if (this.entries.length >= 25 && connected) this.flush();
      else this.schedule();
    },
    schedule(ms = 20000) { if (!this.timer) this.timer = setTimeout(() => { this.timer = 0; this.flush(); }, ms); },
    async flush() {
      clearTimeout(this.timer); this.timer = 0;
      if (!this.entries.length) return;
      if (!connected) { this.schedule(5000); return; }
      const entries = this.entries.splice(0, 100);
      const body = { entries };
      if (!this.sentInfo) body.info = clientInfo();
      try {
        await api("/api/debug", body, { timeout: 15000, record: false });
        this.sentInfo = true;
        if (this.entries.length) this.schedule(1000);
      } catch {
        this.entries.unshift(...entries);
        this.schedule(10000);
      }
    },
    observe() {
      if (this.observing || !this.on || typeof PerformanceObserver !== "function") return;
      this.observing = true;
      try {
        new PerformanceObserver(list => list.getEntries().forEach(entry => {
          const timing = resourceTiming(entry);
          if (timing) this.add({ src: "page", ...timing });
        })).observe({ type: "resource", buffered: true });
      } catch {}
    },
  };

  function clientInfo() {
    const connection = navigator.connection || {};
    return {
      ua: navigator.userAgent, standalone: matchMedia("(display-mode: standalone)").matches,
      net: connection.effectiveType || "", netRtt: connection.rtt ?? "", downlinkMbps: connection.downlink ?? "",
      screen: `${screen.width}x${screen.height}`,
    };
  }

  // Card media, Anki's scripts and add-on files; API calls are timed in api().
  function resourceTiming(entry) {
    let path;
    try { path = decodeURIComponent(new URL(entry.name, location.href).pathname); } catch { return null; }
    const route = path.startsWith("/c/") ? (path.includes("/_addons/") ? "addon-asset" : "media")
      : path.startsWith("/_anki/") ? "anki-asset" : null;
    if (!route || /^\/c\/[^/]+\/$/.test(path)) return null;
    const ext = (path.match(/\.([A-Za-z0-9]{1,5})$/) || [, ""])[1].toLowerCase();
    const result = { r: route, ext, ms: Math.round(entry.duration) };
    if (entry.responseStart > 0 && entry.requestStart > 0) result.ttfb = Math.round(entry.responseStart - entry.requestStart);
    if (entry.transferSize > 0) result.kb = Math.round(entry.transferSize / 102.4) / 10;
    else if (entry.decodedBodySize > 0) result.cached = 1;
    return result;
  }

  let dark = (store.get("anki-remote-theme") || (matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark")) === "dark";

  // ----- notices -----

  let noticeTimer = 0;
  function notice(text, { kind = "error", button = "", onClick = null, sticky = false } = {}) {
    clearTimeout(noticeTimer);
    ui.noticeText.textContent = text || "";
    ui.notice.hidden = !text;
    ui.notice.classList.toggle("info", kind === "info");
    ui.noticeButton.hidden = !button;
    ui.noticeButton.textContent = button;
    ui.noticeButton.onclick = onClick;
    if (text && !sticky) noticeTimer = setTimeout(() => notice(""), 5000);
  }

  // ----- audio -----
  const player = new Audio();
  player.preload = "auto";
  const SILENCE = "data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEAQB8AAEAfAAABAAgAZGF0YQAAAAA=";
  let audioGeneration = 0, finishAudio = null, audioUnlocked = false;

  function mediaUrl(file) { return `/c/${frame.token}/${encodeURIComponent(file)}`; }

  function stopAudio() {
    audioGeneration++;
    player.pause();
    if (finishAudio) finishAudio();
    try { speechSynthesis.cancel(); } catch {}
  }

  // iOS only lets an element play without a tap once a tap has played it.
  function unlockAudio() {
    if (audioUnlocked) return;
    audioUnlocked = true;
    if (player.paused) { player.src = SILENCE; player.play().catch(() => {}); }
  }

  function speak(tts, generation) {
    return new Promise(resolve => {
      if (generation !== audioGeneration || !("speechSynthesis" in window)) return resolve();
      const utterance = new SpeechSynthesisUtterance(tts.text);
      utterance.lang = (tts.lang || "").replace("_", "-");
      utterance.rate = tts.speed || 1;
      utterance.onend = utterance.onerror = () => { finishAudio = null; resolve(); };
      finishAudio = () => { finishAudio = null; resolve(); };
      speechSynthesis.speak(utterance);
    });
  }

  async function playEntries(entries) {
    stopAudio();
    const generation = audioGeneration;
    for (const entry of entries) {
      if (generation !== audioGeneration) return;
      if (!entry) continue;
      if (entry.tts) { await speak(entry.tts, generation); continue; }
      player.src = mediaUrl(entry.file);
      const ended = new Promise(resolve => {
        const finish = () => {
          player.removeEventListener("ended", finish);
          player.removeEventListener("error", finish);
          finishAudio = null; resolve();
        };
        finishAudio = finish;
        player.addEventListener("ended", finish);
        player.addEventListener("error", finish);
      });
      try { await player.play(); }
      catch (error) {
        if (generation === audioGeneration) {
          if (finishAudio) finishAudio();
          if (error.name === "NotAllowedError") notice("Tap Replay audio (in ⋯) to allow sound in this browser.");
          else if (error.name !== "AbortError") notice("Audio could not play. The file may be missing or in an unsupported format.");
        }
        return;
      }
      await ended;
      if (player.error && generation === audioGeneration) {
        notice("Audio could not finish playing. Try Replay audio.");
        return;
      }
    }
  }
  // ----- end audio -----

  function replay() {
    if (!state || !["question", "answer"].includes(state.status)) return;
    const { q, a } = state.audio;
    void playEntries(state.status === "question" ? q : [...(state.replayQuestion ? q : []), ...a]);
  }

  function prefetch(entries) {
    for (const entry of entries.slice(0, 4)) {
      if (entry?.file) fetch(mediaUrl(entry.file), { priority: "low" }).catch(() => {});
    }
  }

  // ----- network -----

  async function api(path, body, { timeout = 30000, signal, record = true } = {}) {
    const started = performance.now();
    const route = path.split("?")[0] + (path.includes("since=") ? ":poll" : "");
    let headersAt = 0, status = 0, serverTiming = "";
    const note = extra => {
      if (!record || !debug.on) return;
      const entry = { r: route, rtt: Math.round(performance.now() - started), ...extra };
      if (headersAt) entry.hdr = Math.round(headersAt - started);
      const part = name => { const match = serverTiming.match(new RegExp(name + ";dur=(\\d+)")); return match ? Number(match[1]) : null; };
      const total = part("total");
      if (total !== null) {
        entry.srv = total;
        entry.anki = part("anki");
        if (part("wait")) entry.wait = part("wait");
        entry.net = Math.max(0, entry.hdr - total);  // round trip minus time spent in the add-on
      }
      debug.add(entry);
    };
    const controller = new AbortController();
    let timedOut = false;
    const timer = setTimeout(() => { timedOut = true; controller.abort(); }, timeout);
    const forward = () => controller.abort();
    signal?.addEventListener("abort", forward);
    try {
      let response;
      try {
        response = await fetch(path, {
          method: body ? "POST" : "GET", credentials: "same-origin", cache: "no-store",
          headers: body ? { "Content-Type": "application/json" } : {},
          body: body ? JSON.stringify(body) : undefined, signal: controller.signal,
        });
      } catch (error) {
        const kind = timedOut ? "timeout" : signal?.aborted ? "aborted" : "network";
        if (kind !== "aborted") note({ err: kind });
        throw Object.assign(new Error(timedOut ? "Anki took too long to respond." : "Cannot reach Anki."), { kind });
      }
      headersAt = performance.now(); status = response.status;
      serverTiming = response.headers.get("Server-Timing") || "";
      let data = {};
      try { data = await response.json(); } catch {}
      note({ st: status, kb: Math.round((Number(response.headers.get("Content-Length")) || 0) / 102.4) / 10 });
      if (!response.ok) {
        const message = data.error || `Anki could not complete the request (${response.status}).`;
        const auth = message === "Authentication required" || message === "Invalid access token";
        throw Object.assign(new Error(message), { kind: auth ? "auth" : response.status === 409 ? "conflict" : "server" });
      }
      return data;
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener("abort", forward);
    }
  }

  function handleError(error) {
    if (error.kind === "aborted") return;
    if (error.kind === "auth") {
      needsLogin = true; connected = false;
      if (error.message === "Invalid access token") notice("That token was not accepted.");
      showLogin();
    } else if (error.kind === "conflict") {
      notice(error.message);
      kickPoll();
    } else if (error.kind === "server") {
      notice(error.message);
    } else {
      connected = false;
      stopAudio();
      notice(error.message + " Reconnecting…", { sticky: true, button: "Retry", onClick: kickPoll });
    }
    controls();
  }

  function sleep(ms) {
    return new Promise(resolve => { const timer = setTimeout(resolve, ms); wake = () => { clearTimeout(timer); resolve(); }; });
  }

  function kickPoll() {
    pollController?.abort();
    wake?.();
  }

  async function pollLoop() {
    let failures = 0;
    for (;;) {
      while (needsLogin || stale || document.hidden) await sleep(60000);
      await whenIdle();
      pollController = new AbortController();
      const since = connected && state ? `?since=${state.seq}` : "";
      try {
        const next = await api("/api/state" + since, null, { timeout: (25 + serverTimeout() + 10) * 1000, signal: pollController.signal });
        failures = 0;
        await whenIdle();
        if (!connected) { connected = true; notice(""); }
        if (!next.unchanged) render(next);
        else controls();
      } catch (error) {
        if (error.kind === "aborted") continue;
        handleError(error);
        if (error.kind !== "auth") await sleep(Math.min(8000, 500 * 2 ** failures++));
      }
    }
  }

  function serverTimeout() { return state?.timeoutSeconds || 12; }

  async function perform(path, body = {}, prepare = null) {
    if (busy || !connected || !state) return null;
    setBusy(true);
    try {
      if (prepare) body = { ...body, ...await prepare() };
      const next = await api(path, { ...body, revision: state.revision }, { timeout: (3 * serverTimeout() + 10) * 1000 });
      render(next);
      return next;
    } catch (error) { handleError(error); return null; }
    finally { setBusy(false); }
  }

  // ----- card frame -----

  function bodyClass(next) {
    return `card card${next.ord + 1}` + (dark ? " nightMode night_mode" : "");
  }

  function postFrame(message) {
    if (message.type === "show") frame.pending = message;
    else if (message.type === "marks" && frame.pending) Object.assign(frame.pending, message, { type: "show" });
    if (!frame.ready) return;
    ui.card.contentWindow.postMessage(message, "*");
    if (message.type === "show") frame.pending = null;
  }

  function ensureFrame(token) {
    // Keep the current card page (and its scripts' state) while its token has
    // more than a day left; tokens are valid for a week.
    const expires = Number(frame.token.split(".")[0]) * 1000;
    if (ui.card.getAttribute("src") && (frame.token === token || expires - Date.now() > 86400000)) return;
    frame.token = token; frame.ready = false; frame.shownKey = ""; frame.shownHtml = "";
    ui.card.src = `/c/${token}/`;
  }

  function cardStorage() {
    const items = {};
    try {
      for (let i = 0; i < localStorage.length; i++) {
        const key = localStorage.key(i);
        if (key.startsWith("card:")) items[key.slice(5)] = localStorage.getItem(key);
      }
    } catch {}
    return items;
  }

  function askTyped() {
    if (!frame.ready) return Promise.resolve(null);
    const id = ++typedId;
    return new Promise(resolve => {
      const timer = setTimeout(() => { typedRequests.delete(id); resolve(null); }, 400);
      typedRequests.set(id, value => { clearTimeout(timer); resolve(value); });
      ui.card.contentWindow.postMessage({ type: "typed", id }, "*");
    });
  }

  window.addEventListener("message", event => {
    if (event.source !== ui.card.contentWindow || !event.data) return;
    const message = event.data;
    if (message.type === "ready") {
      frame.ready = true;
      ui.card.contentWindow.postMessage({ type: "init", storage: cardStorage(), dark, debug: debug.on }, "*");
      if (frame.pending) postFrame(frame.pending);
    } else if (message.type === "cmd") bridgeCommand(String(message.cmd));
    else if (message.type === "key") handleKey(message, true);
    else if (message.type === "storage" && typeof message.key === "string") {
      store.set("card:" + message.key, typeof message.value === "string" ? message.value : null);
    } else if (message.type === "typed") {
      typedRequests.get(message.id)?.(typeof message.value === "string" ? message.value : null);
      typedRequests.delete(message.id);
    } else if (message.type === "timing" && Array.isArray(message.entries)) {
      message.entries.slice(0, 50).forEach(entry => {
        if (entry && typeof entry === "object") debug.add({ src: "card", r: String(entry.r), ext: String(entry.ext || ""),
          ms: Number(entry.ms) || 0, ...(entry.ttfb ? { ttfb: Number(entry.ttfb) } : {}),
          ...(entry.kb ? { kb: Number(entry.kb) } : {}), ...(entry.cached ? { cached: 1 } : {}) });
      });
    }
  });

  // pycmd()/bridgeCommand() calls from card templates, as desktop Anki handles them.
  function bridgeCommand(command) {
    const play = /^play:([qa]):(\d+)$/.exec(command);
    if (play && state?.audio) {
      const entry = state.audio[play[1]][Number(play[2])];
      if (entry) void playEntries([entry]);
    } else if (command === "ans" && state?.status === "question") void reveal();
    else if (/^ease[1-4]$/.test(command) && state?.status === "answer") void answer(Number(command.slice(4)));
  }

  // ----- rendering -----

  function show(node, visible) { node.hidden = !visible; }

  function render(next) {
    if (next.version !== VERSION) {
      connected = false; stale = true;
      stopAudio();
      notice("Anki Remote Reviewer was updated in Anki. Reload this page to continue.",
        { sticky: true, button: "Reload", onClick: () => location.reload() });
      controls();
      return;
    }
    if (state && next.instance === state.instance && next.seq < state.seq) return;
    const previous = state;
    state = next; connected = true; needsLogin = false;
    debug.observe();
    const reviewing = ["question", "answer", "loading"].includes(next.status);
    show(ui.login, false);
    show(ui.decks, next.status === "idle");
    show(ui.review, reviewing);
    show(ui.bottom, reviewing);
    show(ui.decksButton, reviewing);
    show(ui.loading, next.status === "loading");
    ui.connection.classList.remove("bad");
    clearTimeout(loadingTimer);
    if (next.status === "loading") {
      loadingTimer = setTimeout(() => notice("Waiting for desktop Anki — check for a dialog on the computer.",
        { kind: "info" }), 5000);
    }
    if (next.status === "idle") {
      ui.title.textContent = "Decks";
      if (previous && previous.status !== "idle") leaveReview();
      renderDecks(next);
    } else if (next.status !== "loading") {
      ui.title.textContent = next.deck;
      ensureFrame(next.frameToken);
      showCard(next);
      renderBottom(next);
    }
    if (previous?.syncing && !next.syncing && syncRequested) {
      syncRequested = false; syncFlashUntil = Date.now() + 4000;
      setTimeout(controls, 4100);
    }
    controls();
  }

  // Like leaving desktop Anki's reviewer: stop sound and clear the card page.
  function leaveReview() {
    stopAudio();
    frame.shownKey = frame.shownHtml = ""; frame.pending = null; actionsSignature = "";
    if (frame.ready) ui.card.contentWindow.postMessage({ type: "clear" }, "*");
  }

  function showCard(next) {
    const key = `${next.revision}:${next.status}`;
    const flags = { flag: next.flag, marked: next.marked };
    if (key === frame.shownKey && next.html === frame.shownHtml) {
      postFrame({ type: "marks", ...flags });
      return;
    }
    const newView = key !== frame.shownKey;
    frame.shownKey = key; frame.shownHtml = next.html;
    postFrame({ type: "show", side: next.status, html: next.html, bodyClass: bodyClass(next), dark, ...flags });
    if (!newView) return;
    stopAudio();
    if (next.status === "question") {
      if (next.autoplay) void playEntries(next.audio.q);
      prefetch(next.audio.a);
    } else if (next.autoplay) void playEntries(next.audio.a);
  }

  function renderBottom(next) {
    ui.rwkv.textContent = next.rwkv ? "R " + next.rwkv : "";
    ui.counts.replaceChildren();
    if (next.counts) {
      ["new", "learn", "review"].forEach((kind, index) => {
        if (index) ui.counts.append(" + ");
        const span = document.createElement("span");
        span.className = kind;
        const text = String(next.counts[kind]);
        if (next.counts.current === kind) { const u = document.createElement("u"); u.textContent = text; span.append(u); }
        else span.textContent = text;
        ui.counts.append(span);
      });
    }
    const buttons = next.status === "question" ? [{ reveal: true, label: "Show answer" }] : next.buttons;
    const signature = JSON.stringify([next.cardId, next.status, buttons]);
    if (signature === actionsSignature) return;
    actionsSignature = signature;
    ui.actions.replaceChildren(...buttons.map(item => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = item.reveal ? "reveal" : "ease-" + item.ease;
      if (item.interval) {
        const interval = document.createElement("small");
        interval.textContent = item.interval;
        button.append(interval);
      }
      button.append(item.label);
      button.addEventListener("click", () => { item.reveal ? reveal() : answer(item.ease); });
      return button;
    }));
  }

  function renderDecks(next) {
    const signature = JSON.stringify([next.decks, next.currentDeckId, [...expanded]]);
    if (signature === deckSignature) return;
    deckSignature = signature;
    show(ui.deckEmpty, !next.decks.length);
    const items = [];
    let hiddenBelow = Infinity;
    for (const deck of next.decks) {
      if (deck.depth > hiddenBelow) continue;
      hiddenBelow = Infinity;
      const open = expanded.has(deck.id) ? expanded.get(deck.id) : !deck.collapsed;
      if (deck.hasChildren && !open) hiddenBelow = deck.depth;
      const li = document.createElement("li");
      li.className = "deck" + (deck.id === next.currentDeckId ? " current" : "");
      li.style.paddingLeft = deck.depth * 18 + "px";
      const toggle = document.createElement("button");
      toggle.type = "button";
      toggle.className = "deck-toggle" + (deck.hasChildren ? "" : " blank");
      toggle.textContent = open ? "−" : "+";
      if (deck.hasChildren) toggle.setAttribute("aria-label", (open ? "Collapse " : "Expand ") + deck.name);
      else { toggle.tabIndex = -1; toggle.setAttribute("aria-hidden", "true"); }
      toggle.addEventListener("click", () => { expanded.set(deck.id, !open); renderDecks(state); });
      const start = document.createElement("button");
      start.type = "button";
      start.className = "deck-open";
      const name = document.createElement("span");
      name.className = "deck-name";
      name.textContent = deck.name;
      const counts = document.createElement("span");
      counts.className = "deck-counts";
      for (const [kind, value] of [["new", deck.new], ["learn", deck.learn], ["review", deck.review]]) {
        const count = document.createElement("span");
        count.textContent = value;
        count.style.color = value ? `var(--${kind})` : "";
        if (!value) count.className = "zero";
        counts.append(count);
      }
      start.append(name, counts);
      start.addEventListener("click", () => startDeck(deck.id));
      li.append(toggle, start);
      items.push(li);
    }
    ui.deckList.replaceChildren(...items);
  }

  function controls() {
    const ready = connected && !busy && !!state;
    const onCard = ready && ["question", "answer"].includes(state.status);
    ui.undo.disabled = !ready || !state.canUndo;
    ui.undo.title = state?.undoLabel ? `${state.undoLabel} (Ctrl+Z)` : "Undo (Ctrl+Z)";
    ui.decksButton.disabled = !ready;
    ui.actions.querySelectorAll("button").forEach(button => { button.disabled = !onCard; });
    ui.menu.querySelectorAll("[data-card]").forEach(button => { button.disabled = !onCard; });
    ui.menu.querySelectorAll("[data-flag]").forEach(button => {
      button.classList.toggle("active", Number(button.dataset.flag) === state?.flag);
    });
    ui.menu.querySelector('[data-action="mark"]').firstChild.textContent = state?.marked ? "Unmark note" : "Mark note";
    ui.themeItem.textContent = dark ? "Light mode" : "Dark mode";
    ui.sync.disabled = !ready || !!state.syncing;
    ui.sync.textContent = state?.syncing ? "Syncing…" : Date.now() < syncFlashUntil ? "Synced ✓" : "Sync";
    ui.sync.title = state?.lastSync
      ? "Sync with AnkiWeb (last synced " + new Date(state.lastSync * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) + ")"
      : "Sync with AnkiWeb";
    ui.connection.textContent = needsLogin ? "Sign in required" : !connected ? "Disconnected"
      : state?.status === "loading" || busy ? "Working…" : "Connected";
    ui.connection.classList.toggle("bad", !connected);
  }

  function showLogin() {
    stopAudio();
    show(ui.login, true); show(ui.decks, false); show(ui.review, false); show(ui.bottom, false);
    ui.title.textContent = "Anki";
    controls();
    ui.token.focus();
  }

  // ----- actions -----

  function defaultEase() {
    return state.buttons.some(b => b.ease === 3) ? 3 : 2;
  }

  function reveal() {
    if (busy || state?.status !== "question") return;
    unlockAudio();
    return perform("/api/reveal", { cardId: state.cardId }, async () => ({ typed: await askTyped() }));
  }

  function answer(ease) {
    if (state?.status !== "answer" || !state.buttons.some(b => b.ease === ease)) return;
    unlockAudio();
    stopAudio();
    return perform("/api/answer", { cardId: state.cardId, ease });
  }

  async function startDeck(deckId) {
    unlockAudio();
    const next = await perform("/api/start", { deckId });
    if (next?.status === "idle") notice("Congratulations! This deck has nothing due right now.", { kind: "info" });
  }

  function cardAction(action, flag = 0) {
    if (!["question", "answer"].includes(state?.status)) return;
    if (action === "flag" && flag === state.flag) flag = 0;
    return perform("/api/card-action", { cardId: state.cardId, side: state.status, action, flag });
  }

  function toggleTheme() {
    dark = !dark;
    store.set("anki-remote-theme", dark ? "dark" : "light");
    applyTheme();
    if (frame.ready) ui.card.contentWindow.postMessage({ type: "theme", dark }, "*");
  }

  function applyTheme() {
    document.body.dataset.theme = dark ? "dark" : "light";
    document.querySelector('meta[name="theme-color"]').content = dark ? "#222222" : "#ffffff";
    controls();
  }

  function setMenu(open) {
    ui.menu.hidden = !open;
    ui.more.setAttribute("aria-expanded", String(open));
  }

  // Keys reported by the card frame could be forged by card scripts, so they
  // get only what pycmd already allows card scripts on the desktop.
  function handleKey(event, fromFrame = false) {
    if (event.repeat || event.altKey) return false;
    const key = event.key;
    if (fromFrame && !["Escape", " ", "Enter", "1", "2", "3", "4", "r", "R", "F5"].includes(key)) return false;
    if ((event.ctrlKey || event.metaKey) && key.toLowerCase() === "z") { perform("/api/undo"); return true; }
    if (event.ctrlKey || event.metaKey) return false;
    if (key === "Escape") { setMenu(false); return true; }
    if (!connected || !state) return false;
    const status = state.status;
    if (key === " " || key === "Enter") {
      if (status === "question") reveal();
      else if (status === "answer") answer(defaultEase());
      else return false;
    } else if (/^[1-4]$/.test(key) && status === "answer") answer(Number(key));
    else if (key === "r" || key === "R" || key === "F5") replay();
    else if (key === "*") cardAction("mark");
    else if (key === "-") cardAction("bury");
    else if (key === "@") cardAction("suspend");
    else return false;
    return true;
  }

  // ----- wiring -----

  ui.loginForm.addEventListener("submit", async event => {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    const button = ui.loginForm.querySelector("button");
    button.disabled = true;
    try {
      await api("/api/login", { token: ui.token.value.trim() });
      ui.token.value = "";
      needsLogin = false; connected = false;
      notice("");
      render(await api("/api/state"));
    } catch (error) { handleError(error); }
    finally { setBusy(false); button.disabled = false; kickPoll(); }
  });
  ui.decksButton.addEventListener("click", () => perform("/api/stop"));
  ui.undo.addEventListener("click", () => perform("/api/undo"));
  ui.sync.addEventListener("click", async () => {
    syncRequested = true;
    if (!await perform("/api/sync")) syncRequested = false;
  });
  ui.more.addEventListener("click", event => { event.stopPropagation(); setMenu(ui.menu.hidden); });
  ui.menu.addEventListener("click", event => {
    const button = event.target.closest("button");
    if (!button || button.disabled) return;
    setMenu(false);
    if (button.dataset.flag) cardAction("flag", Number(button.dataset.flag));
    else if (button.dataset.action === "replay") { unlockAudio(); replay(); }
    else if (button.dataset.action === "theme") toggleTheme();
    else cardAction(button.dataset.action);
  });
  document.addEventListener("pointerdown", () => {
    if (frame.ready) ui.card.contentWindow.postMessage({ type: "leave" }, "*");
  });
  document.addEventListener("click", event => { if (!ui.menu.hidden && !ui.menu.contains(event.target)) setMenu(false); });
  // Keys the app does not use go to the card, as desktop Anki's card has focus.
  const forwardedKeys = new Set();
  function forwardKey(event, phase) {
    if (!frame.ready || !["question", "answer"].includes(state?.status) || ["Shift", "Control", "Alt", "Meta"].includes(event.key)) return;
    ui.card.contentWindow.postMessage({ type: "key", phase, key: event.key, code: event.code, ctrlKey: event.ctrlKey,
      metaKey: event.metaKey, altKey: event.altKey, shiftKey: event.shiftKey, repeat: event.repeat }, "*");
  }
  document.addEventListener("keydown", event => {
    if (event.target.closest?.("input,textarea,select,[contenteditable]") && event.key !== "Escape") return;
    if (event.target.closest?.("button") && (event.key === " " || event.key === "Enter")) return;
    if (handleKey(event)) { event.preventDefault(); return; }
    forwardedKeys.add(event.code);
    forwardKey(event, "keydown");
  });
  document.addEventListener("keyup", event => {
    if (forwardedKeys.delete(event.code)) forwardKey(event, "keyup");
  });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) { stopAudio(); debug.flush(); }
    else kickPoll();
  });
  window.addEventListener("pagehide", stopAudio);
  window.addEventListener("online", kickPoll);

  applyTheme();
  void pollLoop();
})();
