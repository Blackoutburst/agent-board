'use strict';
/* In-memory Firefox routes only; no server, agent, owner login or provider. */
const assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path');
const { firefox } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const source = path.resolve(__dirname, '..'), owner = 'synthetic-budget-owner', hash = 'a'.repeat(64);
const output = process.env.BOARD_TEST_OUTPUT || '/private/tmp/agent-board-budget-stop-ui-qa';
fs.mkdirSync(output, { recursive: true });
const calls = [], errors = [], checks = [];
const make = (id, desired, observed = 'stopped', cleanup = true, action = null, status = 'complete') => {
  const i = { id, host_id: 'budget-host', catalog_key: id, profile_id: 'profile-' + id, profile_version: 1,
    launchspec_sha256: hash, mode: 'messages', version: 1, generation: 1, desired_state: desired,
    observed_state: observed, observation_revision: 1, reason: '', last_seen_at: 1000,
    command_id: null, active: true, cleanup_confirmed: cleanup, can_start: true, control_reason: '', current_command: null };
  if (action) {
    i.command_id = 'initial-' + id;
    i.current_command = { id: i.command_id, seq: 1, instance_id: id, action, generation: 1,
      launchspec_sha256: hash, instance_version: 1, budget: action === 'stop' ? 0 : 1,
      revision: 1, status, stop_confirmed: action === 'stop' && status === 'complete', created_at: 1000, updated_at: 1000 };
  }
  return i;
};
const spent = make('spent', 'running', 'stopped', true, 'start');
const instances = [spent, make('settled', 'stopped', 'stopped', true, 'stop'),
  make('stop-queued', 'stopped', 'stopped', true, 'stop', 'queued'),
  make('stop-accepted', 'stopped', 'stopped', true, 'stop', 'accepted'),
  make('start-pending', 'running', 'stopped', true, 'start', 'queued'),
  make('restart-pending', 'running', 'stopped', true, 'restart', 'accepted'),
  make('unknown', 'running', 'unknown', false), make('review', 'running', 'needs_review', true)];
const host = { id: 'budget-host', owner_id: owner, device_id: 'budget-device', name: 'Synthetic budget host',
  version: 1, catalog_version: 1, revoked_at: null, enabled: true, instances };
const profiles = instances.map(i => ({ id: i.profile_id, owner_id: owner, name: 'Synthetic ' + i.id,
  version: 1, status: 'active', project_ids: [], bindings: [], active_work: [] }));
