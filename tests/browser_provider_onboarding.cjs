/* Firefox against synthetic metadata only. No provider, login or agent runs. */
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), http = require('node:http');
const { firefox } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const web = path.resolve(__dirname, '../web');
const output = process.env.BOARD_TEST_OUTPUT || '/private/tmp/agent-board-provider-onboarding-ui';
fs.mkdirSync(output, { recursive: true });
const owner = 'thescreamer', profiles = [], requests = [], receipts = new Map();
let host = { id: 'host-owner-initial-setup', owner_id: owner, device_id: 'own-device', name: 'Mon serveur personnel',
  version: 1, catalog_version: 0, revoked_at: null, enabled: true, instances: [], last_seen_at: null };
let malformed = false;
const catalog = [
  { id: 'codex_chatgpt', name: 'Codex · ChatGPT', text_adapter_implemented: true,
    documentation_url: 'https://learn.chatgpt.com/docs/auth' },
  { id: 'claude_code', name: 'Claude Code', text_adapter_implemented: false,
    documentation_url: 'https://code.claude.com/docs/en/authentication' }
].map(item => ({ ...item, billing: 'personal_subscription', personal_login: 'official_owner_cli',
  board_login_implemented: false, connection_status: 'not_attested', model_catalog_status: 'not_attested', confirmed_models: [] }));
