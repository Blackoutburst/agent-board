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
const action = 'agent.ticket.create_assigned', claimAction = 'agent.profile.release_claim';
const resourceAction = 'agent.ticket.retarget_resource';
const profile = (creation = false, otherOptionals = false) => ({
  id: 'lead', owner_id: 'owner', name: 'Lead', kind: 'agent', status: 'active', version: 8, legacy: false,
  project_ids: ['project-one'], policy: { allow_read: true, allow_messages: false, allow_requests: true,
    autonomy: 'requested', actions: [...grants, ...(otherOptionals ? [claimAction, resourceAction] : []),
      ...(creation ? [action] : [])], resources: ['agent-profiles:owner', 'repo:project-one'] }
});

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
        state.actor = { kind: 'human', owner_id: 'owner' }; state.view = 'agents';
        window.requests = []; window.completed = 0; window.current = ${JSON.stringify(current)};
        const fault = ${JSON.stringify(fault)};
        async function api(url, { method = 'GET', data } = {}) {
          if (method === 'GET' && url.startsWith('/projects?')) return [{ id: 'project-one', name: 'Fixture project' }];
          if (method === 'GET') return structuredClone(window.current);
          window.requests.push({ url, method, data: structuredClone(data) });
          if (method === 'POST') window.current = { ...structuredClone(data), id: 'created', owner_id: 'owner', kind: 'agent', version: 1 };
          else {
            if (data.expected_version !== window.current.version) throw new Error('Stale synthetic CAS');
            if (method === 'PATCH') window.current = { ...window.current, ...structuredClone(data) };
            else {
              const policy = window.current.policy;
              const optional = { '/claim-management': '${claimAction}', '/ticket-resource-management': '${resourceAction}',
                '/ticket-creation-management': '${action}' };
              const suffix = Object.keys(optional).find(value => url.endsWith(value));
              if (suffix) {
                policy.actions = policy.actions.filter(value => value !== optional[suffix]);
                if (data.enabled) policy.actions.push(optional[suffix]);
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
            if (fault === 'profile_owner') response.owner_id = 'other-owner';
            if (fault === 'profile_version') response.version--;
            if (fault === 'profile_malformed') return {};
            return response;
          }
          if (fault === 'lost_claim' && url.endsWith('/claim-management')) throw new Error('Synthetic lost claim response');
          if (fault === 'lost_resource' && url.endsWith('/ticket-resource-management')) throw new Error('Synthetic lost resource response');
          if (url.endsWith('/ticket-creation-management')) {
            if (fault === 'lost_creation') throw new Error('Synthetic lost creation-grant response');
            const result = structuredClone(window.current);
            if (fault === 'creation_malformed') return {};
            if (fault === 'creation_id') result.id = 'wrong-profile';
            if (fault === 'creation_owner') result.owner_id = 'other-owner';
            if (fault === 'creation_version') result.version--;
            if (fault === 'creation_action') result.policy.actions = result.policy.actions.filter(value => value !== '${action}');
            if (fault === 'creation_resource') result.policy.resources = result.policy.resources.filter(value => value !== 'agent-profiles:owner');
            if (fault === 'creation_types') result.policy.actions.push(7);
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
    const resources = () => dialog().getByLabel('Rattacher les tickets inactifs aux espaces autorisés', { exact: true });
    const creation = () => dialog().getByLabel('Créer des tickets affectés aux agents autorisés', { exact: true });
    const save = async () => {
      await dialog().getByRole('button', { name: /^(Create|Save) (profile|companion)$/ }).click();
      await page.waitForFunction(() => window.completed || document.querySelector('#modal-content h2')?.textContent === 'Délégation à vérifier');
      return page.evaluate(() => ({ requests: window.requests, current: window.current, completed: window.completed }));
    };
    const reload = async () => {
      await dialog().getByRole('button', { name: 'Recharger le profil', exact: true }).click();
      await page.waitForFunction(() => Boolean(document.querySelector('[name="manage_ticket_creation"]')));
    };

    await load();
    assert.equal(await creation().getAttribute('name'), 'manage_ticket_creation');
    assert.equal(await creation().isChecked(), false); assert.equal(await creation().isDisabled(), true);
    await dialog().getByLabel('Start from a template', { exact: true }).selectOption('management');
    assert.equal(await management().isChecked(), false);
    assert.equal(await creation().isChecked(), false, 'The template grants no assigned-ticket authority');
    let result = await save();
    assert.equal(result.requests.length, 1, 'A Management template alone performs no grant');
    assert.equal(result.requests[0].method, 'POST');
    assert.equal(result.current.policy.actions.includes(action), false);

    await load(); await dialog().getByLabel('Start from a template', { exact: true }).selectOption('management');
    await management().check();
    assert.equal(await creation().isChecked(), false, 'Enabling base management does not opt into assignment');
    await claims().check(); await resources().check(); await creation().check(); result = await save();
    assert.deepEqual(result.requests.map(r => [r.method, r.url, r.data.expected_version]), [
      ['POST', '/agent-profiles', undefined], ['PUT', '/agent-profiles/created/management', 1],
      ['PUT', '/agent-profiles/created/claim-management', 2], ['PUT', '/agent-profiles/created/ticket-resource-management', 3],
      ['PUT', '/agent-profiles/created/ticket-creation-management', 4]]);
    assert.equal(result.current.version, 5); assert.equal(result.completed, 1);
    assert.deepEqual(result.requests.at(-1).data, { expected_version: 4, enabled: true });
    assert.equal(result.requests[0].data.policy.actions.includes(action), false, 'The optional grant is not smuggled into creation');
    assert.ok(result.current.policy.actions.includes(action));

    await load(profile(false)); await creation().check(); result = await save();
    assert.deepEqual(result.requests.map(r => [r.method, r.data.expected_version, r.data.enabled]), [['PATCH', 8, undefined], ['PUT', 9, true]]);
    assert.equal(result.requests[1].url, '/agent-profiles/lead/ticket-creation-management');

    await load(profile(true, true)); await creation().uncheck(); result = await save();
    assert.deepEqual(result.requests.map(r => [r.method, r.data.expected_version, r.data.enabled]), [['PATCH', 8, undefined], ['PUT', 9, false]]);
    assert.equal(result.current.policy.actions.includes(action), false);
    assert.ok(result.current.policy.actions.includes(claimAction)); assert.ok(result.current.policy.actions.includes(resourceAction));

    await load(profile(true, true)); await management().uncheck();
    assert.equal(await creation().isDisabled(), true); result = await save();
    assert.equal(result.requests.length, 2); assert.equal(result.completed, 1);
    assert.equal(result.requests[1].url, '/agent-profiles/lead/management');
    assert.equal(result.current.policy.actions.some(value => value.startsWith('agent.ticket.') || value.startsWith('agent.profile.')), false);

    await load(profile(true, true)); await dialog().getByLabel('Role', { exact: true }).fill('Renamed role'); result = await save();
    assert.equal(result.requests.length, 1, 'Unrelated edits do not replay grants');
    assert.ok(result.current.policy.actions.includes(action)); assert.ok(result.current.policy.actions.includes(resourceAction));

    const legacy = profile(true); legacy.legacy = true; await load(legacy);
    for (const input of [management(), claims(), resources(), creation()]) assert.equal(await input.isDisabled(), true);

    for (const fault of ['lost_creation', 'creation_malformed', 'creation_id', 'creation_owner', 'creation_version',
      'creation_action', 'creation_resource', 'creation_types']) {
      await load(profile(false), fault); await creation().check(); result = await save();
      assert.equal(result.completed, 0, fault);
      assert.equal(result.requests.length, 2, 'An uncertain grant is never retried');
      assert.ok(result.current.policy.actions.includes(action), 'The synthetic committed grant is retained');
      await reload(); assert.equal(await creation().isChecked(), true);
      assert.equal(await page.evaluate(() => window.requests.length), 2, 'Recovery only reads the committed profile');
    }

    for (const fault of ['profile_id', 'profile_owner', 'profile_version', 'profile_malformed']) {
      await load(profile(false), fault); await creation().check(); result = await save();
      assert.equal(result.requests.length, 1, 'An incoherent profile ACK cannot target a grant');
      assert.equal(result.completed, 0); assert.equal(result.current.policy.actions.includes(action), false);
      await reload(); assert.equal(await creation().isChecked(), false);
    }

    for (const fault of ['lost_claim', 'lost_resource']) {
      await load(profile(false), fault);
      if (fault === 'lost_claim') await claims().check(); else await resources().check();
      await creation().check(); result = await save();
      assert.equal(result.requests.length, 2, 'An unconfirmed earlier optional grant stops the chain');
      assert.equal(result.completed, 0); assert.equal(result.current.policy.actions.includes(action), false);
      assert.equal(result.requests.some(request => request.url.endsWith('/ticket-creation-management')), false);
    }

    const companion = { ...profile(true, true), kind: 'companion', acts_for: 'owner', name: 'Companion' };
    await load(companion);
    for (const name of ['manage_agent_profiles', 'manage_agent_claims', 'manage_ticket_resources', 'manage_ticket_creation']) {
      assert.equal(await dialog().locator(`[name="${name}"]`).count(), 0, 'Companions have no ordinary-management controls');
    }
    assert.equal(await dialog().getByLabel('Start from a template', { exact: true }).count(), 0);
    assert.equal(await page.evaluate(() => window.requests.length), 0);
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({ status: 'passed', browser: 'Firefox', network: 'none', cases: [
      'default-denied', 'template-no-grant', 'separate-opt-in', 'five-CAS-steps', 'grant', 'revoke', 'base-revoke',
      'unrelated-edit-preserved', 'legacy-disabled', 'lost-and-malformed-grant-ACKs', 'profile-ACK-validation',
      'uncertain-prior-step-stops-chain', 'companion-controls-hidden'] }));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
