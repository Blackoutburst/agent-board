/* Isolated real-DOM profile editor tests. All API responses are in memory;
 * no server, account, stored browser profile or network connection is used. */
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
const editor = [
  app.slice(0, app.indexOf('let noticeTimer;')),
  section(app, 'function openModal(', 'function head('),
  section(workspace, '  function bindForm(', '  async function projectEditor('),
  section(workspace, '  const profileStates =', '  function profileCard('),
  section(workspace, '  async function profileEditor(', '  function duplicateProfile('),
].join('\n');
const baseGrants = ['agent.profile.read', 'agent.profile.create', 'agent.profile.update', 'agent.profile.archive'];
const claimGrant = 'agent.profile.release_claim';
const profile = (claim = false) => ({
  id: 'managed-lead', owner_id: 'fixture-owner', name: 'Scoped Lead', version: 8, legacy: false,
  project_ids: ['project-one'], policy: { allow_read: true, allow_messages: false, allow_requests: true,
    autonomy: 'requested', actions: [...baseGrants, ...(claim ? [claimGrant] : [])], resources: ['agent-profiles:fixture-owner'] },
});

(async () => {
  const browser = await firefox.launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1080 } });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/*', route => route.abort());
    const load = async (current = null, acknowledgement = null) => {
      await page.goto('about:blank');
      await page.setContent('<dialog id="modal"><div id="modal-content"></div></dialog>');
      await page.addScriptTag({ content: `${editor}\n
        const workPath = encodeURIComponent;
        const guard = action => action;
        let agentTab;
        const agents = async () => { window.completed++; };
        const notify = () => {};
        const providerPreparationSummary = async saved => { window.prepared = saved; };
        state.actor = { kind: 'human', owner_id: 'fixture-owner' };
        window.requests = []; window.completed = 0;
        window.current = ${JSON.stringify(current)};
        const acknowledgement = ${JSON.stringify(acknowledgement)};
        async function api(url, { method = 'GET', data } = {}) {
          if (method === 'GET' && url.startsWith('/projects?')) return [{ id: 'project-one', name: 'Fixture project' }];
          if (method === 'GET') return structuredClone(window.current);
          window.requests.push({ url, method, data: structuredClone(data) });
          if (method === 'POST') window.current = { ...structuredClone(data), id: 'created-lead', owner_id: 'fixture-owner', version: 1 };
          else if (method === 'PATCH') window.current = { ...window.current, ...structuredClone(data), version: window.current.version + 1 };
          else {
            const policy = window.current.policy;
            if (url.endsWith('/claim-management')) {
              policy.actions = policy.actions.filter(value => value !== '${claimGrant}');
              if (data.enabled) policy.actions.push('${claimGrant}');
            } else if (url.endsWith('/management')) {
              if (data.enabled) {
                policy.actions = [...new Set([...policy.actions, ...${JSON.stringify(baseGrants)}])];
                policy.resources = [...new Set([...policy.resources, 'agent-profiles:fixture-owner'])];
              } else {
                policy.actions = policy.actions.filter(value => !value.startsWith('agent.profile.'));
                policy.resources = policy.resources.filter(value => !value.startsWith('agent-profiles:'));
              }
            } else throw new Error('Unexpected mutation ' + url);
            window.current.version++;
            if (url.endsWith('/claim-management') && acknowledgement) {
              if (acknowledgement === 'lost') throw new Error('Synthetic lost response');
              const response = structuredClone(window.current);
              if (acknowledgement === 'id') response.id = 'wrong-profile';
              if (acknowledgement === 'owner') response.owner_id = 'wrong-owner';
              if (acknowledgement === 'version') response.version--;
              if (acknowledgement === 'actions') response.policy.actions = response.policy.actions.filter(value => value !== '${claimGrant}');
              if (acknowledgement === 'read-grant') response.policy.actions = response.policy.actions.filter(value => value !== 'agent.profile.read');
              if (acknowledgement === 'resources') response.policy.resources = [];
              if (acknowledgement === 'malformed') return {};
              return response;
            }
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
    const save = async () => {
      await dialog().getByRole('button', { name: /^(Create|Save) profile$/ }).click();
      await page.waitForFunction(() => window.completed || document.querySelector('#modal-content h2')?.textContent === 'Délégation à vérifier');
      return page.evaluate(() => ({ requests: window.requests, current: window.current, completed: window.completed }));
    };

    await load();
    assert.equal(await claims().isChecked(), false);
    assert.equal(await claims().isDisabled(), true);
    await dialog().getByLabel('Start from a template', { exact: true }).selectOption('management');
    assert.equal(await management().isChecked(), false);
    assert.equal(await claims().isChecked(), false, 'A descriptive template grants no claim authority');
    assert.equal(await claims().isDisabled(), true);
    await dialog().getByLabel('Fixture project', { exact: true }).check();
    await management().check();
    assert.equal(await claims().isDisabled(), false);
    await claims().check();
    let result = await save();
    assert.deepEqual(result.requests.map(r => [r.method, r.url, r.data.expected_version, r.data.enabled]), [
      ['POST', '/agent-profiles', undefined, undefined],
      ['PUT', '/agent-profiles/created-lead/management', 1, true],
      ['PUT', '/agent-profiles/created-lead/claim-management', 2, true],
    ]);
    assert.equal(result.current.version, 3);
    assert.ok(result.current.policy.actions.includes(claimGrant));

    await load();
    await dialog().getByLabel('Profile name', { exact: true }).fill('Base management only');
    await management().check();
    result = await save();
    assert.equal(result.requests.length, 2);
    assert.equal(result.current.policy.actions.includes(claimGrant), false, 'Base Management never grants claim release');

    await load(profile(true));
    await claims().uncheck();
    result = await save();
    assert.deepEqual(result.requests.map(r => [r.method, r.data.expected_version, r.data.enabled]), [['PATCH', 8, undefined], ['PUT', 9, false]]);
    assert.equal(result.current.policy.actions.includes(claimGrant), false);

    const readOnlyManager = profile();
    readOnlyManager.policy.actions = ['agent.profile.read'];
    await load(readOnlyManager);
    await claims().check();
    result = await save();
    assert.equal(result.completed, 1, 'A scoped manager with read authority can receive the separate claim grant');
    assert.deepEqual(result.current.policy.actions, ['agent.profile.read', claimGrant]);
    assert.equal(result.requests.length, 2);

    await load(profile(true));
    await management().uncheck();
    assert.equal(await claims().isDisabled(), true);
    result = await save();
    assert.equal(result.requests.length, 2);
    assert.ok(result.requests[1].url.endsWith('/management'));
    assert.equal(result.requests[1].data.enabled, false);
    assert.deepEqual(result.current.policy.actions, []);

    await load(profile());
    await claims().check();
    await management().uncheck();
    result = await save();
    assert.equal(result.requests.length, 2, 'A pending claim grant is discarded when base Management is disabled');
    assert.equal(result.current.policy.actions.includes(claimGrant), false);

    await load(profile(true));
    result = await save();
    assert.equal(result.requests.length, 1, 'Saving other profile fields does not retry a delegation');
    assert.ok(result.current.policy.actions.includes(claimGrant));

    await load({ ...profile(true), legacy: true });
    assert.equal(await management().isDisabled(), true);
    assert.equal(await claims().isDisabled(), true);

    for (const acknowledgement of ['lost', 'id', 'owner', 'version', 'actions', 'read-grant', 'resources', 'malformed']) {
      await load(profile(), acknowledgement);
      await claims().check();
      result = await save();
      assert.equal(result.completed, 0);
      assert.equal(result.requests.length, 2, 'An uncertain claim grant is not retried automatically');
      assert.match(await dialog().getByRole('alert').textContent(), /Profil enregistré ; délégation non confirmée/);
      assert.equal(await dialog().getByRole('button', { name: 'Save profile', exact: true }).count(), 0, 'The stale editor cannot save again');
      await dialog().getByRole('button', { name: 'Recharger le profil', exact: true }).click();
      await dialog().getByRole('heading', { name: 'Edit agent profile', exact: true }).waitFor();
      assert.equal(await claims().isChecked(), true, 'Recovery reloads the committed claim grant');
      assert.equal(await page.evaluate(() => window.requests.length), 2);
    }
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({ status: 'passed', browser: 'Firefox', cases: ['default-denied', 'template-no-grant', 'separate-grants', 'chained-versions', 'claim-revoke', 'base-revoke', 'legacy-disabled', 'unchanged-no-retry', 'lost-response', 'incoherent-response-recovery'], network: 'none' }));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
