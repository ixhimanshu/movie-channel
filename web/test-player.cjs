// Run: node web/test-player.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const code = [...fs.readFileSync(__dirname + '/index.html', 'utf8').matchAll(/<script>([\s\S]*?)<\/script>/g)].at(-1)[1];

async function test(native = false) {
  const nodes = new Map(), players = [], requests = [];
  const counters = {pause: 0, load: 0};
  let revision = 1;
  function node(id) {
    if (!nodes.has(id)) nodes.set(id, {classList: {add() {}, remove() {}}, style: {},
      addEventListener() {}, pause() { counters.pause++; }, load() { counters.load++; },
      removeAttribute(name) { delete this[name]; }, play() { return Promise.resolve(); },
      canPlayType() { return native ? 'probably' : ''; }});
    return nodes.get(id);
  }
  class Hls {
    static isSupported() { return !native; }
    static Events = {MANIFEST_PARSED: 'manifest', ERROR: 'error'};
    static ErrorTypes = {MEDIA_ERROR: 'media'};
    constructor() { this.handlers = {}; players.push(this); }
    loadSource(source) { this.source = source; }
    attachMedia() {}
    on(event, fn) { this.handlers[event] = fn; }
    destroy() { this.destroyed = true; }
  }
  const context = vm.createContext({
    Hls, window: {Hls}, URLSearchParams, console,
    location: {search: '', port: '', origin: 'https://tv.example.com'},
    document: {getElementById: node, addEventListener() {}, body: node('body')},
    setTimeout: () => 1, clearTimeout() {}, setInterval() {},
    DOMParser: class { parseFromString() { return {querySelectorAll: () => []}; } },
    fetch: async url => {
      requests.push(url);
      return {ok: true, json: async () => ({revision}), text: async () => '<tv />'};
    }
  });
  vm.runInContext(code, context);
  // Let initialization finish, including the initial guide and stream.
  await new Promise(resolve => setImmediate(resolve));
  const oldPlayer = players[0];
  const initialGuideRequests = requests.filter(r => r.includes('xmltv')).length;
  await vm.runInContext('checkChannel()', context);
  assert.equal(counters.load, 0, 'unchanged revision must not restart playback');
  assert.equal(requests.filter(r => r.includes('xmltv')).length, initialGuideRequests);
  revision = 2;
  await vm.runInContext('checkChannel()', context);
  assert.equal(counters.pause, 1);
  assert.equal(counters.load, 1, 'changed revision must discard the old video buffer');
  assert(requests.includes('https://tv.example.com/iptv/xmltv.xml?revision=2'));
  if (native) {
    assert(node('video').src.endsWith('&revision=2'), 'native HLS must reconnect too');
  } else {
    assert(oldPlayer.destroyed);
    assert.equal(players.length, 2);
    assert(players[1].source.endsWith('&revision=2'));
    oldPlayer.handlers.error(null, {fatal: true, type: 'network'});
    assert(!players[1].destroyed, 'a stale player error must not destroy the new player');
  }
  revision = -1;
  await vm.runInContext('checkChannel()', context);
  assert.equal(counters.load, 1, 'invalid revisions must not reset playback');
}

(async () => {
  await test(false);
  await test(true);
  console.log('Passed: unchanged/changed revisions, buffer and guide refresh, stale errors, native HLS.');
})().catch(error => { console.error(error); process.exitCode = 1; });
