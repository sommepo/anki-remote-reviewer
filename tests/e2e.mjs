// End-to-end check of the web app in real headless Chrome/Edge (phone-sized),
// against the fake Anki host. Start a fresh dev server first:
//   python tests/dev_server.py 28610
//   node tests/e2e.mjs http://127.0.0.1:28610 [screenshot-dir]
import assert from "node:assert/strict";
import { launch, signIn } from "./cdp.mjs";

const base = process.argv[2] || "http://127.0.0.1:28610";
const shots = process.argv[3];
const page = await launch();
const { evaluate, until } = page;
const passed = [];
try {
  await signIn(page, base, "dev-token");
  passed.push("sign in and deck list");
  await page.shot(shots, "1-decks");

  // Start and immediately reveal: the fake reviewer ignores input for the
  // first 0.6 s of a session, like the RWKV fork.
  await evaluate(`[...document.querySelectorAll(".deck-open")].find(b => b.textContent.includes("Mining")).click()`);
  await until("question buttons", () => evaluate(`document.querySelector("#actions .reveal")?.disabled === false`));
  await page.tap("#actions .reveal");
  await until("answer buttons after first-card block", () => evaluate(`document.querySelectorAll("#actions .ease-3").length`), 15000);
  passed.push("first reveal of a session waits for desktop readiness");

  const frameId = await until("card frame", () => page.cardFrame());
  await until("answer text in frame", () => evaluate(`document.getElementById("qa").textContent.includes("room")`, frameId));
  const info = await evaluate(`({
    targets: document.querySelectorAll(".kanjiHoverTarget").length,
    nativeReviewer: typeof window._updateQA === "function",
    storageError: window.storageError || null,
    bodyClass: document.body.className, htmlClass: document.documentElement.className,
    origin: (() => { try { return window.parent.document && "same"; } catch { return "isolated"; } })(),
  })`, frameId);
  assert.ok(info.targets >= 2, "template script wrapped kanji: " + JSON.stringify(info));
  assert.equal(info.storageError, null);
  assert.equal(info.origin, "isolated", "card frame must not reach the app");
  assert.match(info.bodyClass, /card card1/);
  assert.match(info.htmlClass, /mobile/);
  passed.push(`template <script src> ran (native reviewer.js: ${info.nativeReviewer}, html: ${info.htmlClass})`);

  await page.hover(".kanjiHoverTarget", frameId);
  const tooltip = await until("hover tooltip", () => evaluate(`[...document.querySelectorAll(".hoverDiv")].find(t => t.style.display === "block")?.textContent`, frameId));
  assert.match(tooltip, /Kanji: /);
  passed.push("hovering a kanji shows the template's tooltip: " + tooltip);
  await page.shot(shots, "2-answer-hover");

  // Answer Good; next card has a type-in field.
  await page.tap("#actions .ease-3");
  await until("second card", () => evaluate(`document.getElementById("qa").textContent.includes("窓")`, frameId));
  assert.equal(await evaluate(`[...document.querySelectorAll(".hoverDiv")].some(t => t.style.display === "block")`, frameId), false,
    "tapping the app hides the previous card's hover tooltip");
  const runs = await evaluate(`[window.hoverRuns, localStorage.getItem("hoverRuns")]`, frameId);
  assert.ok(runs[0] >= 2, "frame globals persist between cards like desktop: " + runs);
  passed.push("frame globals and localStorage persist across cards: " + runs);
  await evaluate(`document.getElementById("typeans").value = "まど"`, frameId);
  await until("reveal enabled", () => evaluate(`document.querySelector("#actions .reveal")?.disabled === false`));
  await page.tap("#actions .reveal");
  await until("typed answer echoed", () => evaluate(`document.getElementById("typed")?.textContent === "まど"`, frameId));
  passed.push("type-in answer reaches desktop Anki");

  // Keys the app ignores reach the card; answer keys work from either document.
  await evaluate(`window.seenKeys = []; document.addEventListener("keydown", e => seenKeys.push(e.key))`, frameId);
  await page.key("ArrowRight", "ArrowRight");
  await until("unused key forwarded to card", () => evaluate(`seenKeys.includes("ArrowRight")`, frameId));
  passed.push("unused keys are forwarded to card scripts");
  await evaluate(`document.body.focus()`, frameId);
  await page.key("1", "Digit1", "1");
  await until("third card after key 1", () => evaluate(`document.getElementById("qa").textContent.includes("plain card")`, frameId));
  passed.push("answer shortcut");
  await until("MathJax typeset", () => evaluate(`!!document.querySelector("#qa mjx-container")`, frameId), 15000);
  passed.push("MathJax from Anki's install typesets card math");

  await page.tap("#more");
  await page.tap('[data-flag="1"]');
  await until("flag drawn", () => evaluate(`!document.getElementById("_flag").hidden`, frameId));
  passed.push("flag card from the menu");
  // Card scripts can post fake key events; they must not reach undo/bury/suspend.
  await evaluate(`for (const key of ["@", "-", "*"]) parent.postMessage({ type: "key", key }, "*");
    parent.postMessage({ type: "key", key: "z", ctrlKey: true }, "*")`, frameId);
  await new Promise(resolve => setTimeout(resolve, 800));
  assert.ok(await evaluate(`document.getElementById("qa").textContent.includes("plain card")`, frameId), "forged keys were ignored");
  assert.equal(await evaluate(`document.querySelector('[data-action="mark"]').textContent.startsWith("Mark")`), true);
  passed.push("forged key events from card scripts cannot undo, bury, suspend or mark");
  await page.shot(shots, "3-question-flagged");

  await page.tap("#undo");
  await until("undo returns to previous card", () => evaluate(`document.getElementById("qa").textContent.includes("窓")`, frameId));
  passed.push("undo");

  await page.tap("#decks-button");
  await until("back to deck list", () => evaluate(`!document.getElementById("decks").hidden`));
  passed.push("back to decks");
  assert.deepEqual(await evaluate(`window.__notices`), [], "no error notices (audio played)");
  console.log("PASS\n- " + passed.join("\n- "));
} catch (error) {
  console.error("FAIL after:\n- " + passed.join("\n- ") + "\n" + error.stack + "\nconsole:\n" + page.logs.join("\n"));
  process.exitCode = 1;
} finally {
  await page.close();
}
