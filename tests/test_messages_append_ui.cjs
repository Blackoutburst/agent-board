'use strict';
/* Shipped validators/outbox in a pure VM; no DOM, filesystem journal or network. */
const assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../web/workspace.js'), 'utf8');
const start = source.indexOf('  const provisionSchemas ='), end = source.indexOf('  function serverInstanceCard(', start);
assert.ok(start > 0 && end > start);
const storage = new Map(); let serial = 0;
const notices = [], fetches = []; let fetchReply;
const scope = vm.createContext({ TextEncoder, TextDecoder, Uint8Array, AbortController, setTimeout, clearTimeout, Number, Date, Map, Set,
  basePath: '', workPath: encodeURIComponent, $: () => ({ open: false }), notify: (...v) => notices.push(v), showLogin: () => {},
  fetch: async (url, options) => { fetches.push({ url, body: options.body, method: options.method }); return fetchReply(url, options); },
  state: { actor: { owner_id: 'owner' }, view: 'other', csrf: 'synthetic' },
  serverInteger: (v, min = 0) => Number.isSafeInteger(v) && v >= min, serverNeedsReload: v => ['conflict', 'refused'].includes(v),
  idempotencyKey: () => 'synthetic-key-' + (++serial), sessionStorage: { getItem: k => storage.get(k) ?? null, setItem: (k, v) => storage.set(k, v) } });
vm.runInContext(source.slice(start, end) + '\nthis.api={appendBodyValid,appendGrantValid,appendContextValid,appendViewValid,appendListValid,appendRecord,appendStore,saveAppendStore,updateAppendRecord,appendStorageKey,provisionStorageKey,sendAppendRecord,appendApi};', scope);
const api = scope.api, hash = 'a'.repeat(64), owner = 'owner', host = { id: 'host', device_id: 'manager' };
const profile = { id: 'profile', version: 1, preferred_provider: 'codex_chatgpt', preferred_model: 'requested-model' };
const grant = { schema: 'server-messages-append-grant-view-v1', owner_id: owner, host_id: host.id, device_id: host.device_id, purpose: 'messages_append_v1', version: 1, enabled: true };
const context = { schema: 'server-messages-append-context-v1', owner_id: owner, host_id: host.id, device_id: host.device_id,
  expected_host_version: 1, expected_catalog_version: 1, expected_instances_sha256: hash, grant_version: 1,
  profile_id: profile.id, profile_version: 1, profile_sha256: hash, provider_id: profile.preferred_provider, requested_model: profile.preferred_model,
  slots: [{ slot_id: hash, profile_id: profile.id, profile_version: 1, device_id: 'slot-device', session_id: 'slot-session', agent_kind: 'codex', identity_key: 'slot-identity' }] };
const body = { schema: 'server-messages-append-request-v1', ...Object.fromEntries(['expected_host_version','expected_catalog_version','expected_instances_sha256','grant_version','profile_id','profile_version','profile_sha256','provider_id','requested_model'].map(k => [k, context[k]])), slot_id: hash };
const row = { schema: 'server-messages-append-view-v1', request_id: 'request', request_sha256: hash, owner_id: owner,
  host_id: host.id, device_id: host.device_id, revision: 1, state: 'requested', reason: 'requested', barrier_held: true,
  profile_id: profile.id, profile_version: 1, provider_id: profile.preferred_provider, requested_model: profile.preferred_model,
  slot_id: hash, can_cancel: true, instance_id: null, proof: null, connection_status: 'not_attested', model_catalog_status: 'not_attested', model_access_status: 'not_attested', controller_status: 'not_reported', controller_reason: '' };
