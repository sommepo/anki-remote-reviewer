const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
const functions = source.slice(source.indexOf('  function stopAudio()'), source.indexOf('  function renderCard('));

function fixture(block = false) {
  const calls = [], messages = [];
  const listeners = new Map();
  const player = {
    src: '', error: null,
    pause() {}, load() {}, removeAttribute() {},
    addEventListener(type, callback) { listeners.set(type, callback); },
    removeEventListener(type) { listeners.delete(type); },
    async play() {
      calls.push(this.src);
      if (block) throw Object.assign(new Error('blocked'), {name:'NotAllowedError'});
    },
  };
  const context = vm.createContext({player, ui:{replay:{}}, message:t=>messages.push(t)});
  vm.runInContext('let audioGeneration=0, finishAudio=null;\n' + functions, context);
  return { context, calls, messages, end:()=>listeners.get('ended')?.() };
}
const tick = () => new Promise(resolve => setImmediate(resolve));
test('answer audio plays sequentially and preserves repeated references', async () => {
  const f = fixture();
  const done = f.context.playSequence(['/media/word.mp3','/media/word.mp3','/media/sentence.opus']);
  await tick(); assert.equal(f.calls.length, 1);
  f.end(); await tick(); assert.equal(f.calls.length, 2);
  f.end(); await tick(); assert.equal(f.calls.length, 3);
  f.end(); await done;
});
test('changing card cancels remaining audio', async () => {
  const f = fixture();
  const done = f.context.playSequence(['/media/word.mp3','/media/sentence.opus']);
  await tick(); f.context.stopAudio(); await done;
  assert.equal(f.calls.length, 1);
});
test('autoplay policy rejection produces a replay instruction', async () => {
  const f = fixture(true);
  await f.context.playSequence(['/media/word.mp3']);
  assert.match(f.messages[0], /Tap Replay audio/);
});
test('manual playback interrupts a previous queue', async () => {
  const f = fixture();
  const first = f.context.playSequence(['/media/old.mp3','/media/unwanted.mp3']);
  await tick();
  const second = f.context.playSequence(['/media/new.mp3']);
  await tick(); f.end(); await Promise.all([first, second]);
  assert.deepEqual(f.calls, ['/media/old.mp3','/media/new.mp3']);
});
