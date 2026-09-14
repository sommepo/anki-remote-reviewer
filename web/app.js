(() => {
  "use strict";
  const $ = id => document.getElementById(id);
  const ui = {
    login: $("login-panel"), decks: $("deck-panel"), review: $("review-panel"),
    message: $("message"), title: $("deck-name"), connection: $("connection-state"),
    select: $("deck-select"), card: $("card"), actions: $("answer-actions"),
    rwkv: $("rwkv"), status: $("status"), undo: $("undo"), theme: $("theme-toggle"),
    replay: $("replay"), deckButton: $("decks-button"), retry: $("retry"),
  };
  let state = null, busy = false, polling = false, epoch = 0, connected = false;
  let signature = "", root = null, audioGeneration = 0, finishAudio = null;
  let awaitingAnswer = false;
  const player = new Audio();
  player.preload = "auto";
  const show = (node, yes) => node.classList.toggle("hidden", !yes);
  const message = text => { ui.message.textContent = text || ""; };

  async function api(path, body) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 16000);
    try {
      const response = await fetch(path, {
        method: body ? "POST" : "GET", credentials: "same-origin", cache: "no-store",
        headers: body ? { "Content-Type": "application/json" } : {},
        body: body ? JSON.stringify(body) : undefined, signal: controller.signal,
      });
      const data = await response.json();
      if (!response.ok) {
        const error = new Error(data.error || "Anki could not complete the request.");
        error.auth = data.error === "Authentication required" || data.error === "Invalid access token";
        throw error;
      }
      return data;
    } finally { clearTimeout(timeout); }
  }

  function stopAudio() {
    audioGeneration++;
    player.pause();
    if (finishAudio) finishAudio();
    player.removeAttribute("src");
    player.load();
    ui.replay.textContent = "Replay audio";
  }

  async function playSequence(files) {
    stopAudio();
    const generation = audioGeneration;
    for (const file of files) {
      if (generation !== audioGeneration) return;
      if (!file.startsWith("/media/")) continue;
      player.src = file;
      ui.replay.textContent = "Playing…";
      const ended = new Promise(resolve => {
        const finish = () => {
          player.removeEventListener("ended", finish);
          player.removeEventListener("error", finish);
          finishAudio = null; resolve();
        };
        finishAudio = finish;
        player.addEventListener("ended", finish, { once: true });
        player.addEventListener("error", finish, { once: true });
      });
      try { await player.play(); }
      catch (error) {
        if (generation === audioGeneration) {
          message(error.name === "NotAllowedError"
            ? "Tap Replay audio to allow sound in this browser."
            : "Audio could not play. The file may be missing or its format unsupported.");
          stopAudio();
        }
        return;
      }
      await ended;
      if (player.error && generation === audioGeneration) {
        message("Audio could not finish playing. Try Replay audio.");
        break;
      }
    }
    if (generation === audioGeneration) ui.replay.textContent = "Replay audio";
  }

  function renderCard(html, css) {
    root ||= ui.card.attachShadow({ mode: "open" });
    // Template HTML is content, not trusted application code. Keep styles and
    // layout but strip active elements and handlers before insertion.
    const template = document.createElement("template");
    template.innerHTML = html;
    template.content.querySelectorAll("script,iframe,object,embed,base,link,meta,form").forEach(n => n.remove());
    template.content.querySelectorAll("*").forEach(node => {
      for (const attr of [...node.attributes]) {
        if (/^on/i.test(attr.name) || ["srcdoc", "action", "formaction", "autoplay"].includes(attr.name)) node.removeAttribute(attr.name);
        if (["href", "src", "poster", "xlink:href"].includes(attr.name)) {
          const value = attr.value.trim();
          if (value && !value.startsWith("/media/") && !value.startsWith("#") && !value.startsWith("data:image/")) node.removeAttribute(attr.name);
        }
      }
      if (node.tagName === "A") { node.target = "_blank"; node.rel = "noopener noreferrer"; }
    });
    const style = document.createElement("style");
    const dark = document.body.dataset.theme === "dark";
    const normalize = text => text.replace(/:root/g, ":host").replace(/\bbody\b(?=\s*[.{:#,>\s])/g, ".anki-card-content");
    style.textContent = `
      :host { display:block; --red:#ff5a5a; --text-fg:${dark ? "#eee" : "#222"}; --text-bg:${dark ? "#292929" : "#fff"}; }
      .anki-card-content { text-align:center; color:var(--text-fg); background:var(--text-bg); overflow-wrap:anywhere; }
      ${normalize(css || "")}
      img,video { max-width:100%; height:auto; }
      .anki-remote-audio { display:inline-flex!important; align-items:center; justify-content:center; min-width:44px; min-height:44px; border:1px solid #888; border-radius:4px; background:#383838; color:white; cursor:pointer; font:18px Arial; }
      .audio-unavailable { font:12px Arial; opacity:.7; }
    `;
    template.content.querySelectorAll("style").forEach(n => { n.textContent = normalize(n.textContent); });
    const content = document.createElement("div");
    const cardClass = /^card\d+$/.test(state?.cardClass || "") ? state.cardClass : "card1";
    content.className = "anki-card-content card " + cardClass + (dark ? " nightMode night_mode" : "");
    content.id = "qa";
    content.append(template.content);
    root.replaceChildren(style, content);
    root.querySelectorAll(".anki-remote-audio").forEach(button => {
      button.addEventListener("click", () => playSequence([button.dataset.sound || ""]));
    });
  }

  function controls() {
    const ready = connected && !busy && state?.status !== "loading";
    ui.undo.disabled = !ready || !state?.canUndo;
    ui.deckButton.disabled = !ready;
    ui.replay.disabled = !ready || !state?.answerAudio?.length;
    ui.actions.querySelectorAll("button").forEach(b => { b.disabled = !ready; });
    ui.decks.querySelector("button").disabled = !ready;
  }

  function render(next, autoplay = false) {
    if (next.version !== "0.2.0-beta.1") {
      throw new Error("Restart Anki to finish the Remote Reviewer update, then reload this page.");
    }
    const changed = signature !== JSON.stringify([next.revision, next.status, next.cardId, next.showIntervals]);
    if (changed) stopAudio();
    signature = JSON.stringify([next.revision, next.status, next.cardId, next.showIntervals]);
    state = next; connected = true;
    ui.connection.textContent = next.status === "loading" ? "Loading card…" : "Connected";
    ui.connection.title = "Remote Reviewer " + (next.version || "older version");
    show(ui.retry, false); show(ui.login, false);
    show(ui.decks, next.status === "idle");
    const reviewing = ["question", "answer"].includes(next.status);
    show(ui.review, reviewing || next.status === "loading");
    // Polling should not replace focused buttons or an open deck selector.
    if (!changed) { controls(); return; }
    if (next.status === "idle") {
      ui.title.textContent = "Decks";
      let selected = ui.select.value;
      try { selected ||= localStorage.getItem("anki-remote-deck"); } catch {}
      ui.select.replaceChildren(...(next.decks || []).map(name => new Option(name, name)));
      if ([...ui.select.options].some(o => o.value === selected)) ui.select.value = selected;
    } else if (reviewing) {
      ui.title.textContent = next.deck;
      ui.status.textContent = next.status === "answer" ? "Answer" : "Question";
      ui.rwkv.textContent = next.status === "answer" && next.rwkv ? "RWKV R: " + next.rwkv : "";
      if (changed) renderCard(next.status === "answer" ? next.answerHtml : next.questionHtml, next.css);
      ui.actions.replaceChildren();
      const buttons = next.status === "question" ? [{ label: "Show answer", reveal: true }] : next.buttons;
      for (const item of buttons) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = item.reveal ? "reveal" : "answer-button ease-" + item.ease;
        button.textContent = item.label;
        if (next.showIntervals && item.interval) {
          const interval = document.createElement("small");
          interval.textContent = item.interval; button.append(interval);
        }
        button.addEventListener("click", () => perform(item.reveal ? "/api/reveal" : "/api/answer", { cardId: next.cardId, ease: item.ease }));
        ui.actions.append(button);
      }
      if ((autoplay || awaitingAnswer) && changed && next.status === "answer") {
        awaitingAnswer = false;
        if (next.autoPlayAudioOnAnswer) void playSequence(next.answerAudio || []);
      }
    }
    controls();
  }

  function failure(error) {
    stopAudio(); connected = false; controls();
    ui.connection.textContent = error.auth ? "Sign in required" : "Disconnected";
    if (error.auth) {
      show(ui.login, true); show(ui.review, false); show(ui.decks, false);
    } else show(ui.retry, true);
    message(error.name === "AbortError" ? "Anki took too long to respond. Reconnect to check the current card." : error.message);
  }

  async function refresh() {
    if (busy || polling || document.hidden) return;
    polling = true;
    const requestEpoch = epoch;
    try {
      const next = await api("/api/state");
      if (requestEpoch === epoch) { const wasConnected = connected; render(next); if (!wasConnected) message(""); }
    } catch (error) { if (requestEpoch === epoch) failure(error); }
    finally { polling = false; }
  }

  async function perform(path, body = {}) {
    if (busy || !connected) return;
    busy = true; epoch++; stopAudio(); controls(); message("");
    awaitingAnswer = path === "/api/reveal";
    try {
      const next = await api(path, { ...body, revision: state.revision });
      render(next, path === "/api/reveal");
    } catch (error) { failure(error); }
    finally { busy = false; controls(); }
  }

  $("login-form").addEventListener("submit", async event => {
    event.preventDefault(); if (busy) return;
    busy = true; epoch++;
    const button = event.currentTarget.querySelector("button"); button.disabled = true;
    try {
      await api("/api/login", { token: $("token").value.trim() });
      $("token").value = ""; render(await api("/api/state")); message("");
    } catch (error) { failure(error); }
    finally { busy = false; button.disabled = false; controls(); }
  });
  $("deck-form").addEventListener("submit", event => {
    event.preventDefault();
    try { localStorage.setItem("anki-remote-deck", ui.select.value); } catch {}
    perform("/api/start", { deck: ui.select.value });
  });
  ui.deckButton.addEventListener("click", () => perform("/api/stop"));
  ui.undo.addEventListener("click", () => perform("/api/undo"));
  ui.replay.addEventListener("click", () => playSequence(state?.answerAudio || []));
  ui.retry.addEventListener("click", refresh);
  ui.theme.addEventListener("click", () => {
    const theme = document.body.dataset.theme === "dark" ? "light" : "dark";
    document.body.dataset.theme = theme; ui.theme.textContent = theme === "dark" ? "Light" : "Dark";
    try { localStorage.setItem("anki-remote-theme", theme); } catch {}
    if (state && ["question", "answer"].includes(state.status)) renderCard(state.status === "answer" ? state.answerHtml : state.questionHtml, state.css);
  });
  document.addEventListener("keydown", event => {
    if (event.repeat || busy || !connected || event.ctrlKey || event.metaKey || event.altKey ||
        event.composedPath().some(n => n.matches?.("input,select,textarea,[contenteditable=true]"))) return;
    if (event.code === "Space" && state?.status === "question") { event.preventDefault(); perform("/api/reveal", { cardId: state.cardId }); }
    else if (/^[1-4]$/.test(event.key) && state?.status === "answer" && state.buttons.some(b => b.ease === Number(event.key))) perform("/api/answer", { cardId: state.cardId, ease: Number(event.key) });
    else if (event.key.toLowerCase() === "r" && state?.status === "answer") void playSequence(state.answerAudio || []);
  });
  try {
    document.body.dataset.theme = localStorage.getItem("anki-remote-theme") === "light" ? "light" : "dark";
    ui.theme.textContent = document.body.dataset.theme === "dark" ? "Light" : "Dark";
  } catch {}
  document.addEventListener("visibilitychange", () => { if (document.hidden) stopAudio(); else refresh(); });
  window.addEventListener("pagehide", stopAudio);
  void refresh();
  setInterval(() => { if (!ui.login.classList.contains("hidden")) return; void refresh(); }, 2000);
})();
