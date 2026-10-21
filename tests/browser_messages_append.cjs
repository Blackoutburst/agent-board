'use strict';
/* All routes fulfilled in memory; synthetic metadata, no TCP/server/provider. */
const assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path');
const { firefox } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const source = path.resolve(__dirname, '..'), owner = 'append-owner', device = 'manager-device', hash = 'a'.repeat(64);
const output = process.env.BOARD_TEST_OUTPUT || '/private/tmp/agent-board-messages-append-ui-qa';
fs.mkdirSync(output, { recursive: true });
const copy = value => JSON.parse(JSON.stringify(value)), calls = [], errors = [], checks = [], receipts = new Map();
const profile = (id, provider = 'codex_chatgpt') => ({ id, owner_id: owner, name: 'Profil ' + id + ' <img src=x onerror=alert(1)>', version: 1,
  status: 'active', preferred_provider: provider, preferred_model: 'requested-model', project_ids: [], active_work: [], bindings: [{ device_id: device, revoked: false }] });
const profiles = [profile('existing'), profile('wanted'), profile('later'), profile('unsupported', 'claude_code')];
const initial = { id: 'old-instance', host_id: 'append-host', catalog_key: 'old-target', profile_id: 'existing', profile_version: 1,
  launchspec_sha256: hash, mode: 'messages', version: 2, generation: 1, desired_state: 'stopped', observed_state: 'stopped',
  observation_revision: 1, reason: '', last_seen_at: null, command_id: 'old-stop', active: true, cleanup_confirmed: true,
  can_start: true, control_reason: '', current_command: { id: 'old-stop', seq: 1, instance_id: 'old-instance', action: 'stop', generation: 1,
    instance_version: 2, launchspec_sha256: hash, budget: 0, status: 'complete', revision: 2, stop_confirmed: true, created_at: 1000, updated_at: 1000 } };
const host = { id: 'append-host', owner_id: owner, device_id: device, name: 'Synthetic append host', version: 1, catalog_version: 1,
  enabled: true, revoked_at: null, instances: [initial] };
let grant = { schema: 'server-messages-append-grant-view-v1', owner_id: owner, host_id: host.id, device_id: device,
  purpose: 'messages_append_v1', version: 0, enabled: false };
const draftGrant = { schema: 'server-provision-draft-grant-view-v1', owner_id: owner, host_id: host.id, device_id: device,
  purpose: 'metadata_request_only_v1', version: 0, enabled: false };
let rows = [], drafts = [], emptySlots = false, malformed = false, dropRequest = false, dropCancel = false, nextStatus = 0;
let holdB = null, bHeld = 0, holdA = null, aHeld = 0, holdContext = null, contextHeld = false, created = 0, cancelled = 0;
const slots = p => [1, 2].map((n) => ({ slot_id: String(n).repeat(64), device_id: 'slot-device-' + n,
  session_id: 'slot-session-' + n, agent_kind: 'codex', identity_key: 'slot-identity-' + n, profile_id: p.id, profile_version: p.version }));
const contextFor = p => ({ schema: 'server-messages-append-context-v1', owner_id: owner, host_id: host.id, device_id: device,
  expected_host_version: host.version, expected_catalog_version: host.catalog_version, expected_instances_sha256: hash,
  grant_version: grant.version, profile_id: p.id, profile_version: p.version, profile_sha256: hash,
  provider_id: p.preferred_provider, requested_model: p.preferred_model, slots: emptySlots ? [] : slots(p) });
const make = body => ({ schema: 'server-messages-append-view-v1', request_id: 'request-' + (++created), request_sha256: hash,
  owner_id: owner, host_id: host.id, device_id: device, revision: 1, state: 'requested', reason: 'requested', barrier_held: true,
  profile_id: body.profile_id, profile_version: body.profile_version, provider_id: body.provider_id, requested_model: body.requested_model,
  slot_id: body.slot_id, can_cancel: true, instance_id: null, proof: null, connection_status: 'not_attested',
  model_catalog_status: 'not_attested', model_access_status: 'not_attested', controller_status: 'not_reported', controller_reason: '' });
