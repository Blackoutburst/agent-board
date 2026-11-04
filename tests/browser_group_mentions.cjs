/* Run against tests/serve_followups_preview.py: isolated local records only. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || '/Users/florianchanson/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const origin = 'http://127.0.0.1:18766';
const output = process.env.BOARD_TEST_OUTPUT;
(async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const context = await browser.newContext({viewport: {width: 1280, height: 900}});
    const login = await (await context.request.post(origin + '/api/login', {
      data: {username: 'blackoutburst', password: 'local-ui-fixture-only'}})).json();
    const headers = {'X-CSRF-Token': login.csrf_token};
    const api = async (route, data) => {
      const response = await context.request.post(origin + '/api' + route, {headers, data});
      assert.equal(response.status(), 200, await response.text()); return response.json();
    };
    const individual = await api('/agent-profiles', {name: 'agent', project_ids: [], idempotency_key: 'reserved-agent-ui'});
    const thread = await api('/threads', {title: 'Group mention browser check',
      participants: ['blackoutburst', 'thescreamer'], idempotency_key: 'group-browser'});
    const page = await context.newPage(), errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(origin + '/?thread=' + thread.id);
    const editor = page.getByLabel('Message', {exact: true});
    await editor.fill('@h');
    const humans = page.getByRole('option').filter({hasText: 'everyone human here'});
    await humans.waitFor(); await humans.click();
    await editor.pressSequentially('Human group browser check');
    await page.getByRole('button', {name: 'Send message', exact: true}).click();
    await page.locator('#messages').getByText('Human group browser check', {exact: false}).waitFor();
    let messages = await (await context.request.get(origin + '/api/threads/' + thread.id)).json();
    assert.deepEqual(messages.messages.find(item => item.body.includes('Human group browser check')).mentions, ['thescreamer']);

    await editor.fill('@a');
    const agents = page.getByRole('option').filter({hasText: 'every agent here'});
    await agents.waitFor(); assert.match(await agents.innerText(), /max 50/);
    if (output) { fs.mkdirSync(output, {recursive: true}); await page.screenshot({path: path.join(output, 'group-mentions-desktop.png')}); }
    await agents.click(); await editor.pressSequentially('Agent group browser check');
    await page.getByRole('button', {name: 'Send message', exact: true}).click();
    await page.locator('#messages').getByText('Agent group browser check', {exact: false}).waitFor();
    messages = await (await context.request.get(origin + '/api/threads/' + thread.id)).json();
    const group = messages.messages.find(item => item.body.includes('Agent group browser check'));
    assert.equal(group.mentions.length, 3);
    assert.ok(group.mentions.every(id => id.startsWith('agent:')));

    await editor.fill('@agent');
    const reserved = page.getByRole('option').filter({has: page.locator('strong', {hasText: /^@agent$/})})
      .filter({hasNotText: 'every agent here'});
    await reserved.waitFor(); await reserved.click();
    assert.equal(await editor.inputValue(), 'agent ');
    await editor.pressSequentially('Specific individual browser check');
    await page.getByRole('button', {name: 'Send message', exact: true}).click();
    await page.locator('#messages').getByText('Specific individual browser check', {exact: false}).waitFor();
    messages = await (await context.request.get(origin + '/api/threads/' + thread.id)).json();
    assert.deepEqual(messages.messages.find(item => item.body.includes('Specific individual browser check')).mentions,
      ['agent:' + individual.id]);

    await editor.fill('@agent'); await reserved.waitFor(); await reserved.click();
    await editor.fill('We should discuss agents tomorrow.');
    await page.getByRole('button', {name: 'Send message', exact: true}).click();
    await page.locator('#messages').getByText('We should discuss agents tomorrow.', {exact: true}).waitFor();
    messages = await (await context.request.get(origin + '/api/threads/' + thread.id)).json();
    assert.deepEqual(messages.messages.find(item => item.body === 'We should discuss agents tomorrow.').mentions, []);

    await editor.fill('@agent'); await reserved.waitFor(); await reserved.click();
    await editor.pressSequentially('The word agent remains ordinary text.');
    await editor.evaluate(input => input.setSelectionRange(0, 6)); await editor.press('Backspace');
    await page.getByRole('button', {name: 'Send message', exact: true}).click();
    await page.locator('#messages').getByText('The word agent remains ordinary text.', {exact: true}).waitFor();
    messages = await (await context.request.get(origin + '/api/threads/' + thread.id)).json();
    assert.deepEqual(messages.messages.find(item => item.body === 'The word agent remains ordinary text.').mentions, []);

    await editor.fill('Hello @agent'); await reserved.waitFor(); await reserved.click();
    await editor.evaluate(input => input.setSelectionRange(0, 0));
    await editor.pressSequentially('Prefix '); await editor.press('End');
    await editor.pressSequentially('Recipient survives unrelated edits.');
    await page.getByRole('button', {name: 'Send message', exact: true}).click();
    await page.locator('#messages').getByText('Recipient survives unrelated edits.', {exact: false}).waitFor();
    messages = await (await context.request.get(origin + '/api/threads/' + thread.id)).json();
    assert.deepEqual(messages.messages.find(item => item.body.includes('Recipient survives unrelated edits.')).mentions,
      ['agent:' + individual.id]);

    await editor.fill('@agent'); await agents.waitFor(); await agents.click();
    await editor.evaluate(input => input.setSelectionRange(6, 6)); await editor.pressSequentially('s');
    await editor.press('End'); await editor.pressSequentially('A plural word is not the group token.');
    await page.getByRole('button', {name: 'Send message', exact: true}).click();
    await page.locator('#messages').getByText('A plural word is not the group token.', {exact: false}).waitFor();
    messages = await (await context.request.get(origin + '/api/threads/' + thread.id)).json();
    assert.deepEqual(messages.messages.find(item => item.body.includes('A plural word is not the group token.')).mentions, []);

    await editor.fill('@agent'); await agents.waitFor(); await agents.click();
    await editor.evaluate(input => input.setSelectionRange(0, 0)); await editor.pressSequentially('@');
    await editor.press('End'); await editor.pressSequentially('A doubled at sign is not the group token.');
    await page.getByRole('button', {name: 'Send message', exact: true}).click();
    await page.locator('#messages').getByText('A doubled at sign is not the group token.', {exact: false}).waitFor();
    messages = await (await context.request.get(origin + '/api/threads/' + thread.id)).json();
    assert.deepEqual(messages.messages.find(item => item.body.includes('A doubled at sign is not the group token.')).mentions, []);

    for (const [kind, option, length] of [['group', agents, 6], ['individual', reserved, 5]]) {
      for (const [side, position] of [['prefix', 0], ['suffix', length]]) {
        for (const letter of ['é', '𝒜']) {
          await editor.fill('@agent'); await option.waitFor(); await option.click();
          await editor.evaluate((input, at) => input.setSelectionRange(at, at), position);
          await editor.pressSequentially(letter); await editor.press('End');
          const marker = `Unicode ${kind} ${side} ${letter} clears its recipient.`;
          await editor.pressSequentially(marker);
          await page.getByRole('button', {name: 'Send message', exact: true}).click();
          await page.locator('#messages').getByText(marker, {exact: false}).waitFor();
          messages = await (await context.request.get(origin + '/api/threads/' + thread.id)).json();
          assert.deepEqual(messages.messages.find(item => item.body.includes(marker)).mentions, [], marker);
        }
      }
    }

    await page.setViewportSize({width: 390, height: 844});
    await editor.fill('@');
    await page.getByRole('option').filter({hasText: 'every agent here'}).waitFor();
    assert.equal(await page.getByRole('option').filter({hasText: 'everyone human here'}).count(), 1);
    const dimensions = await page.evaluate(() => ({page: document.documentElement.scrollWidth, viewport: innerWidth}));
    assert.ok(dimensions.page <= dimensions.viewport + 1, JSON.stringify(dimensions));
    if (output) await page.screenshot({path: path.join(output, 'group-mentions-mobile.png')});
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({pass: true, flows: ['human group picker and exact delivery',
      'agent group picker, cap hint and expanded IDs', 'reserved-name individual selection without group fanout',
      'replacing/deleting a selected occurrence clears its recipient despite ordinary matching words',
      'Unicode and astral-letter prefix/suffix edits clear group and reserved-name selections',
      'mobile picker without overflow'], javascriptErrors: errors}));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exit(1); });
