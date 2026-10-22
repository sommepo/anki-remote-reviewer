// Minimal Chrome DevTools Protocol driver for the end-to-end tests.
// Needs Node 22+ (built-in WebSocket) and Chrome or Edge installed.
import { spawn } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";

export const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1";

export async function launch({ width = 390, height = 844, userAgent = IPHONE } = {}) {
  const executable = [
    process.env.CHROME,
    "C:/Program Files/Google/Chrome/Application/chrome.exe",
    "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/usr/bin/google-chrome", "/usr/bin/chromium",
  ].filter(Boolean).find(p => existsSync(p));
  if (!executable) throw new Error("Chrome or Edge was not found; set CHROME.");
  const profile = mkdtempSync(path.join(tmpdir(), "arr-e2e-"));
  // Keep the sandboxed card frame in-process so its DOM is reachable here.
  const chrome = spawn(executable, [
    "--headless=new", "--remote-debugging-port=0", `--user-data-dir=${profile}`, "--no-first-run",
    "--autoplay-policy=no-user-gesture-required",
    "--disable-features=IsolateOrigins,site-per-process,IsolateSandboxedIframes", "about:blank",
  ], { stdio: "ignore" });
  let port;
  for (let i = 0; i < 100 && !port; i++) {
    await sleep(100);
    try { port = readFileSync(path.join(profile, "DevToolsActivePort"), "utf8").split("\n")[0]; } catch {}
  }
  const target = (await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()).find(t => t.type === "page");
  const socket = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise(resolve => socket.addEventListener("open", resolve, { once: true }));

  let nextId = 0;
  const waiting = new Map();
  const contexts = new Map();   // frameId -> main-world execution context
  const logs = [];
  socket.addEventListener("message", ({ data }) => {
    const message = JSON.parse(data);
    if (message.id && waiting.has(message.id)) {
      const { resolve, reject } = waiting.get(message.id);
      waiting.delete(message.id);
      message.error ? reject(new Error(message.error.message)) : resolve(message.result);
    } else if (message.method === "Runtime.executionContextCreated") {
      const context = message.params.context;
      if (context.auxData?.isDefault) contexts.set(context.auxData.frameId, context.id);
    } else if (message.method === "Runtime.consoleAPICalled") {
      logs.push(message.params.type + ": " + message.params.args.map(a => a.value ?? a.description).join(" ").slice(0, 300));
    } else if (message.method === "Runtime.exceptionThrown") {
      logs.push("exception: " + (message.params.exceptionDetails.exception?.description || message.params.exceptionDetails.text).slice(0, 300));
    }
  });
  const send = (method, params = {}) => new Promise((resolve, reject) => {
    const id = ++nextId;
    waiting.set(id, { resolve, reject });
    socket.send(JSON.stringify({ id, method, params }));
  });

  const page = {
    send, logs, contexts,
    async evaluate(expression, frameId) {
      const params = { expression, awaitPromise: true, returnByValue: true };
      if (frameId) params.contextId = contexts.get(frameId);
      const result = await send("Runtime.evaluate", params);
      if (result.exceptionDetails) throw new Error(result.exceptionDetails.exception?.description || "evaluation failed");
      return result.result.value;
    },
    async until(description, check, timeout = 8000) {
      const end = Date.now() + timeout;
      for (;;) {
        try { const value = await check(); if (value) return value; } catch {}
        if (Date.now() > end) throw new Error("Timed out waiting for " + description);
        await sleep(100);
      }
    },
    async cardFrame() {
      const id = (await send("Page.getFrameTree")).frameTree.childFrames?.[0]?.frame.id;
      return id && contexts.has(id) ? id : null;
    },
    async shot(directory, name) {
      if (!directory) return;
      // Headless Chrome may not repaint an in-process frame after hover-only
      // changes; nudge both documents so the capture shows the current state.
      // (Hidden frames never run animation frames, hence the timeout.)
      const frames = "Promise.race([new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r))), new Promise(r => setTimeout(r, 250))])";
      await page.evaluate(`document.body.style.opacity = document.body.style.opacity === "0.999" ? "" : "0.999"; ${frames}`);
      const card = await page.cardFrame().catch(() => null);
      if (card) await page.evaluate(frames, card);
      const { data } = await send("Page.captureScreenshot", { format: "png" });
      writeFileSync(path.join(directory, name + ".png"), Buffer.from(data, "base64"));
    },
    // A real pointer press, so the card frame sees the pointer leave like on a device.
    async tap(selector, frameId) {
      const box = `(() => { const r = document.querySelector(${JSON.stringify(selector)}).getBoundingClientRect(); return [r.x + r.width / 2, r.y + r.height / 2]; })()`;
      let [x, y] = await page.evaluate(box, frameId);
      if (frameId) {
        const [fx, fy] = await page.evaluate(`(() => { const r = document.getElementById("card").getBoundingClientRect(); return [r.x, r.y]; })()`);
        x += fx; y += fy;
      }
      await send("Input.dispatchMouseEvent", { type: "mouseMoved", x, y });
      await send("Input.dispatchMouseEvent", { type: "mousePressed", x, y, button: "left", clickCount: 1 });
      await send("Input.dispatchMouseEvent", { type: "mouseReleased", x, y, button: "left", clickCount: 1 });
    },
    async hover(selector, frameId) {
      const [x, y] = await page.evaluate(`(() => { const r = document.querySelector(${JSON.stringify(selector)}).getBoundingClientRect(); return [r.x + r.width / 2, r.y + r.height / 2]; })()`, frameId);
      const [fx, fy] = await page.evaluate(`(() => { const r = document.getElementById("card").getBoundingClientRect(); return [r.x, r.y]; })()`);
      await send("Input.dispatchMouseEvent", { type: "mouseMoved", x: x + fx, y: y + fy });
    },
    async key(key, code, text) {
      await send("Input.dispatchKeyEvent", { type: "keyDown", key, code, text, windowsVirtualKeyCode: keyCode(key) });
      await send("Input.dispatchKeyEvent", { type: "keyUp", key, code, windowsVirtualKeyCode: keyCode(key) });
    },
    async close() {
      socket.close();
      chrome.kill();
      await sleep(300);
      try { rmSync(profile, { recursive: true, force: true }); } catch {}
    },
  };

  await send("Runtime.enable");
  await send("Page.enable");
  await send("Emulation.setDeviceMetricsOverride", { width, height, deviceScaleFactor: 2, mobile: true });
  await send("Emulation.setUserAgentOverride", { userAgent });
  // Record transient notices so tests can assert none were errors.
  await send("Page.addScriptToEvaluateOnNewDocument", { source: `window.__notices = [];
    document.addEventListener("DOMContentLoaded", () => {
      const node = document.getElementById("notice-text");
      if (node) new MutationObserver(() => { if (node.textContent) window.__notices.push(node.textContent); })
        .observe(node, { childList: true, characterData: true, subtree: true });
    });` });
  return page;
}

function keyCode(key) {
  return { ArrowLeft: 37, ArrowRight: 39, " ": 32, Enter: 13 }[key] ?? key.toUpperCase().charCodeAt(0);
}

export async function signIn(page, base, token) {
  await page.send("Page.navigate", { url: base + "/" });
  await page.until("login form", () => page.evaluate(`!document.getElementById("login").hidden`));
  await page.evaluate(`document.getElementById("token").value = ${JSON.stringify(token)}; document.getElementById("login-form").requestSubmit()`);
  await page.until("signed in", () => page.evaluate(`document.getElementById("login").hidden && document.getElementById("connection").textContent === "Connected"`));
  if (await page.evaluate(`document.getElementById("decks").hidden`)) await page.tap("#decks-button");
  await page.until("deck list", () => page.evaluate(`document.querySelectorAll(".deck-open").length`));
}
