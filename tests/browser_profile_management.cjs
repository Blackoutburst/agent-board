/* Real Firefox + synthetic loopback Board; never reads production accounts. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { firefox } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const origin = process.env.BOARD_TEST_URL || 'http://127.0.0.1:18765';
if (!['127.0.0.1', 'localhost'].includes(new URL(origin).hostname)) throw new Error('Synthetic loopback fixtures only');
const output = process.env.BOARD_TEST_OUTPUT || '/private/tmp/board-lead-ui';
fs.mkdirSync(output, { recursive: true });

(async () => {
  const browser = await firefox.launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1080 } });
    const errors = [], mutations = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('request', request => {
      if (request.url().includes('/api/agent-profiles') && ['POST', 'PATCH', 'PUT'].includes(request.method())) mutations.push(request);
    });
    await page.goto(origin);
    await page.getByText('Local development sign-in', { exact: true }).click();
    await page.getByLabel('Username', { exact: true }).fill('blackoutburst');
    await page.getByLabel('Password', { exact: true }).fill(process.env.BOARD_TEST_PASSWORD || 'local-ui-fixture-only');
    await page.getByRole('button', { name: 'Sign in', exact: true }).click();
    await page.getByRole('heading', { name: 'Projects', exact: true }).waitFor();
    await page.locator('#agents-nav').click();
    const dialog = () => page.getByRole('dialog');
    const grant = () => dialog().getByLabel('Gérer les profils d’agents ordinaires', { exact: true });
    const prepare = async name => {
      await page.getByRole('button', { name: 'New profile', exact: true }).first().click();
      await dialog().getByLabel('Start from a template', { exact: true }).selectOption('management');
      assert.equal(await dialog().getByLabel('Role', { exact: true }).inputValue(), 'Management');
      assert.equal(await grant().isChecked(), false, 'The descriptive template grants nothing');
      await dialog().getByLabel('Profile name', { exact: true }).fill(name);
      await dialog().getByLabel(process.env.BOARD_TEST_PROJECT || 'Board overhaul', { exact: true }).check();
      await dialog().getByText('Profile permissions', { exact: true }).click();
      await dialog().getByLabel('Read selected projects and discussions', { exact: true }).check();
      await dialog().getByLabel('Create, claim and update work', { exact: true }).check();
      await dialog().getByLabel('Work selection', { exact: true }).selectOption('requested');
      await grant().check();
    };
    const readProfiles = () => page.evaluate(async () => await (await fetch('/api/agent-profiles')).json());
    const prefix = `Lead UI ${Date.now()}`;
    await prepare(prefix);
    assert.equal(await dialog().evaluate(node => [...node.childNodes].some(n => n.nodeType === 3 && n.textContent.includes('null'))), false);
    for (const [width, height, label] of [[1440, 1080, 'desktop'], [390, 844, 'mobile']]) {
      await page.setViewportSize({ width, height });
      await grant().scrollIntoViewIfNeeded();
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
      await page.screenshot({ path: path.join(output, `${label}.png`), fullPage: true });
    }
    await dialog().getByRole('button', { name: 'Create profile', exact: true }).click();
    await dialog().waitFor({ state: 'hidden' });
    const normal = (await readProfiles()).find(p => p.name === prefix);
    assert.ok(normal); assert.equal(normal.bindings.length, 0); assert.equal(normal.version, 2);
    assert.deepEqual(normal.policy.actions, ['agent.profile.read', 'agent.profile.create', 'agent.profile.update', 'agent.profile.archive']);
    assert.deepEqual(normal.policy.resources, ['agent-profiles:blackoutburst']);

    for (const mode of ['before-write', 'after-write', 'malformed-ack']) {
      let grantCalls = 0;
      const handler = async route => {
        grantCalls++;
        if (mode !== 'before-write') {
          const actual = await route.fetch();
          assert.equal(actual.status(), 200, 'The actual grant must commit before its response is lost');
        }
        await route.fulfill(mode === 'malformed-ack' ? { status: 200, contentType: 'application/json', body: '{}' }
          : { status: 503, contentType: 'application/json', body: JSON.stringify({ detail: `Synthetic ${mode} response loss` }) });
      };
      await page.route('**/api/agent-profiles/*/management', handler);
      const name = `${prefix} ${mode}`;
      await prepare(name);
      await dialog().getByRole('button', { name: 'Create profile', exact: true }).click();
      await dialog().getByRole('heading', { name: 'Délégation à vérifier', exact: true }).waitFor();
      const alert = dialog().getByRole('alert');
      await alert.waitFor({ state: 'visible' });
      assert.match(await alert.textContent(), /Profil enregistré ; délégation non confirmée/);
      assert.equal(await dialog().getByRole('button', { name: 'Create profile', exact: true }).count(), 0, 'The stale form cannot retry creation');
      await page.screenshot({ path: path.join(output, `${mode}-recovery.png`), fullPage: true });
      assert.equal(grantCalls, 1);
      await page.unroute('**/api/agent-profiles/*/management', handler);
      await dialog().getByRole('button', { name: 'Recharger le profil', exact: true }).click();
      await dialog().getByRole('heading', { name: 'Edit agent profile', exact: true }).waitFor();
      assert.equal(await grant().isChecked(), mode !== 'before-write', 'Reload uses actual persisted grant state');
      const current = (await readProfiles()).filter(p => p.name === name);
      assert.equal(current.length, 1, 'No duplicate profile is created after an uncertain grant');
      assert.equal(current[0].version, mode !== 'before-write' ? 2 : 1);
      await dialog().getByText(`Editing configuration v${current[0].version}. Saved preferences apply to future work; existing work keeps its captured configuration.`, { exact: true }).waitFor();
      await dialog().getByRole('button', { name: 'Cancel', exact: true }).click();
    }
    assert.equal(mutations.filter(r => r.method() === 'POST').length, 4);
    assert.equal(mutations.filter(r => r.method() === 'PUT').length, 4, 'No automatic grant retry');
    assert.equal(mutations.filter(r => r.method() === 'PATCH').length, 0);
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({ status: 'passed', browser: 'Firefox', cases: ['human-grant', 'before-write-loss', 'after-write-loss', 'malformed-ack'], desktopMobileNoOverflow: true, noBlindRetry: true }));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