function phase(row, state) {
  const revision = { prepared: 2, precommit: 3, committed: 4, complete: 5 }[state], instance = revision >= 4 ? 'new-instance' : null;
  Object.assign(row, { state, reason: state, revision, barrier_held: state !== 'complete', can_cancel: false, instance_id: instance });
  row.proof = { schema: 'server-messages-append-proof-v1', operation_id: '12345678-1234-4123-8123-123456789abc',
    request_id: row.request_id, request_sha256: hash, owner_id: owner, host_id: host.id, device_id: device, grant_version: 1,
    request_revision: revision, expected_host_version: 1, expected_catalog_version: 1, expected_instances_sha256: hash,
    source_release_sha256: hash, old_configuration_sha256: hash, new_configuration_sha256: hash, old_catalog_sha256: hash,
    new_catalog_sha256: hash, bindings_sha256: hash, children_roster_sha256: hash, phase: state, instance_id: instance,
    catalog_version: revision >= 4 ? 2 : null, instances_sha256: revision >= 4 ? hash : null };
}
function draft() { return { schema: 'server-provision-draft-view-v1', request_id: 'legacy-draft', request_sha256: hash, owner_id: owner,
  host_id: host.id, device_id: device, revision: 1, state: 'requested', reason: 'requested', barrier_held: true, reserved_slots: 1,
  profile_id: 'later', profile_version: 1, provider_id: 'codex_chatgpt', requested_model: 'requested-model', can_cancel: true,
  apply_implemented: false, connection_status: 'not_attested', model_catalog_status: 'not_attested', model_access_status: 'not_attested' }; }
