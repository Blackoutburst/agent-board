/* Real Firefox DOM, isolated in-memory API; no account or network access. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { firefox } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '..');
const app = fs.readFileSync(path.join(root, 'web/app.js'), 'utf8');
const workspace = fs.readFileSync(path.join(root, 'web/workspace.js'), 'utf8');
const section = (source, start, end) => {
  const begin = source.indexOf(start), finish = source.indexOf(end, begin);
  assert.ok(begin >= 0 && finish > begin, `Missing source section: ${start}`);
  return source.slice(begin, finish);
};
const editor = [app.slice(0, app.indexOf('let noticeTimer;')),
  section(app, 'function openModal(', 'function head('),
  section(workspace, '  function bindForm(', '  async function projectEditor('),
  section(workspace, '  const profileStates =', '  function profileCard('),
  section(workspace, '  async function profileEditor(', '  function duplicateProfile(')].join('\n');
const grants = ['agent.profile.read', 'agent.profile.create', 'agent.profile.update', 'agent.profile.archive'];
const action = 'agent.ticket.retarget_resource', claimAction = 'agent.profile.release_claim';
const profile = (ticket = false) => ({ id: 'lead', owner_id: 'owner', name: 'Lead', version: 8, legacy: false,
  project_ids: ['project-one'], policy: { allow_read: true, allow_messages: false, allow_requests: true,
    autonomy: 'requested', actions: [...grants, ...(ticket ? [action] : [])], resources: ['agent-profiles:owner'] } });
(async () => {
  const browser = await firefox.launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1080 } });
    const errors = []; page.on('pageerror', error => errors.push(error.message));
    await page.route('**/*', route => route.abort());
    const load = async (current = null, fault = '') => {
      await page.goto('about:blank');
      await page.setContent('<dialog id="modal"><div id="modal-content"></div></dialog>');
      await page.addScriptTag({ content: `${editor}\n
        const workPath = encodeURIComponent, guard = action => action;
        let agentTab; const agents = async () => { window.completed++; };
        const notify = () => {}, providerPreparationSummary = async () => {};
        state.actor = { kind: 'human', owner_id: 'owner' };
        window.requests = []; window.completed = 0; window.current = ${JSON.stringify(current)};
        const fault = ${JSON.stringify(fault)};
        async function api(url, { method = 'GET', data } = {}) {
          if (method === 'GET' && url.startsWith('/projects?')) return [{ id: 'project-one', name: 'Fixture project' }];
          if (method === 'GET') return structuredClone(window.current);
          window.requests.push({ url, method, data: structuredClone(data) });
          if (method === 'POST') window.current = { ...structuredClone(data), id: 'created', owner_id: 'owner', version: 1 };
          else {
            if (data.expected_version !== window.current.version) throw new Error('Stale synthetic CAS');
            if (method === 'PATCH') window.current = { ...window.current, ...structuredClone(data) };
            else {
              const policy = window.current.policy;
              if (url.endsWith('/claim-management') || url.endsWith('/ticket-resource-management')) {
                const action = url.endsWith('/claim-management') ? '${claimAction}' : '${action}';
                policy.actions = policy.actions.filter(value => value !== action);
                if (data.enabled) policy.actions.push(action);
              } else if (url.endsWith('/management')) {
                if (data.enabled) {
                  policy.actions = [...new Set([...policy.actions, ...${JSON.stringify(grants)}])];
                  policy.resources = [...new Set([...policy.resources, 'agent-profiles:owner'])];
                } else {
                  policy.actions = policy.actions.filter(value => !value.startsWith('agent.profile.') && !value.startsWith('agent.ticket.'));
                  policy.resources = policy.resources.filter(value => !value.startsWith('agent-profiles:'));
                }
              } else throw new Error('Unexpected mutation ' + url);
            }
            window.current.version++;
          }
          if (method === 'PATCH' && fault.startsWith('profile_')) {
            const response = structuredClone(window.current);
            if (fault === 'profile_id') response.id = 'wrong-profile';
            if (fault === 'profile_version') response.version--;
            if (fault === 'profile_malformed') return {};
            return response;
          }
          if (fault === 'lost_claim' && url.endsWith('/claim-management')) throw new Error('Synthetic lost claim response');
          if (url.endsWith('/ticket-resource-management')) {
            if (fault === 'lost_ticket') throw new Error('Synthetic lost ticket response');
            const result = structuredClone(window.current);
            if (fault === 'ticket_version') result.version--;
            if (fault === 'ticket_action') result.policy.actions = result.policy.actions.filter(value => value !== '${action}');
            return result;
          }
          return structuredClone(window.current);
        }
        window.openEditor = () => profileEditor(window.current?.id || null);
      ` });
      await page.evaluate(() => window.openEditor());
    };
    const dialog = () => page.getByRole('dialog');
    const management = () => dialog().getByLabel('Gérer les profils d’agents ordinaires', { exact: true });
    const claims = () => dialog().getByLabel('Libérer les réservations expirées des agents gérés', { exact: true });
    const tickets = () => dialog().getByLabel('Rattacher les tickets inactifs aux espaces autorisés', { exact: true });
    const save = async () => {
      await dialog().getByRole('button', { name: /^(Create|Save) profile$/ }).click();
      await page.waitForFunction(() => window.completed || document.querySelector('#modal-content h2')?.textContent === 'Délégation à vérifier');
      return page.evaluate(() => ({ requests: window.requests, current: window.current, completed: window.completed }));
    };
    await load();
    assert.equal(await tickets().isChecked(), false); assert.equal(await tickets().isDisabled(), true);
    await dialog().getByLabel('Start from a template', { exact: true }).selectOption('management');
    assert.equal(await tickets().isChecked(), false, 'The template grants no routing authority');
    await management().check(); await claims().check(); await tickets().check();
    let result = await save();
    assert.deepEqual(result.requests.map(r => [r.method, r.url, r.data.expected_version]), [
      ['POST', '/agent-profiles', undefined], ['PUT', '/agent-profiles/created/management', 1],
      ['PUT', '/agent-profiles/created/claim-management', 2], ['PUT', '/agent-profiles/created/ticket-resource-management', 3]]);
    assert.equal(result.current.version, 4); assert.ok(result.current.policy.actions.includes(action));

    await load(profile(true)); await tickets().uncheck(); result = await save();
    assert.deepEqual(result.requests.map(r => [r.method, r.data.expected_version, r.data.enabled]), [['PATCH', 8, undefined], ['PUT', 9, false]]);
    assert.equal(result.current.policy.actions.includes(action), false);

    await load(profile(true)); await management().uncheck();
    assert.equal(await tickets().isDisabled(), true); result = await save();
    assert.equal(result.requests.length, 2); assert.equal(result.current.policy.actions.includes(action), false);
    assert.equal(result.completed, 1);

    await load(profile(true)); await dialog().getByLabel('Role', { exact: true }).fill('Renamed role'); result = await save();
    assert.equal(result.requests.length, 1); assert.ok(result.current.policy.actions.includes(action), 'Unrelated edits preserve the opt-in');

    const legacy = profile(true); legacy.legacy = true; await load(legacy);
    assert.equal(await tickets().isDisabled(), true);
    for (const fault of ['lost_ticket', 'ticket_version', 'ticket_action']) {
      await load(profile(false), fault); await tickets().check(); result = await save();
      assert.equal(result.completed, 0); assert.equal(result.requests.length, 2, 'Uncertain grants are not retried');
      assert.ok(result.current.policy.actions.includes(action), 'Committed state is retained');
      await dialog().getByRole('button', { name: 'Recharger le profil', exact: true }).click();
      await page.waitForFunction(() => Boolean(document.querySelector('[name="manage_ticket_resources"]')));
      assert.equal(await tickets().isChecked(), true);
    }
    for (const fault of ['profile_id', 'profile_version', 'profile_malformed']) {
      await load(profile(false), fault); await tickets().check(); result = await save();
      assert.equal(result.requests.length, 1, 'An incoherent profile ACK cannot target a grant');
      assert.equal(result.completed, 0); assert.equal(result.current.policy.actions.includes(action), false);
      await dialog().getByRole('button', { name: 'Recharger le profil', exact: true }).click();
      await page.waitForFunction(() => Boolean(document.querySelector('[name="manage_ticket_resources"]')));
      assert.equal(await tickets().isChecked(), false);
    }
    await load(profile(false), 'lost_claim'); await claims().check(); await tickets().check(); result = await save();
    assert.equal(result.requests.length, 2, 'An unconfirmed claim grant prevents a subsequent routing grant');
    assert.equal(result.current.policy.actions.includes(action), false);
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({ status: 'passed', browser: 'Firefox', network: 'none', cases: ['default-denied', 'template-no-grant', 'separate-grant', 'four-CAS-steps', 'revoke', 'base-revoke', 'unrelated-edit-preserved', 'legacy-disabled', 'lost-ACK', 'bad-version-ACK', 'bad-action-ACK', 'uncertain-prior-step-stops-chain', 'profile-ACK-validation'] }));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
