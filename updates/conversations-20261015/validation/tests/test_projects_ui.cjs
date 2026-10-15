// Browser regression checks against synthetic REST fixtures only.
// Run with Node and Playwright available (NODE_PATH may point to its installation).
const { firefox } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

(async () => {
  const browser = await firefox.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const consoleErrors = [];
  page.on('pageerror', (error) => consoleErrors.push(error.message));
  const owner = 'thescreamer';
  const project = { id: 'project-one', name: 'Plateforme test', description: 'Description initiale', context: 'Un contexte partagé', environment: 'Local synthétique', repository_url: 'https://example.com/repo', participants: [owner, 'blackoutburst'], version: 1, archived: false, ticket_counts: { todo: 1, total: 1 }, created_at: 100, updated_at: 100 };
  const ticket = { id: 'ticket-one', project_id: project.id, number: 1, thread_id: 'thread-one', title: 'Ticket <img src=x onerror=alert(1)>', description: 'Description du ticket', acceptance_criteria: 'Vérifier le résultat', priority: 'normal', status: 'todo', assignee_owner_id: null, claimant: null, version: 1, outcome: '', evidence: '', created_at: 100, updated_at: 100 };
  const messages = [];
  const threadInfo = () => ({ id: ticket.thread_id, title: ticket.title, participants: project.participants,
    updated_at: 100, unread_count: 0, message_preview: messages[0]?.body.replace(/\s+/g, ' ').slice(0, 180) || '',
    ticket: { id: ticket.id, number: ticket.number, title: ticket.title, project_id: project.id, project_name: project.name, url: `/?ticket=${ticket.id}` } });
  const generalThreads = Array.from({ length: 5 }, (_, index) => ({ id: `general-${index}`, title: `Question existante ${index + 1}`, updated_at: 100, unread_count: 0, ticket: null, message_preview: '' }));
  const legacyTask = { id: 'legacy-task', thread_id: 'general-0', title: 'Action existante à conserver', destination_kind: 'human', destination_id: owner, status: 'pending', instructions: 'Vérifier la ressource demandée.', claim_version: 0, required_capabilities: [], prerequisites: [] };
  let patchConflict = false;
  let lastTicketPatch;
  const seenRequests = [];
  await page.route('http://board.test/**', async (route) => {
    const req = route.request();
    const url = new URL(req.url());
    const body = req.postDataJSON();
    seenRequests.push({ method: req.method(), path: url.pathname, body });
    const json = (value, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(value) });
    if (!url.pathname.startsWith('/api/')) {
      const file = url.pathname === '/' ? 'index.html' : url.pathname.slice(1);
      if (file === 'platform/theme.css') return route.fulfill({ contentType: 'text/css', body: ':root{color-scheme:dark;--bg:#0b0d12;--panel:#141821;--panel-2:#1b2030;--line:#262d3d;--text:#e6eaf2;--muted:#8b95ab;--accent:#4d9fff;--ok:#35d07f;--bad:#ff5f56;--radius:14px;--bb-font:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}.bb-home-link{color:var(--accent);font:500 13px/1.5 var(--bb-font);text-decoration:none;white-space:nowrap}.bb-home-link:hover{text-decoration:underline}.bb-home-link:focus-visible{outline:2px solid var(--accent);outline-offset:4px}' });
      const safeFiles = { 'index.html': 'text/html', 'app.js': 'text/javascript', 'style.css': 'text/css' };
      assert.ok(safeFiles[file]);
      return route.fulfill({ contentType: safeFiles[file], body: fs.readFileSync(path.join(__dirname, '../web', file), 'utf8') });
    }
    if (req.method() !== 'GET') assert.equal(req.headers()['x-csrf-token'], 'synthetic-csrf');
    if (url.pathname === '/api/me') return json({ actor: { kind: 'human', owner_id: owner }, csrf_token: 'synthetic-csrf' });
    if (url.pathname === '/api/threads') return json([...generalThreads, ...(messages.length ? [threadInfo()] : [])]);
    if (url.pathname === '/api/projects') return json([project]);
    if (url.pathname === `/api/projects/${project.id}`) {
      if (req.method() === 'PATCH') { assert.equal(body.expected_version, project.version); Object.assign(project, body, { version: project.version + 1 }); }
      return json(project);
    }
    if (url.pathname === `/api/projects/${project.id}/tickets`) return json([ticket]);
    if (url.pathname === `/api/tickets/${ticket.id}/claim`) {
      assert.equal(body.expected_version, ticket.version);
      ticket.version++; ticket.claimant = { kind: 'human', owner_id: owner }; ticket.assignee_owner_id = owner; ticket.status = 'in_progress';
      return json(ticket);
    }
    if (url.pathname === `/api/tickets/${ticket.id}`) {
      if (req.method() === 'PATCH') {
        lastTicketPatch = body;
        if (patchConflict) { patchConflict = false; ticket.version++; ticket.description = 'Modification de Blackoutburst'; return json({ detail: 'Version conflict' }, 409); }
        assert.equal(body.expected_version, ticket.version);
        if (ticket.status === 'done' && body.status && body.status !== 'done') { ticket.outcome = ''; ticket.evidence = ''; }
        Object.assign(ticket, body, { version: ticket.version + 1 });
        if (body.release_claim) ticket.claimant = null;
        return json(ticket);
      }
      return json({ ticket, project });
    }
    if (url.pathname === `/api/threads/${ticket.thread_id}`) return json({ thread: threadInfo(), messages, tasks: [], next_before: null });
    if (url.pathname === `/api/threads/${ticket.thread_id}/read`) return json({ ok: true });
    if (url.pathname === `/api/threads/${ticket.thread_id}/messages`) {
      const message = { id: `message-${messages.length + 1}`, thread_id: ticket.thread_id, body: body.body, author: { kind: 'human', owner_id: owner }, created_at: 101, mentions: [], attachments: [] };
      messages.push(message); return json(message);
    }
    if (url.pathname === '/api/threads/general-0') return json({ thread: { ...generalThreads[0], participants: project.participants }, messages: [], tasks: [legacyTask], next_before: null });
    if (url.pathname === '/api/threads/general-0/read') return json({ ok: true });
    if (url.pathname === '/api/tasks/legacy-task/claim') { legacyTask.status = 'in_progress'; legacyTask.claimant = { kind: 'human', owner_id: owner }; legacyTask.claim_version = 1; return json({ claimed: true, task: legacyTask }); }
    throw new Error(`Unmocked API: ${req.method()} ${url.pathname}`);
  });
  try {
    await page.goto('http://board.test/?page=projects');
    assert.equal(await page.locator('#thread-list .thread-item').count(), 5, 'Unstarted ticket must stay out of conversations');
    await page.getByRole('link', { name: project.name }).click();
    await page.getByRole('link', { name: ticket.title, exact: true }).click();
    await page.getByRole('button', { name: 'Prendre en charge', exact: true }).click();
    await page.getByText('Pris en charge par TheScreamer.', { exact: true }).waitFor();
    assert.equal(await page.locator('.ticket-view img').count(), 0, 'Shared HTML must remain plain text');
    await page.getByRole('button', { name: 'Modifier', exact: true }).click();
    let dialog = page.getByRole('dialog');
    await dialog.getByLabel('Titre du ticket').fill('Mon brouillon');
    await dialog.getByRole('button', { name: 'Fermer', exact: true }).click();
    await page.getByRole('button', { name: 'Modifier', exact: true }).click();
    assert.equal(await dialog.getByLabel('Titre du ticket').inputValue(), 'Mon brouillon', 'Closing a dialog must preserve its draft');
    patchConflict = true;
    await dialog.getByRole('button', { name: 'Enregistrer le ticket' }).click();
    await dialog.getByText('Une autre modification a été enregistrée.', { exact: false }).waitFor();
    assert.equal(await dialog.getByLabel('Titre du ticket').inputValue(), 'Mon brouillon', '409 must preserve field contents');
    assert.equal(await dialog.getByRole('button', { name: 'Enregistrer le ticket' }).isDisabled(), true);
    await dialog.getByRole('button', { name: 'Comparer avec la version actuelle' }).click();
    await dialog.getByText('Modification de Blackoutburst', { exact: true }).waitFor();
    await dialog.getByRole('button', { name: 'Réexaminer mon brouillon sur cette version' }).click();
    assert.equal(await dialog.getByLabel('Titre du ticket').inputValue(), 'Mon brouillon');
    assert.equal(await dialog.getByLabel('Description', { exact: true }).inputValue(), 'Modification de Blackoutburst', 'Unchanged local fields must retain newer server values');
    await dialog.getByRole('button', { name: 'Enregistrer le ticket' }).click();
    await page.getByRole('heading', { name: '#1 · Mon brouillon', exact: true }).waitFor();
    assert.deepEqual(lastTicketPatch, { expected_version: 3, title: 'Mon brouillon' });
    await page.getByRole('button', { name: 'Modifier', exact: true }).click();
    await dialog.locator('select[name="status"]').selectOption('done');
    assert.equal(await dialog.getByLabel('Résultat', { exact: true }).getAttribute('required'), '');
    await dialog.getByLabel('Résultat', { exact: true }).fill('Résultat validé');
    await dialog.getByLabel('Preuves de validation').fill('Tests réussis');
    await dialog.getByRole('button', { name: 'Enregistrer le ticket' }).click();
    await page.getByText('Terminé', { exact: true }).waitFor();
    await page.getByRole('button', { name: 'Modifier', exact: true }).click();
    await dialog.locator('select[name="status"]').selectOption('todo');
    await dialog.getByRole('button', { name: 'Enregistrer le ticket' }).click();
    await page.getByText('À faire', { exact: true }).waitFor();
    assert.equal(Object.hasOwn(lastTicketPatch, 'outcome'), false, 'Reopening should not resend stale resolution metadata');
    assert.equal(Object.hasOwn(lastTicketPatch, 'evidence'), false);
    await page.getByRole('button', { name: 'Poser une question / échanger', exact: true }).click();
    await page.getByRole('heading', { name: 'Échange à propos du ticket #1', exact: true }).waitFor();
    assert.equal(await page.locator('#thread-list .thread-item').count(), 5, 'Opening an empty ticket exchange must not list it');
    assert.equal(await page.locator('.task-pane').count(), 0, 'A conversation without legacy tasks uses no task panel');
    assert.equal(await page.getByRole('button', { name: 'Create task', exact: true }).count(), 0);
    assert.equal(await page.getByRole('heading', { name: ticket.title, exact: true }).count(), 0, 'Task title must not become a conversation subject');
    await page.getByText('Cet échange apparaîtra dans les conversations après le premier message.', { exact: false }).waitFor();
    const question = 'Peux-tu fournir la ressource <img src=x onerror=alert(1)> ?';
    await page.getByRole('textbox', { name: 'Message', exact: true }).fill(question);
    await page.getByRole('button', { name: 'Send message', exact: true }).click();
    await page.getByRole('heading', { name: question, exact: true }).waitFor();
    await page.locator('#thread-list .thread-name').getByText(question, { exact: true }).waitFor();
    assert.equal(await page.locator('#thread-list .thread-item').count(), 6);
    assert.equal(await page.locator('#main img, #thread-list img').count(), 0, 'Conversation subject must also remain plain text');
    assert.equal(await page.locator('.thread-context').innerText(), `${project.name} · Ticket #1`);
    await page.goto(`http://board.test/?thread=${ticket.thread_id}`);
    await page.getByRole('heading', { name: question, exact: true }).waitFor();
    await page.getByRole('link', { name: 'Retour au ticket #1', exact: true }).click();
    await page.getByRole('heading', { name: '#1 · Mon brouillon', exact: true }).waitFor();
    await page.goto('http://board.test/?thread=general-0');
    await page.getByRole('heading', { name: generalThreads[0].title, exact: true }).waitFor();
    await page.getByRole('heading', { name: legacyTask.title, exact: true }).waitFor();
    await page.getByRole('button', { name: 'Take responsibility', exact: true }).click();
    await page.getByRole('button', { name: 'Update task', exact: true }).waitFor();
    assert.equal(await page.locator('.thread-layout.has-tasks').count(), 1, 'Existing legacy tasks and their actions remain accessible');
    assert.equal(await page.getByRole('button', { name: 'Create task', exact: true }).count(), 0);
    await page.goto(`http://board.test/?ticket=${ticket.id}`);
    await page.getByRole('heading', { name: '#1 · Mon brouillon', exact: true }).waitFor();
    await page.setViewportSize({ width: 375, height: 812 });
    const assertNoInternalClipping = async (label) => {
      const clipped = await page.locator('.main, .work-breadcrumb, .page-head, .page-body, .work-section, .work-text, .ticket-detail-grid, .ticket-responsibility, .project-context, .work-repository').evaluateAll((nodes) => nodes.flatMap((node) => {
        const bounds = node.getBoundingClientRect();
        if (bounds.width === 0) return [];
        const viewportClipped = bounds.left < -1 || bounds.right > window.innerWidth + 1;
        const contentClipped = node.scrollWidth > node.clientWidth + 1;
        return viewportClipped || contentClipped ? [{ selector: node.className, left: bounds.left, right: bounds.right, width: node.clientWidth, scrollWidth: node.scrollWidth }] : [];
      }));
      assert.deepEqual(clipped, [], `${label}: visible containers and wrapped text must fit, even when an ancestor hides overflow`);
    };
    await assertNoInternalClipping('Ticket mobile');
    await page.getByRole('button', { name: 'Poser une question / échanger', exact: true }).click();
    await page.getByRole('heading', { name: question, exact: true }).waitFor();
    await assertNoInternalClipping('Conversation mobile');
    await page.getByRole('link', { name: 'Retour au ticket #1', exact: true }).click();
    await page.getByRole('heading', { name: '#1 · Mon brouillon', exact: true }).waitFor();
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth), false, 'Ticket detail must not overflow at mobile width');
    await page.getByRole('link', { name: '← Retour au projet', exact: true }).click();
    await page.getByRole('heading', { name: project.name, exact: true }).waitFor();
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth), false, 'Project detail must not overflow at mobile width');
    await assertNoInternalClipping('Project mobile');
    await page.getByRole('button', { name: 'Modifier le projet' }).click();
    await dialog.getByLabel('Archiver ce projet').check();
    await dialog.getByRole('button', { name: 'Enregistrer le projet' }).click();
    await page.getByRole('button', { name: 'Créer un ticket' }).waitFor();
    assert.equal(await page.getByRole('button', { name: 'Créer un ticket' }).isDisabled(), true);
    assert.deepEqual(consoleErrors, []);
    console.log('Firefox UI: drafts, 409 comparison, safe text, claims, completion/reopen, question subjects, lazy conversation listing, direct ticket links, legacy tasks, archive, CSRF and mobile checks passed.');
  } catch (error) { console.error(await page.locator('#modal-content').innerText()); console.error(consoleErrors); throw error; } finally { await browser.close(); }
})().catch((error) => { console.error(error); process.exitCode = 1; });