(async () => {
  const browser = await firefox.launch({ headless: true });
  try {
    const context = await browser.newContext({ viewport: { width: 1440, height: 1080 }, serviceWorkers: 'block' }), page = await context.newPage();
    page.on('pageerror', e => errors.push(e.message)); page.on('dialog', () => { throw new Error('Unsafe synthetic content'); });
    await context.route('**/*', async route => {
      const req = route.request(), url = new URL(req.url()); assert.equal(url.hostname, 'append-ui.invalid');
      const send = (value, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(value) });
      if (url.pathname.startsWith('/api/')) {
        const body = req.postDataJSON(); calls.push({ path: url.pathname, method: req.method(), body });
        if (url.pathname === '/api/me') return send({ actor: { kind: 'human', owner_id: owner }, csrf_token: 'synthetic', owners: [{ id: owner, display_name: 'Synthetic owner' }] });
        if (url.pathname === '/api/inbox') return send({ count: 0, messages: [], tickets: [], tasks: [] });
        if (url.pathname === '/api/runtime') return send({ limits: { owner_id: owner, version: 1, max_concurrent: 2 }, active_count: 0 });
        if (['/api/agents', '/api/agent-profiles'].includes(url.pathname)) return send(profiles);
        if (url.pathname === '/api/settings') return send({ devices: [{ id: device, owner_id: owner, label: 'Synthetic manager', revoked: false }] });
        if (url.pathname === '/api/server-hosts') return send({ hosts: [(({ instances, ...summary }) => summary)(host)] });
        if (url.pathname === '/api/server-hosts/append-host') return send(host);
        if (url.pathname.endsWith('/provisioning-grant')) return send(draftGrant);
        if (url.pathname.endsWith('/provisioning-requests')) {
          const snapshot = copy(drafts), wait = holdA;
          if (wait) { aHeld++; await wait; }
          return send({ schema: 'server-provision-draft-list-v1', requests: snapshot, next_before: null });
        }
        const base = '/api/server-hosts/append-host/messages-append';
        if (url.pathname.startsWith(base)) {
          const suffix = url.pathname.slice(base.length);
          if (req.method() === 'GET') {
            if (suffix === '/grant') { const value = copy(grant), wait = holdB; if (wait) { bHeld++; await wait; } return send(value); }
            if (suffix === '/requests') return send({ schema: 'server-messages-append-list-v1', requests: malformed ? [{ ...rows.at(-1), connection_status: 'connected', secret: 'PRIVATE_SENTINEL' }] : copy([...rows].reverse()), next_before: null });
            if (suffix === '/context') {
              const value = contextFor(profiles.find(p => p.id === url.searchParams.get('profile_id'))), wait = holdContext;
              if (wait) { contextHeld = true; await wait; }
              return send(value);
            }
            throw new Error('Unexpected append read');
          }
          assert.ok(body.idempotency_key); assert.ok(!Object.keys(body).some(k => /token|secret|path|endpoint|instruction/.test(k)));
          if (nextStatus) { const status = nextStatus; nextStatus = 0; return send({ detail: 'PRIVATE_SENTINEL' }, status); }
          const key = req.method() + url.pathname + body.idempotency_key;
          if (receipts.has(key)) { const old = receipts.get(key); assert.deepEqual(old.body, body); return send(old.result); }
          let result, drop = false;
          if (suffix === '/grant') {
            assert.equal(req.method(), 'PUT'); assert.equal(body.schema, 'server-messages-append-grant-v1');
            assert.equal(body.expected_host_version, host.version); assert.equal(body.expected_grant_version, grant.version);
            grant = { ...grant, version: grant.version + 1, enabled: body.enabled }; result = grant;
          } else if (suffix.endsWith('/cancel')) {
            const row = rows.find(r => r.request_id === suffix.split('/')[2]);
            assert.equal(row.state, 'requested'); assert.equal(body.schema, 'server-messages-append-cancel-v1');
            assert.equal(body.expected_revision, row.revision); Object.assign(row, { revision: 2, state: 'cancelled', reason: 'cancelled_by_owner', can_cancel: false, barrier_held: false });
            result = row; cancelled++; drop = dropCancel; dropCancel = false;
          } else {
            assert.equal(suffix, '/requests'); assert.equal(req.method(), 'POST'); assert.equal(body.schema, 'server-messages-append-request-v1');
            assert.ok(grant.enabled); assert.equal(drafts.length, 0); assert.ok(!rows.some(r => r.barrier_held));
            const { schema, owner_id, host_id, device_id, slots: eligible, ...expected } = contextFor(profiles.find(p => p.id === body.profile_id));
            assert.ok(eligible.some(s => s.slot_id === body.slot_id));
            assert.deepEqual(body, { schema: 'server-messages-append-request-v1', ...expected, slot_id: body.slot_id, idempotency_key: body.idempotency_key });
            result = make(body); rows.push(result); drop = dropRequest; dropRequest = false;
          }
          receipts.set(key, { body: copy(body), result: copy(result) });
          return drop ? route.abort('failed') : send(result);
        }
        assert.equal(req.method(), 'GET', 'No writes outside explicit B routes'); return send([]);
      }
      const file = path.resolve(source, 'web', url.pathname === '/' ? 'index.html' : url.pathname.slice(1));
      assert.ok(file.startsWith(path.join(source, 'web') + path.sep)); if (!fs.existsSync(file)) return route.fulfill({ status: 404, body: '' });
      return route.fulfill({ status: 200, contentType: file.endsWith('.js') ? 'application/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html', body: fs.readFileSync(file) });
    });
    const section = () => page.locator('[data-append-host="append-host"]'), aSection = () => page.locator('[data-provision-host="append-host"]');
    const dialog = () => page.getByRole('dialog'), card = id => page.locator(`[data-server-instance="${id}"]`);
    const start = () => card('old-instance').getByRole('button', { name: 'Démarrer', exact: true });
    const writes = () => calls.filter(c => c.method !== 'GET');
    const ready = () => section().getByText(/Ajouts (non )?autorisés pour cet appareil/).waitFor();
    const go = async () => { await page.goto('https://append-ui.invalid/?page=agents'); await page.getByRole('button', { name: 'Sur le serveur', exact: true }).click(); await ready(); };
    const refresh = async () => { await page.getByRole('button', { name: 'Actualiser', exact: true }).click(); await ready(); };
    const close = () => dialog().getByRole('button', { name: 'Fermer la fenêtre', exact: true }).click();
    const requestDialog = async (id = 'wanted') => { await section().getByRole('button', { name: 'Ajouter un profil déjà appairé', exact: true }).click(); await dialog().getByLabel('Profil à ajouter').selectOption(id); await dialog().getByText('Fournisseur demandé : Codex · ChatGPT', { exact: true }).waitFor(); };
    const submit = () => dialog().getByRole('button', { name: 'Demander l’ajout au serveur', exact: true }).click();
    await go(); assert.equal(writes().length, 0); assert.ok(await section().getByRole('button', { name: 'Ajouter un profil déjà appairé', exact: true }).isDisabled());
    checks.push('separate_B_grant_disabled_by_default_no_render_mutation');
    // A true followed by B false must not clear the existing Start barrier.
    drafts = [draft()]; let releaseB; holdB = new Promise(r => { releaseB = r; }); bHeld = 0;
    await page.getByRole('button', { name: 'Actualiser', exact: true }).click();
    await aSection().getByText('Demandée · inactive', { exact: true }).waitFor(); assert.ok(await start().isDisabled());
    releaseB(); holdB = null; await ready(); assert.ok(await start().isDisabled());
    assert.ok(await section().getByRole('button', { name: 'Ajouter un profil déjà appairé', exact: true }).isDisabled());
    checks.push('A_true_B_false_union_keeps_start_and_B_request_blocked');
    drafts = []; await refresh(); await section().getByRole('button', { name: 'Autoriser les ajouts', exact: true }).click();
    assert.match(await dialog().innerText(), /séparée des demandes inactives/);
    await dialog().getByRole('button', { name: 'Autoriser les ajouts', exact: true }).click(); await ready();
    assert.equal(draftGrant.enabled, false); assert.equal(writes().length, 1); assert.ok(await start().isEnabled());
    checks.push('grant_alone_no_barrier_no_A_grant_promotion');
    emptySlots = true; await requestDialog(); await dialog().getByText('Aucun emplacement personnel appairé', { exact: false }).waitFor();
    assert.ok(await dialog().getByRole('button', { name: 'Demander l’ajout au serveur', exact: true }).isDisabled()); assert.equal(writes().length, 1); await close(); emptySlots = false;
    checks.push('empty_slots_guidance_no_pairing_or_implicit_login');
    let releaseContext; holdContext = new Promise(r => { releaseContext = r; }); contextHeld = false;
    await section().getByRole('button', { name: 'Ajouter un profil déjà appairé', exact: true }).click();
    for (let n = 0; !contextHeld && n < 200; n++) await page.waitForTimeout(5);
    assert.ok(contextHeld); await page.evaluate(() => { state.actor.owner_id = 'foreign-owner'; });
    releaseContext(); holdContext = null; await page.waitForTimeout(30);
    assert.ok(await dialog().getByRole('button', { name: 'Demander l’ajout au serveur', exact: true }).isDisabled()); assert.equal(writes().length, 1);
    await page.evaluate(value => { state.actor.owner_id = value; }, owner); await close(); await refresh();
    checks.push('delayed_context_owner_switch_discards_authority');
    await requestDialog(); assert.equal(await dialog().getByLabel('Profil à ajouter').locator('option[value="existing"]').count(), 0);
    assert.equal(await dialog().getByLabel('Profil à ajouter').locator('option[value="unsupported"]').count(), 0);
    await dialog().getByLabel('Emplacement personnel déjà appairé').selectOption('2'.repeat(64)); await submit(); await ready();
    const requested = rows.at(-1), rowCard = () => section().locator(`[data-append-request="${requested.request_id}"]`);
    await rowCard().getByText('Ajout demandé', { exact: true }).waitFor(); assert.ok(await rowCard().getByRole('button', { name: 'Annuler cet ajout', exact: true }).isEnabled());
    assert.equal(writes().at(-1).body.slot_id, '2'.repeat(64)); assert.ok(await start().isDisabled()); assert.equal(drafts.length, 0);
    checks.push('explicit_profile_slot_model_request_no_A_promotion_or_start');
    dropCancel = true; await rowCard().getByRole('button', { name: 'Annuler cet ajout', exact: true }).click();
    await dialog().getByRole('button', { name: 'Annuler l’ajout', exact: true }).click();
    await page.locator('#notice').filter({ hasText: 'Réponse d’ajout non confirmée' }).waitFor(); const cancelBody = copy(writes().at(-1).body);
    await close(); await go(); await section().getByRole('button', { name: 'Réessayer le même ajout', exact: true }).click(); await ready();
    assert.deepEqual(writes().at(-1).body, cancelBody); assert.equal(cancelled, 1); assert.ok(await start().isEnabled());
    checks.push('cancel_before_effect_manual_exact_retry_after_reply_loss');
    // Private prepare can advance after a lost request response: its receipt is
    // replayed exactly, but only fresh GET controls the displayed phase.
    dropRequest = true; await requestDialog(); await submit();
    await page.locator('#notice').filter({ hasText: 'Réponse d’ajout non confirmée' }).waitFor(); const requestBody = copy(writes().at(-1).body);
    const active = rows.at(-1); phase(active, 'prepared'); await close();
    let releaseA; holdA = new Promise(r => { releaseA = r; }); aHeld = 0;
    await go(); await section().getByText('Préparé localement', { exact: true }).waitFor(); assert.ok(await start().isDisabled());
    releaseA(); holdA = null; await aSection().getByText('Aucune demande enregistrée.', { exact: true }).waitFor(); assert.ok(await start().isDisabled());
    assert.equal(await section().locator(`[data-append-request="${active.request_id}"]`).getByRole('button', { name: 'Annuler cet ajout', exact: true }).count(), 0);
    await section().getByRole('button', { name: 'Réessayer le même ajout', exact: true }).click(); await ready();
    assert.deepEqual(writes().at(-1).body, requestBody); assert.equal(created, 2); await section().getByText('Préparé localement', { exact: true }).waitFor();
    checks.push('B_true_A_false_union_and_exact_lost_request_retry', 'prepared_no_cancel_no_historical_receipt_authority');
    active.controller_status = 'blocked'; active.controller_reason = 'children_uncertain'; await refresh();
    await section().getByText('Une activité précédente doit être rapprochée avant de poursuivre.', { exact: true }).waitFor();
    assert.ok(await start().isDisabled()); assert.equal(active.state, 'prepared');
    active.controller_status = 'not_reported'; active.controller_reason = ''; await refresh();
    checks.push('closed_controller_inability_visible_without_phase_or_cleanup_claim');
    initial.observed_state = 'needs_review'; initial.cleanup_confirmed = false; await refresh();
    assert.ok(await card('old-instance').getByRole('button', { name: 'Arrêter', exact: true }).isEnabled()); assert.ok(await start().isDisabled());
    checks.push('Stop_available_under_B_barrier'); initial.observed_state = 'stopped'; initial.cleanup_confirmed = true;
    await section().getByRole('button', { name: 'Retirer l’autorisation d’ajout', exact: true }).click();
    await dialog().getByRole('button', { name: 'Retirer l’autorisation d’ajout', exact: true }).click(); await ready();
    assert.ok(await start().isDisabled()); assert.equal(active.state, 'prepared'); checks.push('grant_withdrawal_does_not_release_engaged_barrier');
    for (const [stateName, label] of [['precommit','Publication en préparation'],['committed','Instance enregistrée'],['complete','Ajout terminé · instance inactive']]) {
      phase(active, stateName);
      if (stateName === 'complete') {
        host.catalog_version = 2; host.instances.push({ ...copy(initial), id: 'new-instance', catalog_key: 'new-target', profile_id: 'wanted', version: 1, generation: 0,
          observed_state: 'unknown', cleanup_confirmed: false, observation_revision: 0, command_id: null, current_command: null });
      }
      await refresh(); await section().getByText(label, { exact: true }).waitFor();
      assert.equal(await section().locator(`[data-append-request="${active.request_id}"]`).getByRole('button').count(), 0);
      assert.ok(!(await section().innerText()).includes('Connexion fournisseur : connectée'));
    }
    assert.ok(await card('new-instance').getByRole('button', { name: 'Démarrer', exact: true }).isDisabled());
    assert.ok(await card('new-instance').getByRole('button', { name: 'Arrêter', exact: true }).isEnabled());
    checks.push('prepared_precommit_committed_complete_distinct_never_connected_or_started');
    const before = writes().length; malformed = true; await page.getByRole('button', { name: 'Actualiser', exact: true }).click();
    await section().getByText('Ajout indisponible.', { exact: false }).waitFor();
    assert.ok(!(await page.locator('#main').innerText()).includes('PRIVATE_SENTINEL')); assert.equal(writes().length, before);
    assert.equal(await card('new-instance').count(), 1); checks.push('invented_connection_or_unknown_fields_refused_without_hiding_instances');
    malformed = false; await refresh(); await page.setViewportSize({ width: 390, height: 844 });
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
    assert.equal(await page.locator('#main img, #main svg').count(), 0);
    assert.ok(!(await page.locator('#main').innerText()).split('\n').some(line => line === 'null'));
    await page.locator('#notice').evaluate(node => { node.hidden = true; });
    await page.screenshot({ path: path.join(output, 'append-mobile.png'), fullPage: true });
    await page.setViewportSize({ width: 1440, height: 1080 }); await page.screenshot({ path: path.join(output, 'append-desktop.png'), fullPage: true });
    checks.push('escaped_profile_names_desktop_mobile_no_overflow');
    assert.deepEqual(errors, []); assert.ok(writes().every(c => c.path.includes('/messages-append/')));
    assert.ok(!calls.some(c => /login|probe|commands|append\/(prepare|commit|complete)|register/.test(c.path)));
    fs.writeFileSync(path.join(output, 'result.json'), JSON.stringify({ status: 'passed_synthetic_firefox', checks, created, cancelled,
      mutations: writes().length, model_calls: 0, provider_calls: 0, production_calls: 0, tcp_listeners: 0, page_errors: errors }, null, 2) + '\n');
    console.log(JSON.stringify({ status: 'passed', checks: checks.length, created, cancelled, output }));
  } finally { await browser.close(); }
})().catch(e => { console.error(e); process.exitCode = 1; });
