const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawn } = require('node:child_process');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || '/Users/florianchanson/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const origin = process.env.BOARD_TEST_URL || 'http://127.0.0.1:18767';
if (!['127.0.0.1', 'localhost'].includes(new URL(origin).hostname)) throw new Error('Synthetic fixture tests are loopback-only');
const output = process.env.BOARD_TEST_OUTPUT || fs.mkdtempSync(path.join(os.tmpdir(), 'board-requests-check-'));
fs.mkdirSync(output, { recursive: true });

async function signIn(browser, owner) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const page = await context.newPage();
  await page.goto(origin);
  await page.getByText('Local development sign-in', { exact: true }).click();
  await page.getByLabel('Username', { exact: true }).fill(owner);
  await page.getByLabel('Password', { exact: true }).fill('local-ui-fixture-only');
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await page.getByRole('heading', { name: 'Projects', exact: true }).waitFor();
  return { context, page };
}

async function needsYou(page, count) {
  await page.getByRole('button', { name: 'Needs you', exact: false }).click();
  if (count) await page.locator('.human-request').first().waitFor();
  else await page.getByRole('heading', { name: "You're all caught up", exact: true }).waitFor();
  assert.equal(await page.locator('.human-request').count(), count);
  assert.equal(await page.getByLabel('Your response', { exact: true }).count(), count);
  assert.equal(await page.getByText('Waiting on other people', { exact: true }).count(), 0);
  await page.waitForFunction(expected => {
    const badge = document.querySelector('#request-count');
    return expected ? !badge.hidden && Number(badge.textContent) === expected : badge.hidden;
  }, count);
}

(async () => {
  let server, browser;
  let serverErrors = '';
  const pageErrors = [];
  try {
    if (!process.env.BOARD_TEST_URL) {
      const root = path.resolve(__dirname, '..');
      server = spawn(process.env.BOARD_TEST_PYTHON || path.join(root, '.venv/bin/python'), ['tests/serve_requests_preview.py'], {
        cwd: root, env: { ...process.env, PYTHONPATH: root }, stdio: ['ignore', 'ignore', 'pipe'],
      });
      server.stderr.on('data', bytes => { serverErrors += String(bytes); });
      server.on('error', error => { serverErrors += error.message; });
      let ready = false;
      const deadline = Date.now() + 15000;
      while (Date.now() < deadline) {
        if (server.exitCode !== null) throw new Error('Fixture failed: ' + serverErrors);
        try { ready = (await fetch(origin)).ok; } catch {}
        if (ready) break;
        await new Promise(resolve => setTimeout(resolve, 50));
      }
      assert.ok(ready, 'Isolated fixture starts: ' + serverErrors);
    }
    browser = await chromium.launch({ headless: true, args: ['--mute-audio'] });
    const first = await signIn(browser, 'blackoutburst');
    first.page.on('pageerror', error => pageErrors.push(error.message));
    await needsYou(first.page, 1);
    assert.equal(await first.page.getByText('Question only Blackoutburst can answer', { exact: true }).count(), 1);
    assert.equal(await first.page.getByText('Question only TheScreamer can answer', { exact: true }).count(), 0);
    await first.page.screenshot({ path: path.join(output, 'needs-you-first-owner.png'), fullPage: true });

    // Shared task context remains accessible; this is a personal inbox filter,
    // not a change to project membership or the coordinator's request API.
    const shared = await (await first.context.request.get(origin + '/api/human-requests')).json();
    assert.equal(shared.length, 3);
    const secondRequest = shared.find(r => r.question === 'Question only TheScreamer can answer');
    assert.ok(secondRequest);
    await first.page.goto(origin + '/?ticket=' + encodeURIComponent(secondRequest.ticket_id));
    await first.page.getByRole('heading', { name: 'Second owner request', exact: true }).waitFor();
    assert.equal(await first.page.getByText('Waiting for TheScreamer to respond.', { exact: true }).count(), 1);
    assert.equal(await first.page.getByLabel('Your response', { exact: true }).count(), 0);

    const tickets = await (await first.context.request.get(origin + '/api/projects/' + encodeURIComponent(shared[0].project_id) + '/tickets')).json();
    const dependent = tickets.find(t => t.title === 'To do waiting for prerequisite');
    assert.equal(dependent.status, 'todo');
    assert.equal(dependent.work_state, 'blocked');
    await first.page.goto(origin + '/?ticket=' + encodeURIComponent(dependent.id));
    await first.page.getByRole('heading', { name: dependent.title, exact: true }).waitFor();
    assert.equal(await first.page.locator('.task-state-line').innerText().then(t => t.includes('To do · waiting for #4')), true);
    assert.equal(await first.page.getByRole('button', { name: 'Start work', exact: true }).count(), 0);
    await first.page.screenshot({ path: path.join(output, 'dependency-status-desktop.png'), fullPage: true });
    await first.page.setViewportSize({ width: 390, height: 844 });
    assert.ok(await first.page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
    await first.page.screenshot({ path: path.join(output, 'dependency-status-mobile.png'), fullPage: true });
    await first.page.setViewportSize({ width: 1440, height: 1000 });

    await needsYou(first.page, 1);
    await first.page.getByLabel('Your response', { exact: true }).fill('First owner answered the fixture.');
    await first.page.getByRole('button', { name: 'Send answer', exact: true }).click();
    await first.page.getByRole('heading', { name: "You're all caught up", exact: true }).waitFor();
    assert.equal(await first.page.locator('.human-request').count(), 0);
    await first.page.reload();
    await needsYou(first.page, 0);

    const second = await signIn(browser, 'thescreamer');
    second.page.on('pageerror', error => pageErrors.push(error.message));
    await needsYou(second.page, 2);
    assert.equal(await second.page.getByText('Question only TheScreamer can answer', { exact: true }).count(), 1);
    assert.equal(await second.page.getByText('Another question for TheScreamer', { exact: true }).count(), 1);
    assert.equal(await second.page.getByText('Question only Blackoutburst can answer', { exact: true }).count(), 0);
    await second.page.setViewportSize({ width: 390, height: 844 });
    assert.ok(await second.page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
    await second.page.screenshot({ path: path.join(output, 'needs-you-second-owner-mobile.png'), fullPage: true });
    assert.deepEqual(pageErrors, []);
    const result = { pass: true, browser: browser.version(), ownersChecked: 2,
      flows: ['recipient-only inbox', 'matching request count', 'other-recipient shared task context', 'stored status and prerequisite wait', 'answer and empty inbox', 'reload', 'second owner isolation', 'mobile layout'],
      javascriptErrors: pageErrors };
    fs.writeFileSync(path.join(output, 'needs-you-verification.json'), JSON.stringify(result, null, 2) + '\n');
    console.log(JSON.stringify(result));
  } finally {
    if (browser) await browser.close();
    if (server && server.exitCode === null) {
      const stopped = new Promise(resolve => server.once('exit', resolve));
      server.kill('SIGTERM');
      await stopped;
    }
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