let checks = 0;
const yes = value => { assert.equal(value, true); checks++; }, no = value => { assert.equal(value, false); checks++; };
yes(api.appendBodyValid('request', body)); yes(api.appendGrantValid(grant, owner, host)); yes(api.appendContextValid(context, owner, host, profile)); yes(api.appendViewValid(row, owner, host));
for (const patch of [{ schema: 'server-provision-draft-request-v1' }, { requested_model: null }, { requested_model: '' }, { provider_id: 'claude_code' }, { grant_version: true }, { profile_sha256: 'X'.repeat(64) }, { slot_id: 'slot-path' }, { token: 'SENTINEL_SECRET' }]) no(api.appendBodyValid('request', { ...body, ...patch }));
for (const patch of [{ purpose: 'metadata_request_only_v1' }, { owner_id: 'foreign' }, { device_id: 'slot-device' }, { version: true }, { extra: false }]) no(api.appendGrantValid({ ...grant, ...patch }, owner, host));
for (const patch of [{ slots: [context.slots[0], context.slots[0]] }, { slots: [{ ...context.slots[0], profile_id: 'foreign' }] }, { slots: [{ ...context.slots[0], token: 'SENTINEL_SECRET' }] }, { owner_id: 'foreign' }, { provider_id: 'claude_code' }, { requested_model: null }]) no(api.appendContextValid({ ...context, ...patch }, owner, host, profile));
yes(api.appendContextValid({ ...context, slots: [] }, owner, host, profile));
for (const patch of [{ state: 'connected' }, { connection_status: 'connected' }, { model_access_status: 'confirmed' }, { can_cancel: 1 }, { proof: {} }, { instance_id: 'already-created' }, { extra: 'SENTINEL_SECRET' }]) no(api.appendViewValid({ ...row, ...patch }, owner, host));
for (const [phase, revision] of [['prepared',2],['precommit',3],['committed',4],['complete',5]]) {
  const instance = revision >= 4 ? 'new-instance' : null;
  const current = { ...row, state: phase, reason: phase, revision, can_cancel: false, barrier_held: phase !== 'complete', instance_id: instance };
  current.proof = { schema: 'server-messages-append-proof-v1', operation_id: '12345678-1234-4123-8123-123456789abc',
    request_id: row.request_id, request_sha256: hash, owner_id: owner, host_id: host.id, device_id: host.device_id,
    grant_version: 1, request_revision: revision, expected_host_version: 1, expected_catalog_version: 1,
    expected_instances_sha256: hash, source_release_sha256: hash, old_configuration_sha256: hash,
    new_configuration_sha256: hash, old_catalog_sha256: hash, new_catalog_sha256: hash, bindings_sha256: hash,
    children_roster_sha256: hash, phase, instance_id: instance, catalog_version: revision >= 4 ? 2 : null,
    instances_sha256: revision >= 4 ? hash : null };
  yes(api.appendViewValid(current, owner, host));
  for (const patch of [{ can_cancel: true }, { revision: true }, { barrier_held: !current.barrier_held }, { proof: { ...current.proof, owner_id: 'foreign' } }, { proof: { ...current.proof, request_revision: 1 } }, { proof: { ...current.proof, private_path: 'SENTINEL_SECRET' } }]) no(api.appendViewValid({ ...current, ...patch }, owner, host));
}
const cancelled = { ...row, state: 'cancelled', reason: 'cancelled_by_owner', revision: 2, barrier_held: false, can_cancel: false };
yes(api.appendViewValid(cancelled, owner, host)); no(api.appendViewValid({ ...cancelled, can_cancel: true }, owner, host));
yes(api.appendViewValid({ ...row, controller_status: 'blocked', controller_reason: 'children_uncertain' }, owner, host));
for (const patch of [{ controller_status: 'ready' }, { controller_status: 'blocked', controller_reason: 'PRIVATE_FAILURE_SENTINEL' }, { controller_status: 'blocked', controller_reason: {} }, { controller_status: 'not_reported', controller_reason: 'source_changed' }]) no(api.appendViewValid({ ...row, ...patch }, owner, host));
no(api.appendContextValid({ ...context, slots: [{ ...context.slots[0], agent_kind: 'claude-code' }] }, owner, host, profile));
yes(api.appendListValid({ schema: 'server-messages-append-list-v1', requests: [row], next_before: null }, owner, host));
no(api.appendListValid({ schema: 'server-messages-append-list-v1', requests: [row, row], next_before: null }, owner, host));
const saved = api.appendRecord(owner, host, 'request', body), original = JSON.stringify(saved);
api.updateAppendRecord(owner, saved.key, 'uncertain');
const retry = api.appendRecord(owner, host, 'request', body);
assert.equal(retry.key, saved.key); assert.equal(JSON.stringify(retry.body), JSON.stringify(body)); checks++;
assert.throws(() => api.appendRecord(owner, host, 'request', { ...body, slot_id: 'b'.repeat(64) }), /reste à confirmer/); checks++;
assert.notEqual(api.appendStorageKey(owner), api.provisionStorageKey(owner));
assert.equal(storage.get(api.provisionStorageKey(owner)), undefined); checks++;
scope.state.actor.owner_id = 'foreign'; assert.throws(() => api.appendRecord(owner, host, 'request', body), /compte a changé/); checks++;
assert.equal(JSON.stringify(saved), original); checks++;
(async () => {
  scope.state.actor.owner_id = owner;
  let refusals = 0;
  for (const status of [400, 401, 403, 404, 409, 422]) {
    storage.clear(); const first = api.appendRecord(owner, host, 'request', body), before = fetches.length;
    fetchReply = () => new Response(JSON.stringify({ detail: 'PRIVATE_RESPONSE_SENTINEL' }), { status });
    await api.sendAppendRecord(owner, host, first);
    const saved = api.appendStore(owner).records[0]; assert.equal(saved.status, status === 409 ? 'conflict' : 'refused');
    assert.equal(fetches.length, before + 1);
    await assert.rejects(() => api.sendAppendRecord(owner, host, saved), /Actualisez/);
    assert.throws(() => api.appendRecord(owner, host, 'request', { ...body, slot_id: 'b'.repeat(64) }), /reste à confirmer/);
    assert.equal(fetches.length, before + 1); refusals++;
  }
  storage.clear(); const first = api.appendRecord(owner, host, 'request', body);
  fetchReply = () => { throw new Error('PRIVATE_RESPONSE_SENTINEL'); };
  await api.sendAppendRecord(owner, host, first); const pending = api.appendStore(owner).records[0];
  assert.equal(pending.status, 'uncertain'); const sent = fetches.at(-1);
  fetchReply = () => new Response(JSON.stringify(row));
  await api.sendAppendRecord(owner, host, pending); assert.deepEqual(fetches.at(-1), sent);
  assert.equal(api.appendStore(owner).records[0].status, 'confirmed');
  storage.clear(); const bounded = api.appendRecord(owner, host, 'request', body);
  fetchReply = () => new Response(JSON.stringify({ private: 'PRIVATE_RESPONSE_SENTINEL'.repeat(1000) }));
  await api.sendAppendRecord(owner, host, bounded); assert.equal(api.appendStore(owner).records[0].status, 'uncertain');
  assert.ok(!JSON.stringify(notices).includes('PRIVATE_RESPONSE_SENTINEL'));
  console.log(JSON.stringify({ checks, terminal_refusal_cases: refusals, response_loss_exact_retry: true,
    oversized_response_preserves_uncertainty: true, private_response_not_rendered: true, network_requests: 0, promotion_from_A: false }));
})().catch(error => { console.error(error); process.exitCode = 1; });
