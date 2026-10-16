/* Synthetic Firefox acceptance. Never reuse production data or credentials. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { firefox } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const origin = process.env.BOARD_TEST_URL || 'http://127.0.0.1:18767';
if (!['127.0.0.1', 'localhost'].includes(new URL(origin).hostname)) throw new Error('Runtime fixture is loopback-only');
const output = process.env.BOARD_TEST_OUTPUT || '/private/tmp/agent-board-runtime-qa';
fs.mkdirSync(output, { recursive: true });

(async () => {
  const browser = await firefox.launch({ headless: true });
  try {
    const context = await browser.newContext({ viewport: { width: 1440, height: 1080 } });
    const page = await context.newPage();
    const errors = [], runtimeRequests = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('dialog', () => { throw new Error('Unexpected script dialog'); });
    page.on('request', request => { if (new URL(request.url()).pathname === '/api/runtime') runtimeRequests.push(request.url()); });
    const api = (route, data, method = 'GET') => page.evaluate(async ({ route, data, method }) => {
      const response = await fetch(`api${route}`, { method, headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': state.csrf }, body: data === undefined ? undefined : JSON.stringify(data) });
      const value = await response.json();
      if (!response.ok) throw new Error(JSON.stringify(value));
      return value;
    }, { route, data, method });
    const fixture = async (route, method = 'GET') => {
      const response = await page.request.fetch(origin + '/__fixture__/' + route, { method, headers: { 'X-Runtime-Fixture': 'local-only' } });
      assert.equal(response.status(), 200);
      return response.json();
    };
    const login = async (target, owner) => {
      await target.goto(origin);
      await target.getByText('Local development sign-in', { exact: true }).click();
      await target.getByLabel('Username', { exact: true }).fill(owner);
      await target.getByLabel('Password', { exact: true }).fill('local-ui-fixture-only');
      await target.getByRole('button', { name: 'Sign in', exact: true }).click();
      await target.getByRole('heading', { name: 'Projects', exact: true }).waitFor();
      await target.locator('#agents-nav').click();
      await target.getByRole('button', { name: 'Runtime', exact: true }).click();
      await target.getByRole('heading', { name: 'Your runtime limits', exact: true }).waitFor();
    };
    const noOverflow = async target => assert.ok(await target.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), 'No horizontal page overflow');
    await login(page, 'blackoutburst');
    const ids = await fixture('ids');
    const card = variant => page.locator(`[data-execution-id="${ids[variant]}"]`);
    assert.equal(await page.locator('.runtime-quota').innerText(), 'Unavailable');
    assert.equal(await page.locator('.runtime-capacity').innerText(), '3 / 4 occupied');
    assert.match(await card('running').innerText(), /Presence stale/);
    assert.match(await card('finished').innerText(), /Stop confirmed/);
    assert.match(await card('needs_review').innerText(), /Cleanup is unconfirmed/);
    assert.equal(await page.locator('#main img, #main svg').count(), 0);
    assert.match(await card('running').innerText(), /<svg onload=alert\(2\)>/);
    assert.ok(!(await page.locator('#main').innerText()).includes('PRIVATE OTHER OWNER EXECUTOR'));
    assert.equal(await page.locator(`[data-execution-id="${ids.other}"]`).count(), 0);
    const sourceLink = card('message').getByRole('link', { name: 'Source message', exact: true });
    assert.equal(new URL(await sourceLink.getAttribute('href')).searchParams.get('message'), ids.message_source);
    assert.ok(!(await card('message').innerText()).includes('Ticket null'));
    await sourceLink.click();
    await page.locator(`#message-${ids.message_source}.highlight`).waitFor();
    await page.locator('#agents-nav').click();
    await page.getByRole('button', { name: 'Runtime', exact: true }).click();
    await page.getByRole('heading', { name: 'Your runtime limits', exact: true }).waitFor();
    await noOverflow(page);
    await page.screenshot({ path: path.join(output, 'runtime-desktop.png'), fullPage: true });

    // Runtime is a snapshot: unrelated existing inbox refreshes do not reload it.
    const initialReads = runtimeRequests.length;
    await page.waitForTimeout(4500);
    assert.equal(runtimeRequests.length, initialReads);
    await page.getByRole('button', { name: 'Refresh', exact: true }).click();
    await page.getByRole('heading', { name: 'Your runtime limits', exact: true }).waitFor();
    assert.equal(runtimeRequests.length, initialReads + 1);

    await card('running').getByRole('button', { name: 'Request stop', exact: true }).click();
    await card('running').getByRole('button', { name: 'Stop requested', exact: true }).waitFor();
    assert.match(await card('running').innerText(), /Waiting for the executor to confirm cleanup/);
    assert.equal(await card('running').getByText('Stop confirmed', { exact: true }).count(), 0);
    assert.equal(await page.locator('.runtime-capacity').innerText(), '3 / 4 occupied');
    await fixture('finish/' + ids.running, 'POST');
    await page.getByRole('button', { name: 'Refresh', exact: true }).click();
    await card('running').getByText('Stop confirmed', { exact: true }).waitFor();
    assert.equal(await page.locator('.runtime-capacity').innerText(), '2 / 4 occupied');
    assert.equal(await card('running').getByRole('button').count(), 0);

    await page.getByLabel('Maximum concurrent executions', { exact: true }).fill('3');
    await page.getByLabel('Pause participating executors', { exact: true }).check();
    await page.getByRole('button', { name: 'Save runtime limits', exact: true }).click();
    await page.getByText('Paused', { exact: true }).waitFor();
    assert.equal((await api('/runtime')).limits.max_concurrent, 3);
    assert.equal((await api('/runtime')).limits.paused, true);
    assert.equal(await page.locator('.runtime-capacity').innerText(), '2 / 3 occupied');

    // A concurrent edit cannot be overwritten by a stale form.
    let current = await api('/runtime');
    await api('/runtime/limits', { expected_version: current.limits.version, max_concurrent: 4, paused: false }, 'PATCH');
    await page.getByLabel('Maximum concurrent executions', { exact: true }).fill('2');
    await page.getByRole('button', { name: 'Save runtime limits', exact: true }).click();
    await page.locator('.runtime-limits [role="alert"]').waitFor();
    assert.match(await page.locator('.runtime-limits [role="alert"]').innerText(), /changed/);
    assert.equal(await page.getByLabel('Maximum concurrent executions', { exact: true }).inputValue(), '2');
    assert.equal((await api('/runtime')).limits.max_concurrent, 4);
    await page.getByRole('button', { name: 'Refresh', exact: true }).click();
    await page.getByRole('heading', { name: 'Your runtime limits', exact: true }).waitFor();

    // Another stop updates the revision; a stale stop button must not claim success.
    current = await api('/runtime');
    const reserved = current.executions.find(item => item.id === ids.reserved);
    await api(`/runtime/executions/${reserved.id}/stop`, { expected_revision: reserved.revision }, 'POST');
    await card('reserved').getByRole('button', { name: 'Request stop', exact: true }).click();
    await card('reserved').getByRole('alert').waitFor();
    assert.match(await card('reserved').getByRole('alert').innerText(), /changed/);
    await page.getByRole('button', { name: 'Refresh', exact: true }).click();
    await card('reserved').getByRole('button', { name: 'Stop requested', exact: true }).waitFor();

    await page.setViewportSize({ width: 390, height: 844 });
    await noOverflow(page);
    await page.screenshot({ path: path.join(output, 'runtime-mobile.png'), fullPage: true });

    await fixture('block/' + ids.reserved, 'POST');
    await page.getByRole('button', { name: 'Refresh', exact: true }).click();
    await page.getByText('Quota or rate limit reported', { exact: true }).waitFor();
    assert.equal(await page.getByLabel('Pause participating executors', { exact: true }).isChecked(), true);
    assert.match(await page.locator('#main').innerText(), /not a quota measurement/);
    assert.ok(!(await page.locator('.runtime-quota').innerText()).includes('%'));
    const usageIds = await fixture('usage', 'POST');
    const usage = variant => page.locator(`[data-connection-id="${usageIds[variant]}"]`);
    await page.getByRole('button', { name: 'Refresh', exact: true }).click();
    await page.getByRole('heading', { name: 'Usage by connection', exact: true }).waitFor();
    assert.equal(await page.locator('.runtime-usage').count(), 4);
    assert.equal(await page.locator('.runtime-quota').innerText(), 'Quota or rate limit reported');
    assert.equal((await api('/runtime')).limits.paused, true, 'A fresh observation never resumes an executor');
    assert.match(await usage('fresh').innerText(), /72% remaining/);
    assert.match(await usage('fresh').innerText(), /35% remaining/);
    assert.match(await usage('fresh').innerText(), /100% remaining/); // The provider explicitly reported 0 used.
    assert.match(await usage('fresh').innerText(), /Remaining usage unavailable/); // A null window is not 0 used.
    assert.match(await usage('second').innerText(), /10% remaining/);
    assert.equal(await usage('stale').getByText('Stale', { exact: true }).count(), 1);
    assert.match(await usage('stale').innerText(), /45% remaining/);
    assert.match(await usage('stale').innerText(), /Reported reset: .*\(past\)/);
    assert.match(await usage('stale').innerText(), /Observed /);
    assert.equal(await usage('unavailable').getByText('Unavailable', { exact: true }).count(), 1);
    assert.ok(!(await usage('unavailable').innerText()).includes('%'));
    assert.match(await usage('unavailable').innerText(), /Personal connection unavailable/);
    assert.equal(await page.locator(`[data-connection-id="${usageIds.other}"]`).count(), 0);
    assert.equal(await page.locator('#main img, #main svg').count(), 0);
    assert.match(await usage('fresh').innerText(), /<img src=x onerror=alert\(3\)>/);
    assert.match(await page.locator('#main').innerText(), /not a confirmed shared subscription/);
    assert.ok(!(await page.locator('.runtime-quota').innerText()).includes('%'));
    await page.setViewportSize({ width: 1440, height: 1080 });
    await noOverflow(page);
    await page.screenshot({ path: path.join(output, 'usage-desktop.png'), fullPage: true });
    await page.setViewportSize({ width: 390, height: 844 });
    await noOverflow(page);
    await page.screenshot({ path: path.join(output, 'usage-mobile.png'), fullPage: true });
    await page.getByLabel('Pause participating executors', { exact: true }).uncheck();
    await page.getByRole('button', { name: 'Save runtime limits', exact: true }).click();
    await page.locator('.runtime-quota').getByText('See connection observations', { exact: true }).waitFor();
    assert.equal((await api('/runtime')).limits.paused, false);
    await fixture('auth-block/' + ids.needs_review, 'POST');
    await page.getByRole('button', { name: 'Refresh', exact: true }).click();
    await page.locator('.runtime-quota').getByText('Authentication failed', { exact: true }).waitFor();
    assert.equal(await page.getByLabel('Pause participating executors', { exact: true }).isChecked(), true);
    await page.getByRole('heading', { name: 'No active executions', exact: true }).waitFor();
    await page.getByLabel('Pause participating executors', { exact: true }).uncheck();
    await page.getByRole('button', { name: 'Save runtime limits', exact: true }).click();
    await page.locator('.runtime-quota').getByText('See connection observations', { exact: true }).waitFor();

    const otherContext = await browser.newContext({ viewport: { width: 390, height: 844 } });
    const otherPage = await otherContext.newPage();
    otherPage.on('pageerror', error => errors.push(error.message));
    await login(otherPage, 'thescreamer');
    assert.equal(await otherPage.locator('.runtime-execution').count(), 1);
    assert.equal(await otherPage.locator(`[data-execution-id="${ids.other}"]`).count(), 1);
    assert.ok(!(await otherPage.locator('#main').innerText()).includes('Runtime reviewer'));
    assert.equal(await otherPage.locator('.runtime-usage').count(), 1);
    assert.equal(await otherPage.locator(`[data-connection-id="${usageIds.other}"]`).count(), 1);
    assert.ok(!(await otherPage.locator('#main').innerText()).includes('Usage desktop'));
    await noOverflow(otherPage);

    // Client-side expiry changes a label, never fetches or creates authority.
    // Deliberately malformed fixture values must remain text or unavailable.
    const malformed = await api('/runtime');
    const selected = malformed.usage_observations.find(item => item.connection_id === usageIds.fresh);
    selected.expires_at = Date.now() / 1000 + 1;
    selected.data_status = 'observed';
    selected.snapshot.buckets[0].limit_id = '<img src=x onerror=alert(4)>';
    selected.snapshot.buckets[0].normal_model_slug = '<svg onload=alert(5)>' + 'longmodel'.repeat(25);
    selected.snapshot.buckets[0].primary.used_percent = 101;
    selected.snapshot.buckets[1].secondary.used_percent = '0';
    await page.route('**/api/runtime', route => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(malformed) }));
    const beforeExpiryReads = runtimeRequests.length;
    await page.getByRole('button', { name: 'Refresh', exact: true }).click();
    await usage('fresh').getByText('Stale', { exact: true }).waitFor();
    assert.equal(runtimeRequests.length, beforeExpiryReads + 1);
    assert.equal(await usage('fresh').getByText('Remaining usage unavailable', { exact: true }).count(), 3);
    assert.ok(!(await usage('fresh').innerText()).includes('-1%'));
    assert.ok(!(await usage('fresh').innerText()).includes('100%'));
    assert.match(await usage('fresh').innerText(), /<svg onload=alert\(5\)>/);
    assert.equal(await page.locator('#main img, #main svg').count(), 0);
    assert.equal(await page.getByLabel('Pause participating executors', { exact: true }).isChecked(), false);
    await noOverflow(page);
    await page.unroute('**/api/runtime');

    // Failed fetches never replace unavailable data with fictitious zero values.
    await page.route('**/api/runtime', route => route.fulfill({ status: 503, contentType: 'application/json', body: '{"detail":"Fixture unavailable"}' }));
    await page.getByRole('button', { name: 'Refresh', exact: true }).click();
    await page.getByText('Runtime unavailable', { exact: true }).waitFor();
    assert.equal(await page.locator('.runtime-capacity').count(), 0);
    assert.equal(await page.getByRole('button', { name: 'Save runtime limits', exact: true }).count(), 0);
    await noOverflow(page);
    await page.unroute('**/api/runtime');
    await page.getByRole('button', { name: 'Refresh', exact: true }).click();
    await page.getByRole('heading', { name: 'Your runtime limits', exact: true }).waitFor();
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({ browser: 'firefox', desktop: true, mobile: true, text_safe: true,
      manual_refresh: true, stop_confirmation_distinct: true, optimistic_versions: true, owner_isolation: true,
      quota_unavailable: true, observed_provider_blocks: true, connection_observations: true,
      nullable_windows: true, local_expiry_without_fetch: true, observation_does_not_resume: true,
      empty_state: true, runtime_errors: errors.length }));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
