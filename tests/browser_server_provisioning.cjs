/* Headless Firefox, loopback synthetic metadata only. No provider or agent. */
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), http = require('node:http');
const { firefox } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const web = path.resolve(__dirname, '../web');
const output = process.env.BOARD_TEST_OUTPUT || '/private/tmp/agent-board-provisioning-ui-fixture';
fs.mkdirSync(output, { recursive: true });
const copy = value => JSON.parse(JSON.stringify(value));
const owner = 'thescreamer', device = 'device-own', hash = 'a'.repeat(64);
const profile = (id, name, provider = 'codex_chatgpt') => ({ id, name, owner_id: owner, version: 1, status: 'active',
  preferred_provider: provider, preferred_model: 'requested-only', presence: 'offline', project_ids: [], active_work: [],
  bindings: [{ device_id: device, revoked: false }] });
const profiles = [profile('existing-profile', 'Profil existant'), profile('wanted-profile', 'Mon relecteur <img src=x onerror=alert(1)>', 'claude_code')];
const instance = { id: 'instance1', host_id: 'host1', catalog_key: 'target1', profile_id: 'existing-profile', profile_version: 1,
  launchspec_sha256: hash, mode: 'messages', version: 2, generation: 1, desired_state: 'stopped', observed_state: 'stopped',
  observation_revision: 1, cleanup_confirmed: true, command_id: 'stop1', active: true, can_start: true, control_reason: '',
  last_seen_at: null, reason: '', current_command: { id: 'stop1', instance_id: 'instance1', action: 'stop', status: 'complete',
    generation: 1, launchspec_sha256: hash, budget: 0, instance_version: 2, revision: 2, stop_confirmed: true, seq: 1, created_at: 1000, updated_at: 1000 } };
const host = { id: 'host1', owner_id: owner, device_id: device, name: 'Serveur personnel', version: 1,
  catalog_version: 1, enabled: true, revoked_at: null, instances: [instance] };
let grant = { schema: 'server-provision-draft-grant-view-v1', owner_id: owner, host_id: host.id, device_id: device,
  purpose: 'metadata_request_only_v1', version: 0, enabled: false };
const records = [], calls = [], receipts = new Map();
let nextStatus = 0, malformed = false, requestCount = 0, cancelCount = 0, holdContext = null, contextHeld = false;
const contextFor = p => ({ schema: 'server-provision-draft-context-v1', owner_id: owner, host_id: host.id, device_id: device,
  expected_host_version: host.version, expected_catalog_version: host.catalog_version, expected_instances_sha256: hash,
  grant_version: grant.version, profile_id: p.id, profile_version: p.version, profile_sha256: 'c'.repeat(64),
  provider_id: p.preferred_provider, requested_model: p.preferred_model || null });
function view(body) { return { schema: 'server-provision-draft-view-v1', request_id: 'request-' + (++requestCount), request_sha256: 'b'.repeat(64),
  owner_id: owner, host_id: host.id, device_id: device, revision: 1, state: 'requested', reason: 'requested', barrier_held: true,
  reserved_slots: 1, profile_id: body.profile_id, profile_version: body.profile_version, provider_id: body.provider_id,
  requested_model: body.requested_model, apply_implemented: false, connection_status: 'not_attested',
  model_catalog_status: 'not_attested', model_access_status: 'not_attested', can_cancel: true }; }
