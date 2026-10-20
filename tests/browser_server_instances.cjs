/* Firefox acceptance against synthetic loopback metadata only. No agent runs. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const { firefox } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const web = path.resolve(__dirname, '../web');
const output = process.env.BOARD_TEST_OUTPUT || '/private/tmp/agent-board-server-ui-qa';
fs.mkdirSync(output, { recursive: true });
const clone = value => JSON.parse(JSON.stringify(value));
const owner = 'thescreamer';
const hash = 'a'.repeat(64);
const command = (instance, action, status = 'complete', budget = 1) => ({ id: 'command-' + instance.id + '-' + instance.version,
  seq: instance.version, instance_id: instance.id, action, generation: instance.generation, launchspec_sha256: hash,
  instance_version: instance.version, budget: action === 'stop' ? 0 : budget, revision: 1, status, stop_confirmed: false,
  created_at: 1000, updated_at: 1000 });
const make = (id, observed, cleanup = false) => ({ id, host_id: 'host1', catalog_key: id, profile_id: 'profile-' + id,
  profile_version: 1, launchspec_sha256: hash, mode: 'messages', version: 1, generation: 0,
  desired_state: observed === 'stopped' ? 'stopped' : 'running', observed_state: observed, observation_revision: 1,
  reason: '', last_seen_at: 1000, command_id: null, active: true, cleanup_confirmed: cleanup, can_start: true,
  control_reason: '', current_command: null });
const stopped = make('stopped', 'stopped', true), busy = make('busy', 'busy'), unknown = make('unknown', 'unknown'),
  review = make('review', 'needs_review', true), restarting = make('restarting', 'busy'), changed = make('changed', 'stopped', true);
for (const instance of [busy, restarting]) { instance.generation = 1; instance.current_command = command(instance, instance === busy ? 'start' : 'restart', instance === busy ? 'complete' : 'queued'); instance.command_id = instance.current_command.id; }
changed.can_start = false; changed.control_reason = 'configuration_changed';
const host = { id: 'host1', owner_id: owner, device_id: 'device-own', name: 'Mon serveur <img src=x onerror=alert(1)>',
  version: 1, catalog_version: 1, revoked_at: null, enabled: true, instances: [stopped, busy, unknown, review, restarting, changed] };
let hosts = [host];
const profiles = host.instances.map(item => ({ id: item.profile_id, owner_id: owner, name: 'Profil ' + item.id,
  version: 1, status: 'active', presence: 'offline', role: 'Développement', preferred_provider: 'codex_chatgpt', preferred_model: 'Modèle de test', project_ids: [], bindings: [], active_work: [] }));
const requests = [], commands = new Map(), grants = new Map();
let grantCount = 0;
let rejectNext = false, commandCount = 0, hostReads = 0;
const json = (response, value, status = 200) => { response.writeHead(status, { 'Content-Type': 'application/json' }); response.end(JSON.stringify(value)); };
const server = http.createServer(async (request, response) => {
  try {
    const target = new URL(request.url, 'http://127.0.0.1');
    if (target.pathname.startsWith('/api/')) {
      let raw = ''; for await (const chunk of request) raw += chunk;
      const body = raw ? JSON.parse(raw) : undefined;
      requests.push({ path: target.pathname, method: request.method, body });
      if (target.pathname === '/api/me') return json(response, { actor: { kind: 'human', owner_id: owner }, csrf_token: 'synthetic-csrf', owners: [{ id: owner, display_name: 'TheScreamer' }] });
      if (target.pathname === '/api/threads' || target.pathname === '/api/requests' || target.pathname === '/api/projects') return json(response, []);
      if (target.pathname === '/api/inbox') return json(response, { count: 0, messages: [], tickets: [], tasks: [] });
      if (target.pathname === '/api/agent-profiles' || target.pathname === '/api/agents') return json(response, profiles);
      if (target.pathname === '/api/settings') return json(response, { devices: [
        { id: 'device-own', owner_id: owner, label: 'Ubuntu personnel', revoked: false },
        { id: 'device-revoked', owner_id: owner, label: 'Ancien appareil', revoked: true },
        { id: 'device-foreign', owner_id: 'blackoutburst', label: 'PRIVATE OTHER DEVICE', revoked: false }] });
      if (target.pathname === '/api/server-hosts' && request.method === 'GET') return json(response, { hosts: [...hosts.map(({ instances, ...summary }) => summary), { id: 'foreign-host', owner_id: 'blackoutburst', name: 'PRIVATE OTHER HOST' }] });
      if (target.pathname.startsWith('/api/server-hosts/') && request.method === 'GET') {
        hostReads++; const item = hosts.find(item => item.id === decodeURIComponent(target.pathname.split('/').pop()));
        return json(response, item || { detail: 'not_found' }, item ? 200 : 404);
      }
      if (target.pathname === '/api/server-hosts' && request.method === 'POST') {
        assert.equal(body.device_id, 'device-own'); assert.ok(body.idempotency_key);
        if (grants.has(body.idempotency_key)) { assert.deepEqual(grants.get(body.idempotency_key).body, body); return json(response, grants.get(body.idempotency_key).host); }
        grantCount++;
        const item = { id: 'new-host', owner_id: owner, device_id: body.device_id, name: body.name, version: 1, catalog_version: 0,
          revoked_at: null, enabled: true, instances: [] }; hosts = [item]; grants.set(body.idempotency_key, { body: clone(body), host: clone(item) }); return json(response, item);
      }
      if (target.pathname.startsWith('/api/server-instances/') && request.method === 'POST') {
        const id = decodeURIComponent(target.pathname.split('/')[3]), instance = host.instances.find(item => item.id === id);
        assert.ok(instance); assert.ok(body.idempotency_key);
        if (commands.has(body.idempotency_key)) { const old = commands.get(body.idempotency_key); assert.deepEqual(old.body, body); return json(response, old.command); }
        if (rejectNext) { rejectNext = false; instance.version++; return json(response, { detail: 'server_instance_changed' }, 409); }
        assert.equal(body.expected_version, instance.version); assert.equal(body.expected_generation, instance.generation);
        assert.equal(body.launchspec_sha256, hash); assert.ok(Number.isInteger(body.budget));
        assert.ok(body.action === 'stop' ? body.budget === 0 : body.budget >= 1 && body.budget <= 20);
        instance.version++; if (body.action !== 'stop') instance.generation++;
        instance.desired_state = body.action === 'stop' ? 'stopped' : 'running';
        const result = command(instance, body.action, 'queued', body.budget); result.id = 'created-' + (++commandCount);
        instance.current_command = result; instance.command_id = result.id;
        commands.set(body.idempotency_key, { body: clone(body), command: clone(result) });
        return json(response, result);
      }
      return json(response, { detail: 'unexpected_synthetic_route' }, 404);
    }
    const name = target.pathname === '/' ? 'index.html' : decodeURIComponent(target.pathname.slice(1));
    const filename = path.resolve(web, name);
    if (!filename.startsWith(web + path.sep)) { response.writeHead(403); response.end(); return; }
    if (!fs.existsSync(filename)) { response.writeHead(404); response.end(); return; }
    const data = fs.readFileSync(filename);response.writeHead(200, { 'Content-Type': filename.endsWith('.js') ? 'application/javascript' : filename.endsWith('.css') ? 'text/css' : 'text/html', 'Cache-Control': 'no-store' });response.end(data);
  } catch (error) { console.error(error); json(response, { detail: 'synthetic_fixture_error' }, 500); }
});
(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  assert.equal(new URL(origin).hostname, '127.0.0.1');
  const browser = await firefox.launch({ headless: true });
  try {
    const context = await browser.newContext({ viewport: { width: 1440, height: 1080 } });
    const page = await context.newPage(), errors = [];
    let dropNextReply = false, dropNextGrantReply = false;
    await page.route("**/api/server-hosts", async route => {
      if (dropNextGrantReply && route.request().method() === "POST") { dropNextGrantReply = false; await route.fetch(); await route.abort("failed"); }
      else await route.continue();
    });
    await page.route('**/api/server-instances/stopped/commands', async route => {
      if (dropNextReply) { dropNextReply = false; await route.fetch(); await route.abort('failed'); }
      else await route.continue();
    });
    page.on('pageerror', error => errors.push(error.message));page.on('dialog', () => { throw new Error('Unsafe content created a browser dialog'); });
    const card = id => page.locator(`[data-server-instance="${id}"]`);
    const control = (id, label) => card(id).getByRole('button', { name: label, exact: true });
    const dialog = () => page.getByRole('dialog');
    const refresh = async () => { await page.getByRole('button', { name: 'Actualiser', exact: true }).click(); await card('stopped').waitFor(); };
    const go = async () => { await page.goto(origin + '/?page=agents'); await page.getByRole('button', { name: 'Sur le serveur', exact: true }).click(); await card('stopped').waitFor(); };
    const noOverflow = async () => assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
    await go();
    assert.ok(await control('stopped', 'Démarrer').isEnabled());assert.ok(await control('busy', 'Redémarrer').isEnabled());
    for (const id of ['unknown', 'review', 'changed', 'restarting']) {
      assert.ok(await control(id, 'Démarrer').isDisabled());assert.ok(await control(id, 'Redémarrer').isDisabled());
      assert.equal(await control(id, 'Arrêter').isEnabled(), id !== 'changed');
    }
    assert.equal(await page.locator('#main img, #main svg').count(), 0);
    assert.ok(!(await page.locator('#main').innerText()).includes('PRIVATE OTHER'));
    assert.ok(!requests.some(item => item.path.endsWith('/foreign-host')));
    assert.ok(!(await page.locator("#main").innerText()).split("\n").some(line => line === "null"));
    await noOverflow();await page.screenshot({ path: path.join(output, 'hosted-desktop.png'), fullPage: true });

    await control('busy', 'Arrêter').click();await dialog().getByRole('button', { name: 'Arrêter', exact: true }).click();
    await control('busy', 'Arrêt demandé').waitFor();assert.match(await card('busy').innerText(), /Au travail/);
    assert.equal(await card('busy').getByText('Arrêt confirmé', { exact: true }).count(), 0);
    busy.observed_state = 'stopped';busy.cleanup_confirmed = true;busy.current_command.status = 'complete';await refresh();
    assert.ok(await control('busy', 'Démarrer').isEnabled());

    dropNextReply = true;await control('stopped', 'Démarrer').click();await dialog().getByLabel('Budget de travail (1 à 20 tours)').fill('2');
    await dialog().getByRole('button', { name: 'Démarrer', exact: true }).click();
    await page.locator('#notice').filter({ hasText: 'La réponse n’a pas été confirmée.' }).waitFor({ timeout: 5000 });
    const original = requests.filter(item => item.path.includes('/stopped/commands')).at(-1).body;
    const countBeforeRetry = commandCount;
    await go();await control('stopped', 'Réessayer la même demande').click();await page.getByText('Demande enregistrée.', { exact: false }).waitFor();
    const replay = requests.filter(item => item.path.includes('/stopped/commands')).at(-1).body;
    assert.deepEqual(replay, original);assert.equal(commandCount, countBeforeRetry);
    assert.ok(await control('stopped', 'Démarrer').isDisabled());
    stopped.observed_state = 'ready';stopped.cleanup_confirmed = false;stopped.current_command.status = 'complete';await refresh();
    assert.ok(await control('stopped', 'Redémarrer').isEnabled());

    rejectNext = true;await control('stopped', 'Redémarrer').click();await dialog().getByLabel('Budget de travail (1 à 20 tours)').fill('3');
    const reads = hostReads, postCount = requests.filter(item => item.method === 'POST').length;
    await dialog().getByRole('button', { name: 'Redémarrer', exact: true }).click();
    await page.locator('#notice').filter({ hasText: 'L’état a changé. Actualisez les instances' }).waitFor();
    assert.equal(hostReads, reads, 'CAS conflict never automatically reloads or creates a replacement request');
    assert.equal(requests.filter(item => item.method === 'POST').length, postCount + 1);
    await refresh();await control('stopped', 'Redémarrer').click();
    await dialog().getByLabel('Budget de travail (1 à 20 tours)').fill('1.5');
    await dialog().locator('form').evaluate(form => form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })));
    await dialog().getByText('Choisissez un budget entier entre 1 et 20.', { exact: true }).waitFor();
    assert.equal(requests.filter(item => item.method === 'POST').length, postCount + 1);
    await dialog().getByLabel('Budget de travail (1 à 20 tours)').fill('3');await dialog().getByRole('button', { name: 'Redémarrer', exact: true }).click();
    await control('stopped', 'Arrêter').waitFor();
    const fresh = requests.filter(item => item.path.includes('/stopped/commands')).at(-1).body;
    assert.notEqual(fresh.idempotency_key, original.idempotency_key);assert.equal(fresh.budget, 3);

    // A confirmed stop settles earlier local launch requests it superseded.
    await control('stopped', 'Arrêter').click();await dialog().getByRole('button', { name: 'Arrêter', exact: true }).click();
    await control('stopped', 'Arrêt demandé').waitFor();
    stopped.observed_state = 'stopped';stopped.cleanup_confirmed = true;stopped.current_command.status = 'complete';await refresh();
    assert.ok(await control('stopped', 'Démarrer').isEnabled());
    await page.setViewportSize({ width: 390, height: 844 });await noOverflow();
    await page.locator('#notice').waitFor({ state: 'hidden' });
    await page.screenshot({ path: path.join(output, 'hosted-mobile.png'), fullPage: true });
    hosts = [];await page.getByRole('button', { name: 'Actualiser', exact: true }).click();await page.getByRole('button', { name: 'Lier mon serveur', exact: true }).click();
    assert.equal(await dialog().getByLabel('Appareil connecté').locator('option').count(), 1);
    assert.equal(await dialog().getByLabel('Appareil connecté').inputValue(), 'device-own');
    await dialog().getByLabel('Nom du serveur').fill('Mon serveur lié');dropNextGrantReply = true;
    await dialog().getByRole('button', { name: 'Autoriser cet appareil', exact: true }).click();
    await page.locator('#notice').filter({ hasText: 'La réponse n’a pas été confirmée.' }).waitFor();
    const firstGrant = requests.filter(item => item.path === '/api/server-hosts' && item.method === 'POST').at(-1).body;
    await page.reload();await page.getByRole('button', { name: 'Sur le serveur', exact: true }).click();
    await page.locator('.server-pending-host').getByRole('button', { name: 'Réessayer la liaison', exact: true }).click();
    await page.locator('.server-pending-host').waitFor({ state: 'hidden' });
    assert.equal(grantCount, 1);assert.deepEqual(requests.filter(item => item.path === '/api/server-hosts' && item.method === 'POST').at(-1).body, firstGrant);
    await page.getByRole('heading', { name: 'Mon serveur lié', exact: true }).waitFor();
    const grant = requests.filter(item => item.path === '/api/server-hosts' && item.method === 'POST').at(-1).body;
    assert.deepEqual(Object.keys(grant).sort(), ['device_id', 'idempotency_key', 'name']);
    assert.equal(grant.device_id, 'device-own');
    const beforeSwitch = requests.filter(item => item.method === 'POST').length;
    hosts = [host];await page.getByRole('button', { name: 'Actualiser', exact: true }).click();await card('busy').waitFor();
    await page.evaluate(() => { state.actor.owner_id = 'blackoutburst'; });
    await control('busy', 'Démarrer').click();assert.equal(await dialog().count(), 0);
    assert.equal(requests.filter(item => item.method === 'POST').length, beforeSwitch);
    await page.evaluate(() => { state.actor.owner_id = 'thescreamer'; });
    const unavailable = await context.newPage();
    await unavailable.addInitScript(() => { const original = Storage.prototype.setItem; Storage.prototype.setItem = function(key, value) { if (key.startsWith('board-server-requests')) throw new Error('synthetic storage unavailable'); return original.call(this, key, value); }; });
    await unavailable.goto(origin + '/?page=agents');await unavailable.getByRole('button', { name: 'Sur le serveur', exact: true }).click();
    await unavailable.getByText('Instances indisponibles', { exact: true }).waitFor();assert.equal(await unavailable.locator('.server-instance-actions button').count(), 0);
    assert.equal(requests.filter(item => item.method === 'POST').length, beforeSwitch);await unavailable.close();
    assert.ok(!requests.some(item => item.path.startsWith('/api/runtime') && item.method !== 'GET'));
    assert.deepEqual(errors, []);
    fs.writeFileSync(path.join(output, 'result.json'), JSON.stringify({ result: 'passed_synthetic_firefox_fixture',
      checks: ['owner_scope', 'text_rendering', 'observed_vs_requested', 'unknown_review_cleanup', 'queued_restart',
        'lost_response_same_key_body_budget', 'reload_persistence', 'CAS_requires_reload', 'budget_integer_bounds',
        'host_grant_existing_device_only', 'host_grant_lost_reply_persisted', 'owner_switch_refuses_stale_control', 'storage_failure_disables_control', 'stop_supersedes_earlier_requests', 'no_runtime_release', 'desktop_mobile_no_overflow'], model_calls: 0,
      real_agent_processes: 0, production_calls: 0, page_errors: errors, posts: requests.filter(item => item.method === 'POST').length }, null, 2));
  } finally { await browser.close(); }
})().then(() => server.close()).catch(error => { console.error(error); server.close(); process.exitCode = 1; });
