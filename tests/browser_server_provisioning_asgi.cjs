/* Real ASGI/SQLite + headless Firefox, entirely synthetic loopback fixture. */
const assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path');
const { spawn } = require('node:child_process');
const { firefox } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const output = process.env.BOARD_TEST_OUTPUT || '/private/tmp/board-provision-asgi-ui';
fs.mkdirSync(output, { recursive: true });
const python = process.env.BOARD_TEST_PYTHON || 'python3';
const script = path.join(__dirname, 'provisioning_asgi_fixture.py');
const child = spawn(python, ['-I', '-B', script, '--result', path.join(output, 'asgi-result.json')],
  { cwd: path.resolve(__dirname, '..'), env: { PATH: '/usr/bin:/bin' }, stdio: ['ignore', 'pipe', 'pipe'] });
let errors = '', stdout = ''; child.stderr.on('data', b => { errors += b.toString(); if (errors.length > 65536) child.kill('SIGTERM'); });
const exited = new Promise(resolve => child.on('exit', (code, signal) => resolve({ code, signal })));
async function stopFixture() {
  child.kill('SIGTERM'); let timer;
  try { return await Promise.race([exited, new Promise((resolve, reject) => { timer = setTimeout(() => { child.kill('SIGKILL'); reject(new Error('fixture_shutdown_timeout')); }, 10000); })]); }
  finally { clearTimeout(timer); }
}
(async () => {
  const ready = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => { child.kill('SIGTERM'); reject(new Error('fixture_start_timeout')); }, 30000);
    child.stdout.on('data', b => {
      stdout += b.toString();
      if (stdout.length > 8192) { clearTimeout(timer); reject(new Error('unexpected_fixture_output')); return; }
      for (const line of stdout.split('\n').filter(Boolean)) {
        let value; try { value = JSON.parse(line); } catch { continue; }
        if (value.ready === true) { clearTimeout(timer); resolve(value); return; }
      }
    });
    child.once('exit', () => { clearTimeout(timer); reject(new Error('fixture_exited_before_ready')); });
  });
  assert.equal(new URL(ready.origin).hostname, '127.0.0.1');
  const browser = await firefox.launch({ headless: true });
  const checks = [], pageErrors = [];
  try {
    const context = await browser.newContext({ viewport: { width: 1440, height: 1080 } });
    const page = await context.newPage();
    await page.route('**/*', async route => {
      assert.equal(new URL(route.request().url()).origin, ready.origin, 'browser must remain on loopback fixture');
      await route.continue();
    });
    page.on('pageerror', e => pageErrors.push(e.message));
    const login = await context.request.post(ready.origin + '/api/login', { data: { username: 'synthetic-owner', password: 'fixture-password-only' } });
    assert.equal(login.status(), 200); const session = await login.json();
    const snapshot = async () => { const r = await context.request.get(ready.origin + '/__synthetic_snapshot'); assert.equal(r.status(), 200); return r.json(); };
    const before = await snapshot(); assert.equal(before.runtime_rows, 0); assert.equal(before.grant_rows, 0);
    await page.goto(ready.origin + '/?page=agents');
    await page.getByRole('button', { name: 'Sur le serveur', exact: true }).click();
    const section = () => page.locator(`[data-provision-host="${ready.host_id}"]`);
    const card = () => page.locator(`[data-server-instance="${ready.instance_id}"]`);
    const dialog = () => page.getByRole('dialog');
    await section().getByText('Demandes inactives non autorisées pour cet appareil.', { exact: true }).waitFor();
    assert.ok(await card().getByRole('button', { name: 'Démarrer', exact: true }).isEnabled());
    assert.ok(await card().getByRole('button', { name: 'Redémarrer', exact: true }).isEnabled());
    assert.ok(await section().getByRole('button', { name: 'Demander la préparation d’un profil', exact: true }).isDisabled());
    checks.push('real_cookie_csrf_owner_and_current_stop', 'separate_grant_initially_absent');
    await section().getByRole('button', { name: 'Autoriser les demandes', exact: true }).click();
    await dialog().getByRole('button', { name: 'Autoriser les demandes inactives', exact: true }).click();
    await section().getByText('Demandes inactives autorisées pour cet appareil.', { exact: true }).waitFor();
    assert.equal((await snapshot()).grant_rows, 1);
    await section().getByRole('button', { name: 'Demander la préparation d’un profil', exact: true }).click();
    await dialog().getByText('Fournisseur demandé : Codex · ChatGPT', { exact: true }).waitFor();
    assert.equal(await dialog().getByLabel('Profil souhaité').inputValue(), ready.wanted_profile);
    assert.equal(await dialog().getByLabel('Profil souhaité').locator('option').count(), 1);
    await dialog().getByText('Modèle demandé : À choisir ultérieurement', { exact: true }).waitFor();
    await dialog().getByRole('button', { name: 'Enregistrer la demande inactive', exact: true }).click();
    await section().getByText('Demandée · inactive', { exact: true }).waitFor();
    const active = await snapshot(); assert.equal(active.requested, 1); assert.equal(active.barriers, 1);
    assert.equal(active.protected_rows_sha256, before.protected_rows_sha256);
    assert.ok(await card().getByRole('button', { name: 'Démarrer', exact: true }).isDisabled());
    assert.ok(await card().getByRole('button', { name: 'Redémarrer', exact: true }).isDisabled());
    checks.push('ui_context_hashes_real_profile_and_catalogue', 'real_request_inert_and_protected_rows_unchanged', 'ui_start_restart_disabled_by_barrier');
    const current = await context.request.get(ready.origin + '/api/server-hosts/' + ready.host_id);
    const currentInstance = (await current.json()).instances[0];
    for (const action of ['start', 'restart']) {
      const denied = await context.request.post(ready.origin + '/api/server-instances/' + ready.instance_id + '/commands', {
        headers: { 'X-CSRF-Token': session.csrf_token, Origin: ready.origin }, data: { action, expected_version: currentInstance.version,
          expected_generation: currentInstance.generation, launchspec_sha256: currentInstance.launchspec_sha256, budget: 1, idempotency_key: 'synthetic-denied-' + action } });
      assert.equal(denied.status(), 409); assert.equal((await denied.json()).detail, 'server_provisioning_pending');
    }
    assert.equal((await snapshot()).protected_rows_sha256, before.protected_rows_sha256);
    checks.push('real_rest_start_and_restart_denied_without_command');
    await page.locator('#notice').evaluate(node => { node.hidden = true; });
    await page.screenshot({ path: path.join(output, 'asgi-requested-desktop.png'), fullPage: true });
    await page.setViewportSize({ width: 390, height: 844 });
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
    await page.screenshot({ path: path.join(output, 'asgi-requested-mobile.png'), fullPage: true });
    await section().getByRole('button', { name: 'Annuler cette demande', exact: true }).click();
    await dialog().getByRole('button', { name: 'Annuler la demande inactive', exact: true }).click();
    await section().getByText('Demande annulée', { exact: true }).waitFor();
    const final = await snapshot(); assert.equal(final.cancelled, 1); assert.equal(final.requested, 0); assert.equal(final.barriers, 0);
    assert.equal(final.runtime_rows, 0); assert.equal(final.instance_rows, 1); assert.equal(final.command_rows, 1);
    assert.equal(final.protected_rows_sha256, before.protected_rows_sha256);
    assert.ok(await card().getByRole('button', { name: 'Démarrer', exact: true }).isEnabled());
    assert.ok(await card().getByRole('button', { name: 'Redémarrer', exact: true }).isEnabled());
    assert.equal(await section().getByRole('button', { name: 'Annuler cette demande', exact: true }).count(), 0);
    checks.push('real_cancel_releases_only_barrier', 'fresh_projection_reopens_controls_no_start', 'desktop_mobile');
    assert.deepEqual(pageErrors, []);
    fs.writeFileSync(path.join(output, 'browser-result.json'), JSON.stringify({ result: 'passed_real_asgi_synthetic_firefox',
      checks, before, active, final, page_errors: pageErrors, provider_calls: 0, model_calls: 0, real_accounts: 0, production_calls: 0 }, null, 2));
  } finally { await browser.close(); child.kill('SIGTERM'); }
  const ending = await stopFixture();
  assert.equal(ending.code, 0, 'ASGI fixture must shut down cleanly');
  const state = JSON.parse(fs.readFileSync(path.join(output, 'asgi-result.json'), 'utf8'));
  assert.equal(state.outside_network_attempts, 0); assert.equal(state.protected_rows_unchanged, true);
  assert.equal(state.after.runtime_rows, 0); assert.equal(state.after.barriers, 0); assert.equal(state.after.cancelled, 1);
  assert.ok(!state.calls.some(c => /probe|register|append|apply/.test(c.path)));
  assert.equal(errors, '');
})().catch(async e => { await stopFixture(); console.error(e); if (errors) console.error(errors); process.exitCode = 1; });