let conflictNext = false, commandCount = 0;
(async () => {
  const browser = await firefox.launch({ headless: true });
  try {
    const context = await browser.newContext({ viewport: { width: 1440, height: 1080 }, serviceWorkers: 'block' });
    const page = await context.newPage();
    page.on('pageerror', error => errors.push(error.message));
    await context.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url());
      assert.equal(url.hostname, 'budget-stop.invalid');
      const json = (value, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(value) });
      if (url.pathname.startsWith('/api/')) {
        calls.push({ path: url.pathname, method: request.method(), body: request.postDataJSON() });
        if (url.pathname === '/api/me') return json({ actor: { kind: 'human', owner_id: owner }, csrf_token: 'synthetic', owners: [{ id: owner, display_name: 'Synthetic owner' }] });
        if (url.pathname === '/api/inbox') return json({ count: 0, messages: [], tickets: [], tasks: [] });
        if (url.pathname === '/api/runtime') return json({ limits: { owner_id: owner, version: 1, max_concurrent: 1, paused: false }, active_count: 0, executions: [], usage_observations: [] });
        if (['/api/agents', '/api/agent-profiles'].includes(url.pathname)) return json(profiles);
        if (url.pathname === '/api/settings') return json({ devices: [{ id: 'budget-device', owner_id: owner, label: 'Synthetic', revoked: false }] });
        if (url.pathname === '/api/server-hosts') return json({ hosts: [(({ instances, ...summary }) => summary)(host)] });
        if (url.pathname === '/api/server-hosts/budget-host') return json(host);
        if (url.pathname === '/api/server-instances/spent/commands' && request.method() === 'POST') {
          const body = request.postDataJSON();
          assert.equal(body.action, 'stop'); assert.equal(body.budget, 0);
          assert.equal(body.expected_version, spent.version); assert.equal(body.expected_generation, spent.generation);
          assert.equal(body.launchspec_sha256, hash); assert.equal(typeof body.idempotency_key, 'string');
          if (conflictNext) { conflictNext = false; return json({ detail: 'server_instance_changed' }, 409); }
          commandCount++; spent.version++; spent.desired_state = 'stopped'; spent.command_id = 'requested-stop-' + commandCount;
          spent.current_command = { id: spent.command_id, seq: 2, instance_id: spent.id, action: 'stop', generation: spent.generation,
            launchspec_sha256: hash, instance_version: spent.version, budget: 0, revision: 1, status: 'queued',
            stop_confirmed: false, created_at: 1001, updated_at: 1001 };
          return json(spent.current_command);
        }
        if (request.method() !== 'GET') throw new Error('Unexpected synthetic mutation');
        if (url.pathname.includes('provisioning')) return json({ detail: 'not_in_this_fixture' }, 404);
        return json([]);
      }
      const file = path.resolve(source, 'web', url.pathname === '/' ? 'index.html' : url.pathname.slice(1));
      assert.ok(file.startsWith(path.join(source, 'web') + path.sep));
      if (!fs.existsSync(file)) return route.fulfill({ status: 404, body: '' });
      return route.fulfill({ status: 200, contentType: file.endsWith('.js') ? 'application/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html', body: fs.readFileSync(file) });
    });
    const card = id => page.locator(`[data-server-instance="${id}"]`);
    const button = (id, label) => card(id).getByRole('button', { name: label, exact: true });
    const dialog = () => page.getByRole('dialog');
    const posts = () => calls.filter(c => c.method === 'POST');
    const reads = () => calls.filter(c => c.path === '/api/server-hosts/budget-host').length;
    const refresh = async () => { await page.getByRole('button', { name: 'Actualiser', exact: true }).click(); await card('spent').waitFor(); };
    await page.goto('https://budget-stop.invalid/?page=agents');
    await page.getByRole('button', { name: 'Sur le serveur', exact: true }).click(); await card('spent').waitFor();
    assert.ok(await button('spent', 'Arrêter').isEnabled());
    assert.ok(await button('settled', 'Arrêter').isDisabled());
    for (const id of ['stop-queued', 'stop-accepted']) assert.ok(await button(id, 'Arrêt demandé').isDisabled());
    for (const id of ['start-pending', 'restart-pending', 'unknown', 'review']) assert.ok(await button(id, 'Arrêter').isEnabled());
    checks.push('running_clean_stop_enabled', 'stopped_clean_stop_disabled', 'remote_stop_pending_stays_disabled', 'other_command_and_uncertainty_guards_unchanged');
    await refresh(); assert.equal(posts().length, 0);
    await button('spent', 'Arrêter').click(); assert.equal(posts().length, 0);
    assert.equal(await dialog().getByLabel('Budget de travail (1 à 20 tours)').count(), 0);
    checks.push('render_refresh_and_dialog_never_send_command');
    const beforeConflictReads = reads(); conflictNext = true;
    await dialog().getByRole('button', { name: 'Arrêter', exact: true }).click();
    await page.locator('#notice').filter({ hasText: 'L’état a changé. Actualisez les instances' }).waitFor();
    assert.equal(posts().length, 1); assert.equal(reads(), beforeConflictReads); const oldBody = posts()[0].body;
    await button('spent', 'Arrêter').click(); await dialog().getByRole('button', { name: 'Arrêter', exact: true }).click();
    await dialog().getByRole('alert').waitFor(); assert.equal(posts().length, 1);
    await dialog().getByRole('button', { name: 'Fermer la fenêtre', exact: true }).click();
    checks.push('cas_conflict_requires_manual_fresh_read_without_resend');
    await refresh(); await button('spent', 'Arrêter').click();
    await dialog().getByRole('button', { name: 'Arrêter', exact: true }).click();
    await button('spent', 'Arrêt demandé').waitFor(); assert.ok(await button('spent', 'Arrêt demandé').isDisabled());
    assert.equal(posts().length, 2); assert.notEqual(posts()[1].body.idempotency_key, oldBody.idempotency_key);
    assert.equal(commandCount, 1); assert.equal(posts()[1].body.budget, 0);
    spent.current_command.status = 'accepted'; await refresh(); assert.ok(await button('spent', 'Arrêt demandé').isDisabled());
    checks.push('explicit_stop_budget_zero_new_key_after_read', 'accepted_stop_stays_disabled_with_clean_observation');
    spent.current_command.status = 'complete'; spent.current_command.stop_confirmed = true; await refresh();
    assert.ok(await button('spent', 'Arrêter').isDisabled());
    await page.setViewportSize({ width: 390, height: 844 });
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
    assert.ok(await button('start-pending', 'Arrêter').isEnabled());
    checks.push('completed_stop_disabled', 'mobile_controls_no_overflow');
    assert.equal(posts().length, 2); assert.deepEqual(errors, []);
    fs.writeFileSync(path.join(output, 'result.json'), JSON.stringify({ status: 'passed_synthetic_headless_firefox', checks,
      synthetic_command_posts: 2, accepted_synthetic_commands: 1, model_calls: 0, provider_calls: 0,
      production_calls: 0, tcp_listeners: 0, page_errors: errors }, null, 2) + '\n');
    console.log(JSON.stringify({ status: 'passed', checks: checks.length, output }));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
