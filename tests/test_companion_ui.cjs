'use strict';
/* Actual UI functions in a synthetic DOM: no server, provider or network. */
const assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
const root = path.join(__dirname, '..');
const app = fs.readFileSync(path.join(root, 'web/app.js'), 'utf8');
const workspace = fs.readFileSync(path.join(root, 'web/workspace.js'), 'utf8');
function fragment(source, from, to) {
  const start = source.indexOf(from), end = source.indexOf(to, start);
  assert.ok(start >= 0 && end > start, from);
  return source.slice(start, end);
}
class Element {
  constructor(tag, props = {}, children = []) {
    this.tag = tag; this.props = { ...props }; this.children = []; this.listeners = {};
    Object.assign(this, props); this.value ??= ''; this.checked ??= false; this.isConnected = true;
    this.append(children);
    this.elements = { namedItem: name => this.querySelectorAll('input, textarea, select').find(node => node.name === name) };
  }
  // Like the browser: native append() renders null/undefined as text ("null").
  append(...values) { this.children.push(...values.flat(Infinity).map(value => value === null || value === undefined ? String(value) : value)); }
  replaceChildren(...values) { this.children = []; this.append(values); }
  setAttribute(key, value) { this.props[key] = value; this[key] = value; }
  addEventListener(name, action) { this.listeners[name] = action; }
  insertBefore(node, reference) { const index = this.children.indexOf(reference); this.children.splice(Math.max(0, index), 0, node); }
  remove() { this.isConnected = false; }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  querySelectorAll(selector) {
    return descendants(this).filter(node => selector.split(',').some(part => {
      const match = part.trim().match(/^([a-z]+)(?:\[(name|type)="([^"]+)"\])?$/);
      return match && node.tag === match[1] && (!match[2] || node[match[2]] === match[3]);
    }));
  }
}
const descendants = node => node instanceof Element ? node.children.flatMap(child => child instanceof Element ? [child, ...descendants(child)] : []) : [];
const el = (tag, props = {}, ...children) => new Element(tag, props, children);
const text = node => node instanceof Element ? node.children.map(text).join(' ') : String(node);
const buttons = node => descendants(node).filter(child => child.tag === 'button');
const button = (node, label) => buttons(node).find(child => text(child) === label);
const btn = (label, action, className = '', props = {}) => el('button', { type: 'button', onClick: action, class: className, ...props }, label);
const field = (label, name, options = {}) => el('label', {}, label,
  el(options.type === 'textarea' ? 'textarea' : 'input', { name, type: options.type || 'text', value: options.value || '' }));
const check = (label, name, checked, value = 'on') => el('label', {}, label, el('input', { name, type: 'checkbox', checked: !!checked, value }));
const select = (label, name, options, value) => el('label', {}, label,
  el('select', { name, value: value ?? options[0]?.[0] ?? '' }, options.map(([key, name]) => el('option', { value: key }, name))));
class FormData {
  constructor(form) {
    this.values = new Map();
    for (const node of form.querySelectorAll('input, textarea, select')) {
      if (!node.name || node.disabled || node.type === 'checkbox' && !node.checked) continue;
      this.values.set(node.name, [...(this.values.get(node.name) || []), node.value]);
    }
  }
  get(name) { return this.values.get(name)?.[0] ?? null; }
  getAll(name) { return this.values.get(name) || []; }
  has(name) { return this.values.has(name); }
}
const policy = () => ({ allow_read: false, allow_messages: false, allow_requests: false, autonomy: 'responses', actions: [], resources: [] });
const profile = (kind = 'agent', extra = {}) => ({ id: kind, kind, acts_for: kind === 'companion' ? 'owner' : null,
  owner_id: 'owner', name: kind === 'companion' ? 'Ari' : 'Worker', version: 1, status: 'active',
  project_ids: [], bindings: [], active_work: [], policy: policy(), ...extra });
const ownerName = owner => ({ owner: 'Alice', foreign: 'Bob' })[owner] || owner;
let body, modal, entries = [], pendingRecord = null, serial = 0, totalCalls = 0, projectReply = async () => [], creationReply = value => value;
const calls = [], notices = [], commands = [];
const scope = vm.createContext({ el, btn, field, check, select, FormData, TextEncoder, URLSearchParams,
  ownerName, workPath: encodeURIComponent, date: () => 'date', lines: value => [...new Set(value.split(/\r?\n/).filter(Boolean))],
  state: { actor: { kind: 'human', owner_id: 'owner' }, request: 0, view: 'agents', owners: [] },
  badge: (label, className = '') => el('span', { class: 'badge ' + className }, label),
  profileAvatar: () => el('span'), profileStatus: value => el('span', {}, value.status), presenceBadge: () => el('span'),
  workLink: label => el('a', {}, label), status: () => el('span'), empty: label => el('p', {}, label),
  panel: (label, ...content) => el('section', {}, el('h2', {}, label), content),
  box: (label, description, action) => el('section', {}, el('h2', {}, label), el('p', {}, description), action),
  tabs: items => el('nav', {}, items.map(([, label]) => el('span', {}, label))),
  frame: (_title, _subtitle, actions = []) => (body = el('main', {}, actions)),
  navigate: view => { scope.state.view = view; }, workRoute: () => {},
  workError: error => { throw error; }, idempotencyKey: () => 'synthetic-' + (++serial),
  openModal: (title, content) => { modal = { title, content }; }, closeModal: () => { modal = null; },
  $: id => { assert.equal(id, 'modal'); return { open: modal !== null }; },
  notify: value => notices.push(value), bindForm: (form, action) => { form.submit = action; },
  formButtons: label => el('button', { type: 'submit' }, label),
  profileWorkNotice: () => null, profileTemplates: {}, profileStates: { active: 'Active', paused: 'Paused', archived: 'Archived' },
  profileField: (label, name, _maximum, options) => field(label, name, options),
  Markdown: { block: value => el('p', {}, value) },
  serverCommand: () => null, pendingServerRecord: () => pendingRecord, serverNeedsReload: () => false, serverDate: () => 'date',
  serverStates: { stopped: 'Stopped', ready: 'Ready' }, serverReasons: {}, serverActions: { start: 'Start', stop: 'Stop', restart: 'Restart' },
  serverActionDialog: (_owner, _instance, action) => commands.push(action), sendServerRecord: () => commands.push('retry'),
  changeProfileStatus: () => {}, profileBindings: () => {}, renameAgent: () => {}, setup: () => {}, providerWizard: () => {},
  document: {}, api: async (url, options) => {
    totalCalls++;
    calls.push({ url, method: options?.method || 'GET', data: options?.data });
    if (url === '/projects?include_archived=true') return projectReply();
    if (url === '/agent-profiles?include_archived=true') return entries;
    if (url === '/agents') return [];
    if (url.startsWith('/agent-profiles/') && !options) return entries.find(item => item.id === url.split('/')[2]);
    if (url === '/agent-profiles' && options?.method === 'POST') {
      const kind = options.data.kind || 'agent', saved = profile(kind, { ...options.data, id: 'created-' + serial });
      entries.push(saved); return creationReply(saved);
    }
    if (url.startsWith('/agent-profiles/') && options?.method === 'PATCH') {
      const saved = entries.find(item => item.id === url.split('/')[2]); Object.assign(saved, options.data); return saved;
    }
    throw new Error('Unexpected synthetic endpoint: ' + url);
  }
});
vm.runInContext('const guard = action => (...args) => Promise.resolve().then(() => action(...args));\nlet agentTab="profiles", includeArchivedProfiles=false;\n'
  + fragment(app, 'function companionOwner(', 'function renderMessages(')
  + fragment(workspace, '  function activityList(', '  const profileStates =')
  + fragment(workspace, '  function profileCard(', '  const serverStates =')
  + fragment(workspace, '  function serverInstanceCard(', '  async function renderServerHosts(')
  + fragment(workspace, '  async function agents()', '  const runtimeReasons =')
  + fragment(workspace, '  async function profileEditor(', '  function changeProfileStatus(')
  + fragment(workspace, '  async function profileHistory(', '  async function profileBindings(')
  + '\nthis.ui={companionOwner,authorLabel,authorMeta,activityList,agentList,profileCard,agents,profileEditor,duplicateProfile,profileHistory,serverInstanceCard};', scope);
const ui = scope.ui;
let checks = 0;
const ok = (value, message) => { assert.ok(value, message); checks++; };
const companion = { kind: 'agent', profile_kind: 'companion', companion_for: 'owner', acts_for: 'owner',
  owner_id: 'owner', agent_name: 'Ari', display_name: 'Alice' };
const reset = () => { entries = []; calls.length = 0; scope.state.actor = { kind: 'human', owner_id: 'owner' };
  scope.state.view = 'agents'; modal = null; projectReply = async () => []; creationReply = value => value; pendingRecord = null; };
(async () => {
  assert.equal(ui.authorLabel(companion), 'Ari · for Alice'); checks++;
  ok(text(ui.authorMeta(companion, 1)).includes('COMPANION'));
  for (const patch of [{ profile_kind: 'agent' }, { companion_for: 'foreign' }, { acts_for: 'foreign' }, { owner_id: 'foreign' },
    { companion_for: null }, { kind: 'human' }, { kind: 'system' }]) {
    ok(!text(ui.authorMeta({ ...companion, ...patch }, 1)).includes('COMPANION'), 'A forged or incomplete marker stays ordinary');
  }
  assert.equal(ui.authorLabel({ kind: 'agent', agent_name: 'Worker', owner_id: 'owner' }), 'Worker'); checks++;
  assert.equal(ui.authorLabel({ kind: 'human', display_name: 'Alice', owner_id: 'owner' }), 'Alice'); checks++;
  assert.equal(ui.authorLabel({ kind: 'system' }), 'Board'); checks++;
  const ordinary = ui.profileCard(profile()), own = ui.profileCard(profile('companion'));
  ok(button(ordinary, 'Duplicate')); ok(!text(ordinary).includes('COMPANION'));
  ok(!button(own, 'Duplicate')); ok(!text(own).includes('Current work'));
  ok(text(own).includes('Ari · for Alice') && text(own).includes('COMPANION'));
  const shared = ui.profileCard(profile('companion', { owner_id: 'foreign', acts_for: 'foreign' }));
  ok(text(shared).includes('Ari · for Bob') && !button(shared, 'Edit profile'));
  ok(!text(ui.profileCard(profile('companion', { acts_for: 'foreign' }))).includes('COMPANION'));
  const connected = ui.agentList([profile('companion', { tickets: [{ id: 'hidden', number: 17, title: 'Hidden execution' }], sessions: [{ label: 'Personal runtime', autonomous: true }] })]);
  ok(text(connected).includes('Ari · for Alice') && !text(connected).includes('Hidden execution') && !text(connected).includes('Worker'));
  const activity = ui.activityList([{ actor: companion, event: 'ticket_updated', number: 1, ticket_id: 't', detail: {}, created_at: 1 }]);
  ok(text(activity).includes('Ari · for Alice') && text(activity).includes('COMPANION'));
  reset(); await ui.agents(); ok(button(body, 'Create companion'));
  entries = [profile('companion', { status: 'archived' })]; await ui.agents();
  ok(!button(body, 'Create companion'), 'An archived companion still consumes the one-companion slot');
  ok(text(body).includes('Ari · for Alice'));
  reset(); scope.state.actor.kind = 'agent'; await ui.agents(); ok(!button(body, 'Create companion'));
  const before = calls.length; await ui.profileEditor(null, null, 'companion'); assert.equal(calls.length, before); checks++;
  reset(); await ui.profileEditor(null, null, 'companion');
  assert.equal(modal.title, 'Create companion'); checks++;
  const create = modal.content; create.elements.namedItem('name').value = 'Ari';
  ok(!text(create).includes('Authorized queue work'));
  ok(!text(create).includes('null'), 'Hidden companion sections leave no "null" text');
  await create.submit(new FormData(create));
  const post = calls.find(call => call.method === 'POST');
  assert.equal(post.data.kind, 'companion'); checks++;
  ok(!Object.hasOwn(post.data, 'acts_for') && !button(body, 'Create companion'));
  ok(!Object.hasOwn(post.data, 'policy') && !Object.hasOwn(post.data, 'project_ids'), 'Creation retains backend companion defaults and owner scope');
  reset(); entries = [profile('companion', { policy: { ...policy(), allow_read: true, allow_messages: true, allow_requests: true, autonomy: 'requested' } })]; await ui.profileEditor('companion');
  const edit = modal.content; await edit.submit(new FormData(edit));
  const patch = calls.find(call => call.method === 'PATCH');
  ok(!Object.hasOwn(patch.data, 'kind'), 'Editing never offers kind conversion');
  assert.equal(patch.data.policy.autonomy, 'requested'); checks++;
  ok(patch.data.policy.allow_read && patch.data.policy.allow_messages && patch.data.policy.allow_requests);
  await ui.profileHistory('companion');
  ok(text(modal.content).includes('Ari · for Alice') && text(modal.content).includes('COMPANION'));
  reset(); await ui.profileEditor(); await modal.content.submit(new FormData(modal.content));
  ok(!Object.hasOwn(calls.find(call => call.method === 'POST').data, 'kind'), 'Ordinary profile creation retains its default contract');
  reset(); const pending = {}; pending.promise = new Promise(resolve => { pending.resolve = resolve; }); projectReply = () => pending.promise;
  const opening = ui.profileEditor(null, null, 'companion'); scope.state.request++; pending.resolve([]); await opening;
  ok(modal === null, 'A late creation response cannot open after navigation');
  for (const failure of [false, true]) {
    reset(); const pending = {}; pending.promise = new Promise((resolve, reject) => { pending.resolve = resolve; pending.reject = reject; }); projectReply = () => pending.promise;
    const opening = ui.profileEditor(null, null, 'companion'), edit = { title: 'Edit profile', content: el('form', {}, 'Unsaved changes') };
    modal = edit;
    if (failure) pending.reject(new Error('STALE_CREATION_ERROR')); else pending.resolve([]);
    await opening; assert.equal(modal, edit); checks++;
  }
  reset(); await ui.profileEditor(null, null, 'companion'); creationReply = value => ({ ...value, kind: 'agent' });
  const unconfirmed = modal.content; await assert.rejects(unconfirmed.submit(new FormData(unconfirmed)), /not confirmed/); checks++;
  assert.equal(modal.content, unconfirmed); checks++;
  reset(); const beforeDuplicate = calls.length; ui.duplicateProfile(profile('companion')); assert.equal(calls.length, beforeDuplicate); checks++;
  const host = { enabled: true, revoked_at: null }, instance = { id: 'i', profile_id: 'companion', profile_version: 1,
    active: true, can_start: true, observed_state: 'ready', desired_state: 'running', cleanup_confirmed: true };
  const capacity = [], barriers = [], card = ui.serverInstanceCard('owner', host, instance,
    new Map([['companion', profile('companion')]]), {}, capacity, barriers);
  ok(!button(card, 'Démarrer') && !button(card, 'Redémarrer'));
  ok(button(card, 'Arrêter') && !button(card, 'Arrêter').disabled);
  button(card, 'Arrêter').onClick(); assert.deepEqual(commands, ['stop']); checks++;
  capacity.forEach(update => update({ occupied: 1, maximum: 1 })); barriers.forEach(update => update(true));
  const workerCard = ui.serverInstanceCard('owner', host, { ...instance, profile_id: 'agent' },
    new Map([['agent', profile()]]), {}, [], []);
  ok(button(workerCard, 'Démarrer') && button(workerCard, 'Redémarrer'), 'Ordinary controls remain');
  pendingRecord = { status: 'prepared', body: { action: 'start' } };
  ok(!button(ui.serverInstanceCard('owner', host, instance, new Map([['companion', profile('companion')]]), {}, [], []), 'Réessayer la même demande'));
  pendingRecord = { status: 'prepared', body: { action: 'stop' } };
  ok(button(ui.serverInstanceCard('owner', host, instance, new Map([['companion', profile('companion')]]), {}, [], []), 'Réessayer la même demande'));
  const all = [profile(), profile('companion')];
  scope.agentEntries = all; scope.t = null;
  const assignment = workspace.match(/const assignable = ([^;]+);/)[1];
  ok(vm.runInContext(assignment, scope).every(item => item.kind !== 'companion'));
  scope.profiles = all; scope.owner = 'owner'; scope.published = new Set();
  const provisioning = workspace.match(/const newProfiles = ([^;]+);/)[1];
  ok(vm.runInContext(provisioning, scope).every(item => item.kind !== 'companion'));
  scope.profiles = all.map(item => ({ ...item, preferred_provider: 'codex_chatgpt', preferred_model: 'model', bindings: [{ device_id: 'manager', revoked: false }] }));
  scope.host = { device_id: 'manager' }; scope.serverInteger = value => Number.isSafeInteger(value);
  scope.provisionProviders = { codex_chatgpt: 'Codex' }; scope.provisionModel = () => true; scope.provisionText = value => typeof value === 'string' && value.length > 0;
  const append = fragment(workspace, '  async function appendRequestDialog(', '  async function renderMessagesAppend(').match(/const candidates = ([^;]+);/)[1];
  ok(vm.runInContext(append, scope).every(item => item.kind !== 'companion'));
  console.log(JSON.stringify({ result: 'passed', checks, synthetic_calls: totalCalls,
    network_calls: 0, providers: 0, board_effects: 0 }));
})().catch(error => { console.error(error); process.exitCode = 1; });
