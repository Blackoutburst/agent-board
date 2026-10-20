/* Pure validation and immutable owner-scoped outbox tests. */
const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname, '../web/workspace.js'), 'utf8');
const storage = new Map(); let serial = 0;
const context = vm.createContext({ TextEncoder, Date, serverInteger: (v, min = 0) => Number.isSafeInteger(v) && v >= min,
  serverNeedsReload: s => ['conflict', 'refused'].includes(s), idempotencyKey: () => 'key-' + (++serial),
  state: { actor: { owner_id: 'owner-a' } },
  sessionStorage: { getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value) } });
const start = source.indexOf('  const provisionSchemas ='), end = source.indexOf('  async function sendProvisionRecord', start);
assert.ok(start > 0 && end > start);
vm.runInContext(source.slice(start, end) + '\nthis.api={provisionBodyValid,provisionGrantValid,provisionViewValid,provisionListValid,provisionContextValid,provisionStore,provisionRecord,updateProvisionRecord,provisionText,provisionApi};', context);
const api = context.api, hash = 'a'.repeat(64), owner = 'owner-a', host = { id: 'host', device_id: 'device' };
const body = { schema: 'server-provision-draft-request-v1', expected_host_version: 1, expected_catalog_version: 1,
  expected_instances_sha256: hash, grant_version: 1, profile_id: 'profile', profile_version: 1, profile_sha256: hash,
  provider_id: 'codex_chatgpt', requested_model: null };
const grant = { schema: 'server-provision-draft-grant-view-v1', owner_id: owner, host_id: host.id, device_id: host.device_id,
  purpose: 'metadata_request_only_v1', version: 0, enabled: false };
const value = { schema: 'server-provision-draft-view-v1', request_id: 'request', request_sha256: hash, owner_id: owner,
  host_id: host.id, device_id: host.device_id, revision: 1, state: 'requested', reason: 'requested', barrier_held: true,
  reserved_slots: 1, profile_id: body.profile_id, profile_version: 1, provider_id: body.provider_id, requested_model: null,
  apply_implemented: false, connection_status: 'not_attested', model_catalog_status: 'not_attested', model_access_status: 'not_attested', can_cancel: true };
let checks = 0;
const ok = v => { assert.equal(v, true); checks++; }, no = v => { assert.equal(v, false); checks++; };
ok(api.provisionBodyValid('request', body));
for (const patch of [{ token: 'sentinel' }, { grant_version: true }, { expected_host_version: 1.5 }, { profile_version: 0 },
  { expected_catalog_version: Number.MAX_SAFE_INTEGER + 1 }, { profile_sha256: 'A'.repeat(64) }, { provider_id: 'api-fallback' },
  { requested_model: '' }, { requested_model: '\uD800' }, { requested_model: 'x\ncommand' }, { requested_model: 'x'.repeat(201) }]) no(api.provisionBodyValid('request', { ...body, ...patch }));
ok(api.provisionBodyValid('request', { ...body, requested_model: '😀'.repeat(200) }));
no(api.provisionBodyValid('request', { ...body, requested_model: '😀'.repeat(201) }));
ok(api.provisionGrantValid(grant, owner, host));
for (const patch of [{ enabled: true }, { owner_id: 'owner-b' }, { device_id: 'other' }, { purpose: 'provision_and_login' }, { unknown: false }]) no(api.provisionGrantValid({ ...grant, ...patch }, owner, host));
ok(api.provisionViewValid(value, owner, host));
for (const patch of [{ state: 'ready' }, { state: 'prepared' }, { apply_implemented: true }, { connection_status: 'connected' },
  { model_catalog_status: 'confirmed' }, { model_access_status: 'confirmed' }, { barrier_held: false }, { reserved_slots: 0 },
  { owner_id: 'owner-b' }, { device_id: 'other' }, { secret: 'sentinel' }, { revision: true }]) no(api.provisionViewValid({ ...value, ...patch }, owner, host));
