/* Firefox coordinated-work acceptance with disposable loopback accounts only. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { firefox } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const origin = 'http://127.0.0.1:18770';
const output = process.env.BOARD_TEST_OUTPUT || '/private/tmp/board-family-ui-qa';
fs.mkdirSync(output, { recursive: true });
(async () => {
  const browser = await firefox.launch({ headless: true });
  try {
    const owner = await browser.newContext({ viewport: { width: 1440, height: 1000 }, deviceScaleFactor: 2 });
    const other = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    for (const [context, username] of [[owner, 'blackoutburst'], [other, 'thescreamer']]) {
      const response = await context.request.post(origin + '/api/login', { data: { username, password: 'local-ui-fixture-only' } });
      assert.equal(response.status(), 200);
    }
    const fixture = async (route, method = 'GET') => {
      const response = await owner.request.fetch(origin + '/__fixture__/' + route, { method, headers: { 'X-Family-Fixture': 'local-only' } });
      assert.equal(response.status(), 200); return response.json();
    };
    const ids = await fixture('ids'), page = await owner.newPage(), errors = [], stops = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('dialog', () => { throw new Error('Unexpected script dialog'); });
    page.on('request', request => { if (request.method() === 'POST' && /\/delegation-families\/[^/]+\/stop$/.test(new URL(request.url()).pathname)) stops.push({ url: request.url(), data: request.postDataJSON() }); });
    const card = () => page.locator('.delegation-card');
    const open = async name => {
      await page.goto(origin + '/?ticket=' + ids[name].parent_ticket_id);
      await card().getByRole('heading', { name: 'Travail coordonné', exact: true }).waitFor();
    };
    const noOverflow = async target => assert.ok(await target.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
    const noPrivate = async target => {
      const text = await target.locator('.delegation-card').innerText();
      assert.ok(!text.includes('PRIVATE_FAMILY_PLAN') && !text.includes('private-part') && !text.includes('a'.repeat(64)));
      assert.equal(await target.locator('.delegation-card img, .delegation-card svg').count(), 0);
    };
    await open('active');
    assert.equal(await card().locator('.delegation-member').count(), 3);
    assert.equal(await card().locator('.delegation-member').first().innerText().then(text => /Modèle/.test(text)), false);
    const links = await card().locator('.delegation-member-title>a').evaluateAll(nodes => nodes.map(node => new URL(node.href).searchParams.get('ticket')));
    assert.deepEqual(new Set(links), new Set([ids.active.parent_ticket_id, ...ids.active.child_ticket_ids]));
    await noPrivate(page); await noOverflow(page);
    await card().getByRole('button', { name: 'Actualiser le suivi', exact: true }).click();
    await card().getByRole('heading', { name: 'Travail coordonné', exact: true }).waitFor();
    await page.screenshot({ path: path.join(output, 'family-desktop.png'), fullPage: true });
    const child = card().locator('.delegation-member-title>a').nth(1); await child.click();
    assert.equal(new URL(page.url()).searchParams.get('ticket'), links[1]);
    await card().getByRole('heading', { name: 'Travail coordonné', exact: true }).waitFor();
    await card().locator('.delegation-member-title>a').first().click();
    assert.equal(new URL(page.url()).searchParams.get('ticket'), ids.active.parent_ticket_id);

    const otherPage = await other.newPage(), privateReads = [];
    otherPage.on('pageerror', error => errors.push(error.message));
    otherPage.on('request', request => { if (new URL(request.url()).pathname.startsWith('/api/delegation-families/')) privateReads.push(request.url()); });
    await otherPage.goto(origin + '/?ticket=' + ids.active.parent_ticket_id);
    await otherPage.locator('.delegation-card').getByRole('heading', { name: 'Travail coordonné', exact: true }).waitFor();
    assert.equal(await otherPage.locator('.delegation-member').count(), 3);
    assert.equal(await otherPage.locator('.delegation-controls').count(), 0);
    await noPrivate(otherPage); assert.deepEqual(privateReads, []);
    assert.equal((await other.request.get(origin + '/api/delegation-families/' + ids.active.family_id)).status(), 404);

    await page.setViewportSize({ width: 390, height: 844 }); await noOverflow(page);
    await page.screenshot({ path: path.join(output, 'family-mobile.png'), fullPage: true });
    const activeBefore = (await (await owner.request.get(origin + '/api/runtime')).json()).active_count;
    assert.equal(activeBefore, 1);
    await card().getByRole('button', { name: 'Demander l’arrêt', exact: true }).click();
    await page.getByRole('dialog').getByRole('button', { name: 'Annuler', exact: true }).click();
    assert.equal(stops.length, 0);
    await card().getByRole('button', { name: 'Demander l’arrêt', exact: true }).click();
    await page.getByRole('dialog').getByRole('button', { name: 'Confirmer l’arrêt', exact: true }).click();
    await card().getByText('Arrêt demandé', { exact: true }).waitFor();
    assert.equal(stops.length, 1); assert.equal(stops[0].data.expected_revision, 1);
    assert.equal((await (await owner.request.get(origin + '/api/runtime')).json()).active_count, activeBefore);
    assert.ok((await card().innerText()).includes('confirmation de leur fermeture'));
    await noOverflow(page);

    await page.setViewportSize({ width: 1440, height: 1000 }); await open('changing');
    const ticketVersion = await page.evaluate(() => state.ticket.version);
    await fixture('rename-child', 'POST');
    await card().locator('.delegation-member-title>a').filter({ hasText: ' updated' }).waitFor();
    await card().getByText('Arrêt nécessaire', { exact: true }).waitFor();
    assert.equal(await page.evaluate(() => state.ticket.version), ticketVersion);
    await fixture('rename-child', 'POST');
    await card().locator('.delegation-member-title>a').filter({ hasText: ' updated' }).waitFor({ state: 'detached' });
    await fixture('advance', 'POST');
    await card().getByText('Résultat accepté', { exact: true }).waitFor();
    await card().getByRole('button', { name: 'Demander l’arrêt', exact: true }).click();
    await page.getByRole('dialog').getByRole('button', { name: 'Confirmer l’arrêt', exact: true }).waitFor();
    await fixture('advance', 'POST');
    await page.getByRole('dialog').getByRole('button', { name: 'Confirmer l’arrêt', exact: true }).click();
    await page.locator('#notice').getByText('Le suivi a changé.', { exact: false }).waitFor();
    await card().getByText('Résultats réunis', { exact: true }).waitFor();
    assert.equal(stops.length, 2); assert.equal(stops[1].data.expected_revision, 2);
    assert.equal((await (await owner.request.get(origin + '/api/delegation-families/' + ids.changing.family_id)).json()).state, 'integrating');
    await card().getByRole('button', { name: 'Demander l’arrêt', exact: true }).click();
    await page.getByRole('dialog').getByRole('button', { name: 'Confirmer l’arrêt', exact: true }).click();
    await card().getByText('Arrêt demandé', { exact: true }).waitFor();
    assert.equal(stops.length, 3); assert.equal(stops[2].data.expected_revision, 3);
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({ browser: 'firefox', desktop: true, mobile: true, exactTicketLinks: true,
      privateOwnerControls: true, otherOwnerNoPrivateRequest: true, noPlanInDOM: true, confirmationRequired: true,
      stopRetainsRuntimeCapacity: true, metadataRefreshWithoutTicketVersion: true, conflictRefreshNoBlindRetry: true, errors }));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
