const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
const audio = source.slice(source.indexOf('  // ----- audio -----'), source.indexOf('  // ----- end audio -----'));

function fixture(block = false) {
  const calls = [], notices = [], spoken = [];
  const listeners = new Map();
  const player = {
    src: '', error: null, paused: true,
    pause() {}, load() {},
    addEventListener(type, callback) { listeners.set(type, callback); },
    removeEventListener(type) { listeners.delete(type); },
    async play() {
      calls.push(this.src);
      if (block) throw Object.assign(new Error('blocked'), { name: 'NotAllowedError' });
    },
  };
  const speechSynthesis = { cancel() {}, speak(u) { spoken.push([u.text, u.lang, u.rate]); setImmediate(() => u.onend()); } };
  const context = vm.createContext({
    Audio: function () { return player; }, frame: { token: 'tok' }, notice: text => notices.push(text),
    speechSynthesis, SpeechSynthesisUtterance: function (text) { this.text = text; }, window: { speechSynthesis },
    encodeURIComponent,
  });
  context.window = context;
  vm.runInContext(audio + '\nthis.api = { playEntries, stopAudio, mediaUrl };', context);
  return { api: context.api, calls, notices, spoken, end: () => listeners.get('ended')?.() };
}
const tick = () => new Promise(resolve => setImmediate(resolve));
const files = (...names) => names.map(file => ({ file }));

test('media URLs go through the card frame token', () => {
  assert.equal(fixture().api.mediaUrl('雨 1.mp3'), '/c/tok/%E9%9B%A8%201.mp3');
});
test('audio plays sequentially and preserves repeated references', async () => {
  const f = fixture();
  const done = f.api.playEntries(files('word.mp3', 'word.mp3', 'sentence.opus'));
  await tick(); assert.equal(f.calls.length, 1);
  f.end(); await tick(); assert.equal(f.calls.length, 2);
  f.end(); await tick(); assert.equal(f.calls.length, 3);
  f.end(); await done;
  assert.deepEqual(f.notices, []);
});
test('TTS tags are spoken in place with the right language', async () => {
  const f = fixture();
  const done = f.api.playEntries([{ tts: { text: 'まど', lang: 'ja_JP', speed: 1.5 } }, ...files('after.mp3')]);
  await tick(); await tick();
  assert.deepEqual(f.spoken, [['まど', 'ja-JP', 1.5]]);
  assert.deepEqual(f.calls, ['/c/tok/after.mp3']);
  f.end(); await done;
});
test('changing card cancels remaining audio', async () => {
  const f = fixture();
  const done = f.api.playEntries(files('word.mp3', 'sentence.opus'));
  await tick(); f.api.stopAudio(); await done;
  assert.equal(f.calls.length, 1);
});
test('autoplay policy rejection produces a replay instruction', async () => {
  const f = fixture(true);
  await f.api.playEntries(files('word.mp3'));
  assert.match(f.notices[0], /Replay audio/);
});
test('manual playback interrupts a previous queue', async () => {
  const f = fixture();
  const first = f.api.playEntries(files('old.mp3', 'unwanted.mp3'));
  await tick();
  const second = f.api.playEntries(files('new.mp3'));
  await tick(); f.end(); await Promise.all([first, second]);
  assert.deepEqual(f.calls, ['/c/tok/old.mp3', '/c/tok/new.mp3']);
});