function overview(profileId) {
  const p = profiles.find(item => item.id === profileId);
  const result = { schema_version: 1, owner_id: owner, providers: catalog,
    host: host ? { ...host, published_targets: [] } : null,
    requested: p ? { profile_id: p.id, profile_version: p.version, name: p.name, provider: p.preferred_provider, model: p.preferred_model } : null,
    target_registration: p ? 'not_published' : 'no_profile',
    connection: { status: malformed ? 'connected' : 'not_attested', provider_id: null, observed_at: null },
    models: { catalog_status: 'not_attested', access_status: 'not_attested', models: [] },
    provisioning: { board_provisioning_implemented: false, board_login_implemented: false, isolated_owner_required: true, api_fallback: false } };
  return result;
}
function json(res, value, status = 200) { res.writeHead(status, { 'Content-Type': 'application/json' }); res.end(JSON.stringify(value)); }
const server = http.createServer(async (req, res) => {
  try {
    const url = new URL(req.url, 'http://127.0.0.1');
    if (url.pathname.startsWith('/api/')) {
      let text = ''; for await (const part of req) text += part;
      const body = text ? JSON.parse(text) : undefined;
      requests.push({ path: url.pathname, method: req.method, body });
      if (url.pathname === '/api/me') return json(res, { actor: { kind: 'human', owner_id: owner }, csrf_token: 'synthetic-csrf', owners: [{ id: owner, display_name: 'TheScreamer' }] });
      if (['/api/threads', '/api/requests', '/api/projects', '/api/agents'].includes(url.pathname)) return json(res, []);
      if (url.pathname === '/api/inbox') return json(res, { count: 0, messages: [], tickets: [], tasks: [] });
      if (url.pathname === '/api/settings') return json(res, { devices: [{ id: 'own-device', owner_id: owner, label: 'Ubuntu personnel', revoked: false }] });
      if (url.pathname === '/api/server-hosts') return json(res, { hosts: host ? [host] : [] });
      if (url.pathname === '/api/server-hosts/host-owner-initial-setup') return json(res, host);
      if (url.pathname === '/api/provider-onboarding') return json(res, overview(url.searchParams.get('profile_id')));
      if (url.pathname === '/api/agent-profiles' && req.method === 'GET') return json(res, profiles);
      if (url.pathname === '/api/agent-profiles' && req.method === 'POST') {
        assert.ok(body.idempotency_key);
        if (receipts.has(body.idempotency_key)) {
          const stored = receipts.get(body.idempotency_key); assert.deepEqual(stored.body, body); return json(res, stored.profile);
        }
        const profile = { ...body, id: 'created-profile-'+(profiles.length+1), owner_id: owner, version: 1, status: 'active',
          legacy: false, bindings: [], active_work: [], presence: 'offline', project_ids: body.project_ids || [] };
        profiles.push(profile); receipts.set(body.idempotency_key, { body, profile }); return json(res, profile);
      }
      return json(res, { detail: 'unexpected_synthetic_request' }, 404);
    }
    const filename = path.resolve(web, '.'+(url.pathname === '/' ? '/index.html' : url.pathname));
    assert.ok(filename.startsWith(web+path.sep));
    if (url.pathname === '/favicon.ico') { res.writeHead(204); return res.end(); }
    const content = fs.readFileSync(filename);
    res.writeHead(200, { 'Content-Type': filename.endsWith('.js') ? 'application/javascript' : filename.endsWith('.css') ? 'text/css' : 'text/html', 'Cache-Control': 'no-store' });
    res.end(content);
  } catch (error) { console.error(error); json(res, { detail: 'synthetic_error' }, 500); }
});
(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  const browser = await firefox.launch({ headless: true });
  try {
    const context = await browser.newContext({ viewport: { width: 1440, height: 1080 } });
    const page = await context.newPage(), errors = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('dialog', () => { throw new Error('Unsafe text created a browser dialog'); });
    const dialog = () => page.getByRole('dialog');
    const go = async () => { await page.goto(origin+'/?page=agents'); await page.getByRole('button', { name: 'Sur le serveur', exact: true }).click(); await page.getByRole('button', { name: 'Préparer un agent serveur', exact: true }).waitFor(); };
    await go();
    await page.getByText('Configurer le gestionnaire initial', { exact: true }).click();
    assert.equal(await page.getByLabel('Identifiant de l’hôte pour le gestionnaire').inputValue(), host.id);
    assert.equal(await page.getByLabel('Identifiant de l’hôte pour le gestionnaire').getAttribute('readonly'), '');
    await page.getByRole('button', { name: 'Préparer un agent serveur', exact: true }).click();
    await dialog().getByLabel('Nom de l’agent').fill('Mon relecteur <img src=x onerror=alert(1)>');
    await dialog().getByLabel('Modèle demandé (facultatif)').fill('requested-not-verified');
    assert.match(await dialog().innerText(), /aucun processus/);
    assert.equal(await dialog().getByLabel('Fournisseur demandé').locator('option').count(), 2);
    await dialog().getByLabel('Fournisseur demandé').selectOption('claude_code');
    await dialog().getByText('Claude Code ne peut pas encore être utilisé', { exact: false }).waitFor();
    await dialog().getByLabel('Fournisseur demandé').selectOption('codex_chatgpt');
    assert.equal(requests.filter(r => r.method === 'POST').length, 0);
    await dialog().getByRole('button', { name: 'Personnaliser le profil', exact: true }).click();
    assert.equal(await dialog().getByLabel('Preferred provider', { exact: true }).inputValue(), 'codex_chatgpt');
    assert.equal(await dialog().getByLabel('Preferred model', { exact: true }).inputValue(), 'requested-not-verified');
    let drop = true;
    await page.route('**/api/agent-profiles', async route => {
      if (drop && route.request().method() === 'POST') { drop = false; await route.fetch(); await route.abort('failed'); }
      else await route.continue();
    });
    await dialog().getByRole('button', { name: 'Create profile', exact: true }).click();
    await dialog().getByRole('alert').waitFor();
    assert.equal(profiles.length, 1);
    await dialog().getByRole('button', { name: 'Create profile', exact: true }).click();
    await dialog().getByText('Connexion fournisseur : non vérifiée.', { exact: false }).waitFor();
    assert.equal(profiles.length, 1);
    const posts = requests.filter(r => r.method === 'POST'); assert.equal(posts.length, 2); assert.deepEqual(posts[0].body, posts[1].body);
    assert.equal(profiles[0].preferred_model, 'requested-not-verified');
    assert.equal(profiles[0].policy.allow_read, false); assert.equal(profiles[0].policy.allow_messages, false);
    assert.equal(await dialog().locator('img, svg').count(), 0);
    assert.match(await dialog().innerText(), /Codex · ChatGPT/);
    assert.ok(!(await dialog().innerText()).includes('codex_chatgpt'));
    assert.ok(!(await dialog().innerText()).includes('Connecté'));
    await page.screenshot({ path: path.join(output, 'prepared-desktop.png'), fullPage: true });
    await page.setViewportSize({ width: 390, height: 844 });
    await dialog().getByText('Configurer le gestionnaire initial', { exact: true }).click();
    assert.equal(await dialog().getByLabel('Identifiant de l’hôte pour le gestionnaire').inputValue(), host.id);
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth+1));
    await page.screenshot({ path: path.join(output, 'prepared-mobile.png'), fullPage: true });
    await dialog().getByRole('button', { name: 'Voir mes instances serveur', exact: true }).click();
    await page.getByRole('button', { name: 'Préparer un agent serveur', exact: true }).click();
    await dialog().getByLabel('Nom de l’agent').fill('Must not cross owner');
    await page.evaluate(() => { state.actor.owner_id = 'blackoutburst'; });
    await dialog().getByRole('button', { name: 'Personnaliser le profil', exact: true }).click();
    assert.equal(requests.filter(r => r.method === 'POST').length, 2);
    malformed = true; await go(); await page.getByRole('button', { name: 'Préparer un agent serveur', exact: true }).click();
    await page.locator('#notice').filter({ hasText: 'Les informations de préparation sont indisponibles.' }).waitFor();
    assert.equal(await dialog().count(), 0);
    malformed = false; host = null; await go(); await page.getByRole('button', { name: 'Préparer un agent serveur', exact: true }).click();
    await dialog().getByText('Aucun hôte lié à votre compte.', { exact: false }).waitFor();
    assert.deepEqual(errors, []);
    assert.deepEqual(requests.filter(r => /login|probe|commands/.test(r.path)
      || (/runtime/.test(r.path) && !(r.method === 'GET' && r.path === '/api/runtime'))), []);
    fs.writeFileSync(path.join(output, 'result.json'), JSON.stringify({ result: 'passed_synthetic_firefox_fixture', checks: [
      'host_id_visible_readonly', 'two_closed_providers', 'no_invented_model_list', 'requested_not_connected',
      'claude_adapter_unavailable', 'existing_profile_editor', 'lost_response_same_key_body', 'profile_no_permissions',
      'text_no_xss', 'desktop_mobile_no_overflow', 'owner_switch_no_write', 'malformed_attestation_refused',
      'missing_host_guidance', 'no_login_runtime_mutation_or_start'], posts: posts.length,
      capacity_reads: requests.filter(r => r.method === 'GET' && r.path === '/api/runtime').length,
      model_calls: 0, real_agent_processes: 0,
      provider_calls: 0, production_calls: 0, page_errors: errors }, null, 2));
  } finally { await browser.close(); }
})().then(() => server.close()).catch(error => { console.error(error); server.close(); process.exitCode = 1; });