function cancel(value) { Object.assign(value, { revision: value.revision + 1, state: 'cancelled', reason: 'cancelled_by_owner', barrier_held: false, reserved_slots: 0, can_cancel: false }); }
function json(res, value, status = 200) { res.writeHead(status, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' }); res.end(JSON.stringify(value)); }
const server = http.createServer(async (req, res) => {
  try {
    const url = new URL(req.url, 'http://127.0.0.1');
    if (url.pathname.startsWith('/api/')) {
      let raw = ''; for await (const part of req) raw += part;
      const body = raw ? JSON.parse(raw) : undefined;
      calls.push({ path: url.pathname, method: req.method, body });
      if (url.pathname === '/api/me') return json(res, { actor: { kind: 'human', owner_id: owner }, csrf_token: 'synthetic', owners: [{ id: owner, display_name: 'TheScreamer' }] });
      if (['/api/threads', '/api/requests', '/api/projects', '/api/agents'].includes(url.pathname)) return json(res, []);
      if (url.pathname === '/api/inbox') return json(res, { count: 0, messages: [], tickets: [], tasks: [] });
      if (url.pathname === '/api/runtime') return json(res, { limits: { owner_id: owner, version: 1, max_concurrent: 1 }, active_count: 0 });
      if (url.pathname === '/api/settings') return json(res, { devices: [{ id: device, owner_id: owner, label: 'Appareil du serveur', revoked: false }] });
      if (url.pathname === '/api/agent-profiles') return json(res, profiles);
      if (url.pathname.startsWith('/api/agent-profiles/')) return json(res, profiles.find(p => p.id === url.pathname.split('/').at(-1)));
      if (url.pathname === '/api/server-hosts') return json(res, { hosts: [host] });
      if (url.pathname === '/api/server-hosts/host1') {
        const snapshot = copy(host); if (records.some(r => r.state === 'requested')) snapshot.instances.forEach(i => { i.can_start = false; i.control_reason = 'authorization_changed'; });
        return json(res, snapshot);
      }
      if (url.pathname === '/api/server-hosts/host1/provisioning-context') {
        const value = contextFor(profiles.find(p => p.id === url.searchParams.get('profile_id')));
        const wait = holdContext; if (wait) { holdContext = null; contextHeld = true; await wait; }
        return json(res, value);
      }
      if (url.pathname === '/api/server-hosts/host1/provisioning-grant' && req.method === 'GET') return json(res, grant);
      if (url.pathname === '/api/server-hosts/host1/provisioning-requests' && req.method === 'GET') {
        const list = { schema: 'server-provision-draft-list-v1', requests: [...records].reverse(), next_before: null };
        if (malformed) list.requests = [{ ...records[0], connection_status: 'connected', extra_secret: 'PRIVATE_SENTINEL' }];
        return json(res, list);
      }
      if (url.pathname.startsWith('/api/server-hosts/host1/provisioning-') && req.method !== 'GET') {
        assert.ok(body.idempotency_key); assert.ok(!Object.keys(body).some(k => /token|secret|path|endpoint|instruction/.test(k)));
        if (nextStatus) { const status = nextStatus; nextStatus = 0; return json(res, { detail: 'PRIVATE_SENTINEL' }, status); }
        const key = req.method + url.pathname + body.idempotency_key;
        if (receipts.has(key)) { const old = receipts.get(key); assert.deepEqual(old.body, body); return json(res, old.value); }
        let result;
        if (url.pathname.endsWith('/provisioning-grant')) {
          assert.equal(req.method, 'PUT'); assert.equal(body.schema, 'server-provision-draft-grant-v1');
          assert.equal(body.expected_host_version, host.version); assert.equal(body.expected_grant_version, grant.version);
          grant = { ...grant, version: grant.version + 1, enabled: body.enabled }; result = grant;
        } else if (url.pathname.endsWith('/cancel')) {
          const value = records.find(r => r.request_id === url.pathname.split('/').at(-2));
          assert.equal(body.schema, 'server-provision-draft-cancel-v1'); assert.equal(body.expected_revision, value.revision);
          cancel(value); cancelCount++; result = value;
        } else {
          assert.equal(body.schema, 'server-provision-draft-request-v1'); assert.ok(grant.enabled);
          const { schema, owner_id, host_id, device_id, ...expected } = contextFor(profiles.find(p => p.id === body.profile_id));
          assert.deepEqual(body, { schema: 'server-provision-draft-request-v1', ...expected, idempotency_key: body.idempotency_key });
          assert.ok(!records.some(r => r.state === 'requested')); result = view(body); records.push(result);
        }
        receipts.set(key, { body: copy(body), value: copy(result) }); return json(res, result);
      }
      return json(res, { detail: 'unexpected_synthetic_request' }, 404);
    }
    const filename = path.resolve(web, '.' + (url.pathname === '/' ? '/index.html' : url.pathname));
    assert.ok(filename.startsWith(web + path.sep));
    if (url.pathname === '/favicon.ico') { res.writeHead(204); return res.end(); }
    res.writeHead(200, { 'Content-Type': filename.endsWith('.js') ? 'application/javascript' : filename.endsWith('.css') ? 'text/css' : 'text/html' }); res.end(fs.readFileSync(filename));
  } catch (error) { console.error(error); json(res, { detail: 'synthetic_error' }, 500); }
});
(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  const browser = await firefox.launch({ headless: true });
  try {
    const browserContext = await browser.newContext({ viewport: { width: 1440, height: 1080 } });
    const page = await browserContext.newPage(), errors = [], checks = [];
    page.on('pageerror', e => errors.push(e.message)); page.on('dialog', () => { throw new Error('Unsafe content'); });
    const section = () => page.locator('[data-provision-host="host1"]'), dialog = () => page.getByRole('dialog');
    const posts = () => calls.filter(c => c.method !== 'GET');
    const go = async () => { await page.goto(origin + '/?page=agents'); await page.getByRole('button', { name: 'Sur le serveur', exact: true }).click(); await section().getByText(/Demandes inactives (non )?autorisées/).waitFor(); };
    const refresh = async () => { await page.getByRole('button', { name: 'Actualiser', exact: true }).click(); await section().getByText(/Demandes inactives (non )?autorisées/).waitFor(); };
    const requestDialog = async () => { await section().getByRole('button', { name: 'Demander la préparation d’un profil', exact: true }).click(); await dialog().getByLabel('Profil souhaité').selectOption('wanted-profile'); await dialog().getByText('Fournisseur demandé : Claude Code', { exact: true }).waitFor(); };
    const send = () => dialog().getByRole('button', { name: 'Enregistrer la demande inactive', exact: true }).click();
    await go(); assert.equal(posts().length, 0);
    assert.ok(await section().getByRole('button', { name: 'Demander la préparation d’un profil', exact: true }).isDisabled());
    await section().getByRole('button', { name: 'Autoriser les demandes', exact: true }).click();
    assert.match(await dialog().innerText(), /distincte du contrôle/); assert.match(await dialog().innerText(), /ne démarre aucun agent/);
    await dialog().getByRole('button', { name: 'Autoriser les demandes inactives', exact: true }).click();
    await section().getByText('Demandes inactives autorisées pour cet appareil.', { exact: true }).waitFor();
    checks.push('separate_disabled_by_default_grant');
    // No implicit profile binding; the UI offers a profile navigation only.
    profiles.forEach(p => { p.bindings = []; }); await refresh();
    await section().getByRole('button', { name: 'Demander la préparation d’un profil', exact: true }).click();
    await dialog().getByText('Il faut un profil actif', { exact: false }).waitFor();
    assert.match(await dialog().innerText(), /confirmez sa liaison/); assert.equal(posts().length, 1);
    await dialog().getByRole('button', { name: 'Connexions du profil : Mon relecteur', exact: false }).click();
    await dialog().getByRole('button', { name: 'Link executor', exact: true }).waitFor();
    assert.equal(await dialog().getByLabel('Connected device', { exact: true }).inputValue(), device);
    assert.equal(posts().length, 1, 'opening existing binding settings never binds silently');
    await dialog().getByRole('button', { name: 'Close dialog', exact: true }).click();
    profiles.forEach(p => { p.bindings = [{ device_id: device, revoked: false }]; }); await refresh();
    checks.push('profile_binding_not_created');
    let drop = true;
    await page.route('**/api/server-hosts/host1/provisioning-requests', async route => {
      if (drop && route.request().method() === 'POST') { drop = false; await route.fetch(); await route.abort('failed'); }
      else await route.continue();
    });
    await requestDialog(); assert.match(await dialog().innerText(), /non vérifiés/); assert.equal(await dialog().locator('img,svg').count(), 0);
    await send(); await page.locator('#notice').filter({ hasText: 'Réponse non confirmée' }).waitFor();
    assert.equal(requestCount, 1); const first = copy(posts().at(-1));
    // Another human action can overtake a lost response. Replayed requested
    // receipt must never replace the current cancelled projection.
    cancel(records[0]); await go(); assert.equal(posts().length, 2, 'reload must not retry');
    await section().getByRole('button', { name: 'Réessayer la même préparation', exact: true }).click();
    await section().getByText('Demande annulée', { exact: true }).waitFor();
    assert.deepEqual(posts().at(-1), first); assert.equal(requestCount, 1);
    assert.equal(await section().getByText('Demandée · inactive', { exact: true }).count(), 0);
    checks.push('lost_response_manual_exact_retry_after_reload', 'historical_receipt_not_current_authority', 'text_only_profile_and_preferences');
    await requestDialog(); await send(); await section().getByText('Demandée · inactive', { exact: true }).waitFor();
    assert.ok(await page.locator('[data-server-instance="instance1"]').getByRole('button', { name: 'Démarrer', exact: true }).isDisabled());
    assert.ok(await page.locator('[data-server-instance="instance1"]').getByRole('button', { name: 'Redémarrer', exact: true }).isDisabled());
    assert.match(await section().innerText(), /Aucun agent n’est préparé ni lancé/);
    assert.ok(!(await section().innerText()).includes('null'));
    await page.locator('#notice').evaluate(node => { node.hidden = true; });
    await page.screenshot({ path: path.join(output, 'requested-desktop.png'), fullPage: true });
    await page.setViewportSize({ width: 390, height: 844 });
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
    await page.screenshot({ path: path.join(output, 'requested-mobile.png'), fullPage: true });
    checks.push('inactive_request_holds_barrier', 'desktop_mobile_no_overflow');
    let dropCancel = true;
    await page.route('**/api/server-hosts/host1/provisioning-requests/*/cancel', async route => {
      if (dropCancel) { dropCancel = false; await route.fetch(); await route.abort('failed'); } else await route.continue();
    });
    await section().getByRole('button', { name: 'Annuler cette demande', exact: true }).click();
    await dialog().getByRole('button', { name: 'Annuler la demande inactive', exact: true }).click();
    await page.locator('#notice').filter({ hasText: 'Réponse non confirmée' }).waitFor(); const originalCancel = copy(posts().at(-1));
    await go(); const beforeRetry = posts().length;
    await section().getByRole('button', { name: 'Réessayer la même préparation', exact: true }).click();
    await section().getByRole('button', { name: 'Demander la préparation d’un profil', exact: true }).waitFor();
    assert.equal(posts().length, beforeRetry + 1); assert.deepEqual(posts().at(-1), originalCancel); assert.equal(cancelCount, 1);
    checks.push('cancellation_cas_manual_exact_retry_no_refill');
    // Conflicts and terminal validation errors require a fresh explicit read.
    await page.setViewportSize({ width: 1440, height: 1080 });
    for (const status of [409, 422]) {
      nextStatus = status; await requestDialog(); await send();
      await page.locator('#notice').filter({ hasText: 'La demande n’est pas acceptée' }).waitFor();
      const refused = copy(posts().at(-1)), count = posts().length;
      await requestDialog(); await send(); await dialog().getByRole('alert').filter({ hasText: 'Une réponse reste à confirmer' }).waitFor();
      assert.equal(posts().length, count); await dialog().getByRole('button', { name: 'Fermer la fenêtre' }).click();
      await refresh(); await requestDialog();
      if (status === 409) {
        nextStatus = 422; await send(); await page.locator('#notice').filter({ hasText: 'La demande n’est pas acceptée' }).waitFor();
        assert.notEqual(posts().at(-1).body.idempotency_key, refused.body.idempotency_key); await refresh();
      } else await dialog().getByRole('button', { name: 'Fermer la fenêtre' }).click();
    }
    checks.push('409_and_422_no_blind_retry_fresh_read_new_key');
    // Account change while a context read is in flight cannot display or send it.
    let release; holdContext = new Promise(resolve => { release = resolve; }); contextHeld = false;
    await section().getByRole('button', { name: 'Demander la préparation d’un profil', exact: true }).click();
    for (let i = 0; !contextHeld && i < 300; i++) await new Promise(resolve => setTimeout(resolve, 5));
    assert.ok(contextHeld); const count = posts().length;
    await page.evaluate(() => { state.actor.owner_id = 'other-owner'; });
    const delivered = page.waitForResponse(r => r.url().includes('/provisioning-context'));
    release(); await delivered; await page.evaluate(() => Promise.resolve());
    assert.ok(await dialog().getByRole('button', { name: 'Enregistrer la demande inactive', exact: true }).isDisabled());
    assert.equal(posts().length, count);
    await page.evaluate(() => { state.actor.owner_id = 'thescreamer'; }); await go();
    checks.push('delayed_context_owner_switch_no_write');
    malformed = true; await page.getByRole('button', { name: 'Actualiser', exact: true }).click();
    await section().getByText('Préparation indisponible.', { exact: false }).waitFor();
    assert.equal(await section().getByRole('button').count(), 0); assert.ok(!(await page.locator('#main').innerText()).includes('PRIVATE_SENTINEL'));
    assert.equal(await page.locator('[data-server-instance="instance1"]').count(), 1);
    checks.push('unknown_fields_attestations_refused_without_hiding_controls');
    assert.deepEqual(errors, []); assert.ok(!calls.some(c => /login|probe|commands|append|apply|register/.test(c.path)));
    fs.writeFileSync(path.join(output, 'result.json'), JSON.stringify({ result: 'passed_synthetic_firefox_fixture', checks,
      posts: posts().length, request_count: requestCount, cancel_count: cancelCount, provider_calls: 0, model_calls: 0,
      real_agent_processes: 0, production_calls: 0, page_errors: errors }, null, 2));
  } finally { await browser.close(); }
})().then(() => server.close()).catch(e => { console.error(e); server.close(); process.exitCode = 1; });
