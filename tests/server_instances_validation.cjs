/* Pure tests of the shipped UI block. No browser, network or process. */
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm'), assert = require('node:assert/strict');
const source = fs.readFileSync(path.join(__dirname, '../web/workspace.js'), 'utf8');
const begin = source.indexOf('  const serverStates ='), end = source.indexOf('  function serverActionDialog(', begin);
assert.ok(begin > 0 && end > begin);
let sequence = 0;
function context(storage = new Map()) {
  const calls = [];
  const scope = { TextEncoder, Date, Number, JSON, Set, Map, Object, Array, Error,
    state: { actor: { owner_id: 'owner' }, view: 'other' }, document: { querySelector: () => null }, CSS: { escape: x => x },
    $: () => ({ open: false }), notify: () => {}, workPath: encodeURIComponent,
    idempotencyKey: () => `key-${++sequence}`,
    sessionStorage: { getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value) },
    api: async (route, args) => { calls.push(JSON.parse(JSON.stringify({ route, args }))); throw new Error('synthetic response loss'); } };
  vm.createContext(scope);
  vm.runInContext(source.slice(begin, end) + '\nglobalThis.exposed = {serverHostName,serverRecord,serverStore,saveServerStore,sendServerRecord};', scope);
  return { scope, api: scope.exposed, calls, storage };
}
const command = { action: 'start', expected_version: 1, expected_generation: 0, launchspec_sha256: 'a'.repeat(64), budget: 2 };
(async () => {
  const valid = context();
  for (const name of ['', ' ', 'x'.repeat(101), 'a\u0000b', 'a\nb']) assert.equal(valid.api.serverHostName(name), false);
  for (const name of ['Mon serveur', 'é'.repeat(100), 'x'.repeat(100)]) assert.equal(valid.api.serverHostName(name), true);
  let refusals = 0;
  for (const kind of ['host', 'command']) for (const status of [400, 404, 422]) {
    const first = context(), target = kind === 'host' ? 'device' : 'instance';
    // Legacy names remain immutable and readable, so old losses can be resolved.
    const body = kind === 'host' ? { device_id: target, name: 'x'.repeat(101) } : command;
    const record = first.api.serverRecord('owner', kind, target, body), original = JSON.stringify(record.body);
    first.scope.api = async (route, args) => { first.calls.push(JSON.parse(JSON.stringify({ route, args }))); const error = new Error('terminal refusal'); error.status = status; throw error; };
    await first.api.sendServerRecord('owner', record);
    assert.equal(first.api.serverStore('owner').records[0].status, 'refused');
    assert.equal(first.calls.length, 1);
    const second = context(first.storage), saved = second.api.serverStore('owner').records[0];
    await assert.rejects(() => second.api.sendServerRecord('owner', saved), /Actualisez/);
    assert.equal(second.calls.length, 0);
    const corrected = kind === 'host' ? { device_id: target, name: 'corrigé' } : { ...command, budget: 3 };
    assert.throws(() => second.api.serverRecord('owner', kind, target, corrected), /reste à confirmer/);
    // Model only the explicit successful fresh-render transition; Firefox
    // exercises the real reload path rather than granting that step here.
    const data = second.api.serverStore('owner'); data.records[0].status = 'reloaded'; second.api.saveServerStore('owner', data);
    const replacement = second.api.serverRecord('owner', kind, target, corrected);
    assert.notEqual(replacement.key, record.key); assert.equal(JSON.stringify(record.body), original);
    refusals++;
  }
  const first = context(), original = first.api.serverRecord('owner', 'command', 'instance', command);
  await first.api.sendServerRecord('owner', original);
  const second = context(first.storage), retry = second.api.serverStore('owner').records[0];
  assert.equal(retry.status, 'uncertain'); await second.api.sendServerRecord('owner', retry);
  assert.deepEqual(second.calls[0], first.calls[0]);
  console.log(JSON.stringify({ name_boundaries: 8, terminal_refusal_cases: refusals, lost_response_exact_replay: true, network_requests: 0 }));
})().catch(error => { console.error(error); process.exitCode = 1; });
