// Runs inside the sandboxed card document, before Anki's reviewer.js.
// Bridges Anki's pycmd/bridgeCommand calls, keys and storage to the app.
(() => {
  "use strict";
  const app = window.parent;
  const post = message => app.postMessage(message, "*");

  // Opaque-origin documents cannot use web storage. Templates get working
  // stand-ins: localStorage persists through the app, sessionStorage lasts
  // as long as this frame (like a desktop review session).
  class MemoryStorage {
    constructor(onChange) { this._items = new Map(); this._onChange = onChange; }
    get length() { return this._items.size; }
    key(index) { return [...this._items.keys()][index] ?? null; }
    getItem(key) { key = String(key); return this._items.has(key) ? this._items.get(key) : null; }
    setItem(key, value) { this._items.set(String(key), String(value)); this._onChange?.(String(key), String(value)); }
    removeItem(key) { this._items.delete(String(key)); this._onChange?.(String(key), null); }
    clear() { for (const key of [...this._items.keys()]) this.removeItem(key); }
    _load(items) { for (const [key, value] of Object.entries(items || {})) this._items.set(key, String(value)); }
  }
  const local = new MemoryStorage((key, value) => post({ type: "storage", key, value }));
  const session = new MemoryStorage();
  for (const [name, storage] of [["localStorage", local], ["sessionStorage", session]]) {
    try { Object.defineProperty(window, name, { configurable: true, get: () => storage }); } catch {}
  }

  window.bridgeCommand = (command) => { post({ type: "cmd", cmd: String(command) }); };
  window.pycmd = window.bridgeCommand;

  // Minimal stand-in for Anki's reviewer.js when it cannot be loaded.
  function fallbackApi() {
    window.onUpdateHook ||= [];
    window.onShownHook ||= [];
    let queue = Promise.resolve();
    const runScripts = async root => {
      for (const old of [...root.querySelectorAll("script")]) {
        await new Promise(resolve => {
          const script = document.createElement("script");
          for (const attribute of old.attributes) script.setAttribute(attribute.name, attribute.value);
          script.textContent = old.textContent;
          if (old.src) script.onload = script.onerror = () => resolve();
          old.replaceWith(script);
          if (!old.src) resolve();
        });
      }
    };
    const update = (html, bodyClass, scrollTop) => {
      queue = queue.then(async () => {
        const qa = document.getElementById("qa");
        window.onUpdateHook.length = 0;
        window.onShownHook.length = 0;
        qa.innerHTML = html;
        await runScripts(qa);
        if (bodyClass) document.body.className = bodyClass;
        if (scrollTop) window.scrollTo(0, 0);
        for (const hook of [...window.onUpdateHook, ...window.onShownHook]) { try { await hook(); } catch (e) { console.log(e); } }
        if (!scrollTop) document.getElementById("answer")?.scrollIntoView();
      });
    };
    window._showQuestion = (q, a, bodyClass) => update(q, bodyClass, true);
    window._showAnswer = (a, bodyClass) => update(a, bodyClass, false);
    window._drawFlag = flag => {
      const node = document.getElementById("_flag");
      node.hidden = flag === 0;
      node.style.color = `var(--flag-${flag})`;
    };
    window._drawMark = marked => { document.getElementById("_mark").hidden = !marked; };
    window.getTypedAnswer = () => document.getElementById("typeans")?.value ?? null;
  }

  // On desktop the mouse leaves the card to reach the answer buttons. Taps on
  // the app never reach this document, so emulate that for hover-driven templates.
  function pointerLeft() {
    const hovered = [...document.querySelectorAll(":hover")].reverse();
    if (!hovered.length) return;
    hovered[0].dispatchEvent(new MouseEvent("mouseout", { bubbles: true, relatedTarget: null }));
    hovered[0].dispatchEvent(new PointerEvent("pointerleave", { bubbles: false }));
    for (const node of hovered) node.dispatchEvent(new MouseEvent("mouseleave", { bubbles: false, relatedTarget: null }));
  }

  function show(message) {
    pointerLeft();
    document.documentElement.classList.toggle("night-mode", !!message.dark);
    if (message.side === "question") window._showQuestion(message.html, "", message.bodyClass);
    else window._showAnswer(message.html, message.bodyClass);
    marks(message);
  }

  function marks(message) {
    try { window._drawFlag(message.flag || 0); window._drawMark(!!message.marked); } catch {}
  }

  function theme(message) {
    document.documentElement.classList.toggle("night-mode", !!message.dark);
    document.body.classList.toggle("nightMode", !!message.dark);
    document.body.classList.toggle("night_mode", !!message.dark);
  }

  window.addEventListener("message", event => {
    if (event.source !== app || !event.data) return;
    const message = event.data;
    if (message.type === "init") { local._load(message.storage); theme(message); }
    else if (message.type === "show") show(message);
    else if (message.type === "marks") marks(message);
    else if (message.type === "theme") theme(message);
    else if (message.type === "leave") pointerLeft();
    else if (message.type === "clear") {
      pointerLeft();
      document.querySelectorAll("audio, video").forEach(media => media.pause());
      document.getElementById("qa").replaceChildren();
    }
    else if (message.type === "key") replayKey(message);
    else if (message.type === "typed") {
      let value = null;
      try { value = window.getTypedAnswer(); } catch {}
      post({ type: "typed", id: message.id, value });
    }
  });

  // Keys the app does not use are replayed here, as on desktop where the card
  // always has keyboard focus (e.g. Lapis's arrow keys and "C" toggle).
  function replayKey(message) {
    const target = document.activeElement && document.activeElement !== document.documentElement ? document.activeElement : document.body;
    const init = { key: message.key, code: message.code, ctrlKey: message.ctrlKey, metaKey: message.metaKey,
                   altKey: message.altKey, shiftKey: message.shiftKey, repeat: message.repeat, bubbles: true, cancelable: true };
    target.dispatchEvent(new KeyboardEvent(message.phase || "keydown", init));
  }

  // Keys go to the app so shortcuts work while the card has focus.
  document.addEventListener("keydown", event => {
    if (!event.isTrusted || event.defaultPrevented || event.isComposing) return;
    const target = event.target;
    if (target?.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(target?.tagName || "")) return;
    if (event.key === " ") event.preventDefault();
    post({ type: "key", key: event.key, code: event.code, ctrlKey: event.ctrlKey, metaKey: event.metaKey,
           altKey: event.altKey, shiftKey: event.shiftKey, repeat: event.repeat });
  });

  // Desktop Anki opens web links in the system browser and never lets the card
  // page navigate away; a reload here would lose the card and its state.
  document.addEventListener("click", event => {
    const link = event.target?.closest?.("a[href]");
    if (!link || event.defaultPrevented) return;
    const href = link.getAttribute("href").trim();
    if (href.startsWith("#") || /^javascript:/i.test(href)) return;
    event.preventDefault();
    if (/^(https?:|mailto:)/i.test(href)) window.open(link.href, "_blank", "noopener");
  });
  document.addEventListener("submit", event => event.preventDefault());

  document.addEventListener("DOMContentLoaded", () => {
    const native = typeof window._showQuestion === "function";
    if (!native) fallbackApi();
    post({ type: "ready", native });
  });
})();