const cancelled = { ...value, revision: 2, state: 'cancelled', reason: 'cancelled_by_owner', barrier_held: false, reserved_slots: 0, can_cancel: false };
ok(api.provisionViewValid(cancelled, owner, host)); no(api.provisionViewValid({ ...cancelled, can_cancel: true }, owner, host));
const page = { schema: 'server-provision-draft-list-v1', requests: [value], next_before: null };
ok(api.provisionListValid(page, owner, host));
for (const patch of [{ requests: [value, value] }, { requests: Array(17).fill(cancelled) }, { next_before: 'not-last-id' }, { secret: 'sentinel' }]) no(api.provisionListValid({ ...page, ...patch }, owner, host));
const p = { id: 'profile', version: 1, preferred_provider: 'codex_chatgpt', preferred_model: '' };
const read = { ...body, schema: 'server-provision-draft-context-v1', owner_id: owner, host_id: host.id, device_id: host.device_id };
ok(api.provisionContextValid(read, owner, host, p));
for (const patch of [{ profile_sha256: null }, { owner_id: 'owner-b' }, { profile_version: 2 }, { requested_model: 'unexpected' }, { instruction: 'sentinel' }]) no(api.provisionContextValid({ ...read, ...patch }, owner, host, p));
const original = api.provisionRecord(owner, host, 'request', body);
api.updateProvisionRecord(owner, original.key, 'uncertain');
const replay = api.provisionRecord(owner, host, 'request', body);
assert.equal(replay.key, original.key); assert.deepEqual(JSON.parse(JSON.stringify(replay.body)), body); checks++;
assert.throws(() => api.provisionRecord(owner, host, 'request', { ...body, requested_model: 'changed' })); checks++;
context.state.actor.owner_id = 'owner-b';
assert.throws(() => api.provisionRecord(owner, host, 'request', body)); checks++;
const other = api.provisionRecord('owner-b', host, 'request', body); assert.notEqual(other.key, original.key); checks++;
context.state.actor = null; assert.throws(() => api.provisionRecord('owner-b', host, 'request', body)); checks++;
context.state.actor = { owner_id: owner };
api.updateProvisionRecord(owner, replay.key, 'refused'); assert.throws(() => api.provisionRecord(owner, host, 'request', body)); checks++;
api.updateProvisionRecord(owner, replay.key, 'reloaded'); const corrected = api.provisionRecord(owner, host, 'request', body); assert.notEqual(corrected.key, replay.key); checks++;
const storeKey = 'board-provision-drafts-v1:' + owner, raw = storage.get(storeKey);
for (const mutate of [d => { d.owner = 'owner-b'; }, d => { d.records[0].body.token = 'sentinel'; }, d => { d.records[0].kind = 'apply'; },
  d => { d.records[0].status = 'complete'; }, d => { d.records.push(d.records[0]); }, d => { d.secret = 'sentinel'; }]) {
  const changed = JSON.parse(raw); mutate(changed); storage.set(storeKey, JSON.stringify(changed)); assert.throws(() => api.provisionStore(owner)); checks++;
}
storage.set(storeKey, 'x'.repeat(65537)); assert.throws(() => api.provisionStore(owner)); checks++;
(async () => {
  let deadline, fetches = 0, cancelled = 0, logouts = 0, reads = 0;
  Object.assign(context, { TextDecoder, AbortController, basePath: '/', showLogin: () => { logouts++; },
    setTimeout: (fn, delay) => { assert.equal(delay, 15000); deadline = fn; return 1; }, clearTimeout: () => {} });
  const response = bytes => ({ ok: true, status: 200, body: { getReader: () => ({
    read: async () => { reads++; return reads === 1 ? { value: bytes, done: false } : { done: true }; },
    cancel: async () => { cancelled++; } }) } });
  context.fetch = async (url, options) => { fetches++; assert.equal(url, '/api/server-hosts/host/provisioning-grant');
    assert.equal(options.credentials, 'same-origin'); assert.equal(options.cache, 'no-store');
    return response(new TextEncoder().encode('{"value":1}')); };
  const parsed = await api.provisionApi('/server-hosts/host/provisioning-grant'); assert.equal(parsed.value, 1); checks++;
  reads = 0; context.fetch = async () => { fetches++; return response(new Uint8Array(16385)); };
  await assert.rejects(api.provisionApi('/server-hosts/host/provisioning-grant'), /indisponible/); assert.equal(reads, 1); assert.equal(cancelled, 1); checks++;
  const beforeBody = fetches;
  await assert.rejects(api.provisionApi('/server-hosts/host/provisioning-grant', { method: 'PUT', data: { value: 'x'.repeat(8192) } }), /trop longue/);
  assert.equal(fetches, beforeBody); checks++;
  context.fetch = async () => { fetches++; return { ok: false, status: 401, get body() { throw new Error('PRIVATE_BODY_MUST_NOT_BE_READ'); } }; };
  await assert.rejects(api.provisionApi('/server-hosts/host/provisioning-grant'), /Préparation indisponible/); assert.equal(logouts, 1); checks++;
  context.fetch = (url, options) => { fetches++; return new Promise((resolve, reject) => options.signal.addEventListener('abort', () => reject(new Error('synthetic timeout')), { once: true })); };
  const beforeTimeout = fetches, timed = api.provisionApi('/server-hosts/host/provisioning-grant'); deadline();
  await assert.rejects(timed, /synthetic timeout/); assert.equal(fetches, beforeTimeout + 1, 'timeout must never retry'); checks++;
  console.log(JSON.stringify({ result: 'passed_pure_ui_validation', checks, synthetic_fetches: fetches, network_calls: 0, provider_calls: 0, model_calls: 0 }));
})().catch(error => { console.error(error); process.exitCode = 1; });
