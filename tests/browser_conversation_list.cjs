/* Synthetic Firefox regression: long conversation previews must stay readable. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { firefox } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const origin = 'http://127.0.0.1:18769';
const output = process.env.BOARD_TEST_OUTPUT || '/private/tmp/board-conversation-list-qa';
fs.mkdirSync(output, { recursive: true });
(async () => {
  const browser = await firefox.launch({ headless: true });
  try {
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, deviceScaleFactor: 2 });
    const login = await (await context.request.post(origin + '/api/login', { data: { username: 'blackoutburst', password: 'local-ui-fixture-only' } })).json();
    const headers = { 'X-CSRF-Token': login.csrf_token };
    const post = async (route, data) => {
      const response = await context.request.post(origin + '/api' + route, { headers, data });
      assert.equal(response.status(), 200, response.status() === 200 ? '' : await response.text()); return response.json();
    };
    const project = await post('/projects', { name: 'Project ' + 'LongName'.repeat(12), participants: ['blackoutburst'], idempotency_key: 'project' });
    const ticket = await post('/projects/' + project.id + '/tickets', { title: 'Ticket with a long preview', idempotency_key: 'ticket' });
    await post('/threads/' + ticket.thread_id + '/messages', { body: 'Ready for review on an isolated branch https://board.example/api/' + 'abcdef0123456789'.repeat(20), idempotency_key: 'preview' });
    const longTitle = 'Conversation ' + 'unbroken'.repeat(22);
    const long = await post('/threads', { title: longTitle, participants: ['blackoutburst'], idempotency_key: 'long' });
    for (let i = 0; i < 30; i++) await post('/threads', { title: 'Conversation ' + i, participants: ['blackoutburst'], idempotency_key: 'thread-' + i });
    const page = await context.newPage(), errors = []; page.on('pageerror', e => errors.push(e.message));
    await page.goto(origin + '/?project=' + project.id);
    await page.getByRole('heading', { name: project.name, exact: true }).waitFor();
    await page.locator('.conversation-nav>summary').click();
    await page.locator('.thread-item').filter({ hasText: longTitle }).first().waitFor({ state: 'attached' });
    const verify = async (label) => {
      const geometry = await page.locator('.thread-list').evaluate(list => ({
        horizontalOverflow: list.scrollWidth > list.clientWidth + 1,
        rows: [...list.querySelectorAll('.thread-item')].map(row => {
          const title = row.querySelector('.thread-name>span'), r = row.getBoundingClientRect(), t = title.getBoundingClientRect();
          const scale = r.width / row.offsetWidth, style = getComputedStyle(title);
          return { width: row.clientWidth, overflow: row.scrollWidth > row.clientWidth + 1,
            leftInside: t.left >= r.left - 1, rightInside: t.right <= r.right + 1,
            boundedTitle: t.height <= 2 * parseFloat(style.lineHeight) * scale + 2 };
        })
      }));
      assert.ok(!geometry.horizontalOverflow, label + ': conversation list must not scroll sideways');
      assert.ok(geometry.rows.length >= 30);
      for (const row of geometry.rows) {
        assert.ok(!row.overflow && row.leftInside && row.rightInside, label + ': card contents stay inside');
        assert.ok(row.boundedTitle, label + ': preview uses at most two lines');
      }
    };
    await verify('desktop');
    await page.locator('.thread-item').filter({ hasText: longTitle }).first().scrollIntoViewIfNeeded();
    await page.screenshot({ path: path.join(output, 'conversations-desktop.png'), fullPage: false });
    await page.setViewportSize({ width: 1000, height: 620 }); await verify('narrow desktop');
    await page.evaluate(() => { document.documentElement.style.zoom = '1.5'; }); await verify('CSS zoom 150%');
    await page.screenshot({ path: path.join(output, 'conversations-zoom.png'), fullPage: false });
    await page.evaluate(() => { document.documentElement.style.zoom = ''; });
    const target = page.locator('.thread-item').filter({ hasText: longTitle }).first(); await target.click();
    await page.getByRole('heading', { name: longTitle, exact: true }).waitFor();
    assert.equal(new URL(page.url()).searchParams.get('thread'), long.id);
    await page.setViewportSize({ width: 390, height: 844 });
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({ browser: 'firefox', deviceScaleFactor: 2, longTitle: true, longTicketPreview: true,
      boundedCards: true, sourceConversationNavigation: true, narrowDesktop: true, cssZoom150: true, mobileNoOverflow: true, errors }));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
