"""Bounded plan publication and recovery; no provider or server execution."""
from copy import deepcopy
import fcntl
import json
import os

import pytest

from blackoutburst_agents.board_reader import BoardReadError
from blackoutburst_agents.dev_journal import PrivateDirectory, decode
from blackoutburst_agents.dev_manifest import DevelopmentError, canonical
from blackoutburst_agents.team_ticket_client import TeamTicketBoardClient
from blackoutburst_agents.team_ticket_plan import TeamTicketPlanController, documents
from blackoutburst_agents import cli
from blackoutburst_agents import team_ticket_cli


def inputs():
    catalog = {'schema': 'team-ticket-catalog-v1', 'owner_id': 'owner', 'slots': [
        {'slot_id': 'coder', 'agent_id': 'coder-profile', 'profile_version': 2, 'resource': 'repo:work'}]}
    def step(name, dependencies):
        return dict(step_id=name, slot_id='coder', title='Task ' + name, description='Explicit bounded work.',
                    acceptance_criteria='Verified result.', depends_on=dependencies, milestone='First delivery')
    plan = dict(schema='team-ticket-plan-v1', owner_id='owner', project_id='project',
                parent_ticket_id='parent', project_version=3, parent_version=4,
                steps=[step('c', ['b', 'a']), step('b', ['a']), step('a', [])])
    return plan, catalog


class BoardClient:
    session_id = 'lead-session'
    endpoint = 'http://localhost/mcp'

    def __init__(self):
        self.project = dict(id='project', version=3, name='Project', description='Context', context='Chosen project',
                            repository_url='', environment='', participants=['owner'], archived=False,
                            updated_at=0, ticket_counts={})
        self.parent = dict(id='parent', project_id='project', version=4, title='Parent', status='todo',
                           assignee_presence={'last_seen_at': 0})
        policy = dict(allow_read=True, allow_requests=True, autonomy='requested',
                      actions=['repo.read_file'], resources=['repo:work'])
        self.target = dict(id='coder-profile', owner_id='owner', version=2, status='active',
                           project_ids=['project'], policy=policy)
        self.lead = dict(id='lead-profile', owner_id='owner', version=7, status='active', legacy=False,
                         project_ids=['project'], policy={})
        self.policy = dict(enabled=True, allow_read=True, allow_requests=True, autonomy='queue',
                           actions=['agent.profile.read'], resources=['agent-profiles:owner', 'repo:work'])
        self.calls = []; self.created = {}; self.tickets = {}; self.lose_once = False; self.wrong = False
        self.before_create = None; self.registrations = 0

    def registration(self):
        self.registrations += 1
        self.parent['assignee_presence']['last_seen_at'] = self.registrations
        return deepcopy(dict(identity=dict(id='lead-profile', owner_id='owner', device_id='device', profile_version=7),
            session=dict(id=self.session_id, owner_id='owner', agent_id='lead-profile', device_id='device',
                         profile_version=7, last_seen_at=self.registrations), profile=self.lead, policy=self.policy))

    def call(self, name, args):
        self.calls.append((name, deepcopy(args)))
        if name == 'get_project':
            return deepcopy(self.project)
        if name == 'get_managed_agent_profile':
            return deepcopy(self.target)
        if name == 'get_ticket':
            return deepcopy(dict(ticket=self.parent if args['ticket_id'] == 'parent'
                                 else self.tickets[args['ticket_id']], project=self.project))
        assert name == 'create_ticket', 'No other tool may mutate the board'
        if self.before_create:
            self.before_create(args)
        key = args['idempotency_key']
        if key not in self.created:
            number = len(self.tickets) + 1
            ticket = {key: deepcopy(value) for key, value in args.items() if key != 'idempotency_key'}
            ticket.update(id='child-' + str(number), thread_id='thread-' + str(number), number=number,
                          version=1, status='todo', claimant=None)
            self.tickets[ticket['id']] = ticket
            self.created[key] = (deepcopy(args), ticket['id'])
            self.project['updated_at'] += 1; self.project['ticket_counts'] = {'total': number}
        original, ticket_id = self.created[key]
        assert args == original, 'Idempotent replay must use the exact payload'
        result = deepcopy(self.tickets[ticket_id])
        if self.lose_once:
            self.lose_once = False
            raise BoardReadError('timeout')
        if self.wrong:
            result['resource'] = 'repo:other'
        return result


@pytest.fixture
def case(tmp_path):
    plan, catalog = inputs(); client = BoardClient()
    controller = TeamTicketPlanController(client, tmp_path / 'journal')
    return plan, catalog, client, controller


def test_preview_is_deterministic_sorted_bounded_and_does_not_write(case):
    plan, catalog, client, controller = case
    first = controller.preview(plan, catalog)
    assert first == controller.preview(plan, catalog)
    assert first['order'] == ['a', 'b', 'c']
    assert len(first['approval_sha256']) == 64
    assert not controller.directory.exists()
    assert not client.created
    reordered = deepcopy(plan); reordered['steps'].reverse()
    assert first == controller.preview(reordered, catalog)


def test_apply_creates_reused_slot_dag_with_intent_before_every_effect(case):
    plan, catalog, client, controller = case
    approved = controller.preview(plan, catalog)['approval_sha256']
    def before(_):
        assert decode((controller.directory / 'intent.json').read_bytes())['approval_sha256'] == approved
    client.before_create = before
    result = controller.apply(plan, catalog, approved_sha256=approved)
    assert result['status'] == 'complete' and len(result['tickets']) == 3
    tickets = list(client.tickets.values())
    assert tickets[1]['depends_on'] == [tickets[0]['id']]
    assert set(tickets[2]['depends_on']) == {tickets[0]['id'], tickets[1]['id']}
    assert all(row['assignee_agent_id'] == 'coder-profile' for row in tickets)
    assert all('milestone:First delivery' in row['labels'] for row in tickets)
    assert {name for name, _ in client.calls} <= {'get_project', 'get_ticket', 'get_managed_agent_profile', 'create_ticket'}


def test_lost_ack_replays_exact_key_without_duplicate_or_new_approval(case):
    plan, catalog, client, controller = case
    approved = controller.preview(plan, catalog)['approval_sha256']
    client.lose_once = True
    with pytest.raises(BoardReadError, match='timeout'):
        controller.apply(plan, catalog, approved_sha256=approved)
    assert len(client.tickets) == 1
    result = controller.apply(plan, catalog, approved_sha256=approved)
    assert len(result['tickets']) == 3 and len(client.tickets) == 3
    creates = [args for name, args in client.calls if name == 'create_ticket']
    assert creates[0] == creates[1]


def test_lost_ack_then_child_progressed_reconciles_exact_server_key(case):
    plan, catalog, client, controller = case
    approved = controller.preview(plan, catalog)['approval_sha256']; client.lose_once = True
    with pytest.raises(BoardReadError):
        controller.apply(plan, catalog, approved_sha256=approved)
    child = next(iter(client.tickets.values()))
    child.update(version=2, status='in_progress', title='Human changed after lost ACK',
                 assignee_agent_id=None, labels=[])
    result = controller.apply(plan, catalog, approved_sha256=approved)
    assert len(result['tickets']) == 3 and len(client.tickets) == 3
    assert child['version'] == 2 and child['title'] == 'Human changed after lost ACK'
    creates = [args for name, args in client.calls if name == 'create_ticket']
    assert creates[0] == creates[1]


def test_confirmed_children_progress_is_preserved_and_never_recreated(case):
    plan, catalog, client, controller = case
    approved = controller.preview(plan, catalog)['approval_sha256']
    first = controller.apply(plan, catalog, approved_sha256=approved)
    child = client.tickets[first['tickets'][0]['ticket_id']]
    child.update(version=9, title='Human revised task', status='done', assignee_agent_id=None,
                 labels=[], resource='repo:moved')
    calls = len([x for x in client.calls if x[0] == 'create_ticket'])
    assert controller.apply(plan, catalog, approved_sha256=approved) == first
    assert len([x for x in client.calls if x[0] == 'create_ticket']) == calls
    assert child['title'] == 'Human revised task' and child['version'] == 9


def test_dependencies_returned_in_creation_order_are_compared_as_a_set(case):
    plan, catalog, client, controller = case
    base = deepcopy(plan['steps'][-1])
    def step(name, deps): return {**base, 'step_id': name, 'depends_on': deps}
    plan['steps'] = [step('b', []), step('z', []), step('a', ['z']), step('x', ['a', 'b'])]
    original = client.call
    def reordered(name, args):
        value = original(name, args)
        if name == 'create_ticket':
            value['depends_on'].sort(key=lambda key: client.tickets[key]['number'])
        return value
    client.call = reordered
    approved = controller.preview(plan, catalog)['approval_sha256']
    result = controller.apply(plan, catalog, approved_sha256=approved)
    assert len(result['tickets']) == 4


@pytest.mark.parametrize('field', ['project', 'parent', 'profile', 'policy'])
def test_current_scope_changes_refuse_before_creation(case, field):
    plan, catalog, client, controller = case
    approved = controller.preview(plan, catalog)['approval_sha256']
    if field == 'project': client.project['context'] = 'Changed without trusting version alone'
    elif field == 'parent': client.parent['title'] = 'Changed parent'
    elif field == 'profile': client.target['policy']['resources'] = []
    else: client.policy['allow_requests'] = False
    with pytest.raises(DevelopmentError):
        controller.apply(plan, catalog, approved_sha256=approved)
    assert not client.created


def test_different_plan_cannot_reuse_journal_even_with_new_approval(case):
    plan, catalog, client, controller = case
    approved = controller.preview(plan, catalog)['approval_sha256']
    controller.apply(plan, catalog, approved_sha256=approved)
    plan['steps'][0]['title'] = 'A different plan'
    new = controller.preview(plan, catalog)['approval_sha256']
    with pytest.raises(DevelopmentError, match='team_ticket_journal_conflict'):
        controller.apply(plan, catalog, approved_sha256=new)
    assert len(client.tickets) == 3


def test_wrong_creation_response_is_not_recorded_or_repaired(case):
    plan, catalog, client, controller = case
    approved = controller.preview(plan, catalog)['approval_sha256']; client.wrong = True
    with pytest.raises(DevelopmentError, match='team_ticket_creation_unconfirmed'):
        controller.apply(plan, catalog, approved_sha256=approved)
    assert decode((controller.directory / 'receipts.json').read_bytes())['tickets'] == {}


def test_unknown_or_changed_receipt_refuses(case):
    plan, catalog, client, controller = case
    approved = controller.preview(plan, catalog)['approval_sha256']
    controller.apply(plan, catalog, approved_sha256=approved)
    with PrivateDirectory(controller.directory) as private:
        state = decode(private.read('receipts.json', 100000)); state['tickets']['a']['payload_sha256'] = '0' * 64
        private.replace('receipts.json', canonical(state))
    with pytest.raises(DevelopmentError, match='team_ticket_receipt_changed'):
        controller.apply(plan, catalog, approved_sha256=approved)


def test_journal_flock_excludes_another_controller(case):
    plan, catalog, _, controller = case
    approved = controller.preview(plan, catalog)['approval_sha256']
    with controller._locked():
        with pytest.raises(DevelopmentError, match='team_ticket_journal_busy'):
            controller.apply(plan, catalog, approved_sha256=approved)


@pytest.mark.parametrize('mutation', [
    lambda p, c: p.update(extra=True),
    lambda p, c: c['slots'][0].update(extra=True),
    lambda p, c: p['steps'][0].update(extra=True),
    lambda p, c: p.update(project_version=True),
    lambda p, c: c['slots'][0].update(profile_version=True),
    lambda p, c: c['slots'][0].update(resource='runtime:all'),
    lambda p, c: p['steps'][0].update(slot_id='missing'),
    lambda p, c: p['steps'][0].update(depends_on=['missing']),
    lambda p, c: p['steps'][0].update(depends_on=['a', 'a']),
    lambda p, c: p['steps'][0].update(title='🙂' * 51),
    lambda p, c: p['steps'][0].update(description='x' * 4001),
    lambda p, c: p['steps'][0].update(milestone='a\nb'),
    lambda p, c: p['steps'][0].update(acceptance_criteria='ghp_' + 'a' * 30),
    lambda p, c: p['steps'][0].update(title='\udfff'),
    lambda p, c: p['steps'][0].update(step_id='x' * 200),
    lambda p, c: p.update(parent_ticket_id='x' * 200),
])
def test_closed_types_prose_and_secret_bounds(mutation):
    plan, catalog = inputs(); mutation(plan, catalog)
    with pytest.raises(DevelopmentError):
        documents(plan, catalog)


def test_cycle_and_empty_or_excess_steps_refuse():
    plan, catalog = inputs(); plan['steps'][-1]['depends_on'] = ['c']
    with pytest.raises(DevelopmentError, match='team_ticket_plan_cycle'):
        documents(plan, catalog)
    for count in (0, 17):
        plan, catalog = inputs(); plan['steps'] = [deepcopy(plan['steps'][0]) for _ in range(count)]
        with pytest.raises(DevelopmentError): documents(plan, catalog)


def test_narrow_client_does_not_enable_worker_commands_or_implicit_registration(tmp_path):
    path = tmp_path / 'connection.json'
    path.write_text(json.dumps(dict(mcp_url='http://localhost/mcp', token='synthetic-token',
                                  client='codex', identity_key='lead', name='Lead')))
    path.chmod(0o600)
    with TeamTicketBoardClient.from_connection_file(path, session_id='lead') as client:
        for name in ('claim_ticket', 'update_ticket', 'post_message', 'reserve_execution', 'create_managed_agent_profile'):
            with pytest.raises(BoardReadError, match='tool_not_allowed'): client.call(name, {})
        with pytest.raises(BoardReadError, match='team_ticket_registration_required'):
            client.call('get_project', {'project_id': 'project'})
        with pytest.raises(BoardReadError, match='invalid_arguments'):
            client.call('get_project', {'project_id': 'project', 'session_id': 'other'})


def test_cli_preview_apply_and_invalid_approval_have_no_provider_path(tmp_path, monkeypatch, capsys):
    plan, catalog = inputs(); client = BoardClient()
    client.__enter__ = lambda: client
    class Open:
        def __enter__(self): return client
        def __exit__(self, *_): pass
    monkeypatch.setattr(team_ticket_cli.TeamTicketBoardClient, 'from_connection_file', lambda *_a, **_kw: Open())
    plan_path = tmp_path / 'plan.json'; plan_path.write_bytes(canonical(plan))
    catalog_path = tmp_path / 'catalog.json'; catalog_path.write_bytes(canonical(catalog))
    args = ['--connection-file', str(tmp_path / 'private.json'), '--session', client.session_id,
            '--plan', str(plan_path), '--catalog', str(catalog_path)]
    assert cli.main(['team-plan', 'preview', *args]) == 0
    preview = json.loads(capsys.readouterr().out)
    apply = ['team-plan', 'apply', *args, '--journal', str(tmp_path / 'journal'),
             '--approve-sha256', preview['approval_sha256']]
    assert cli.main(apply) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'complete'
    assert len(client.created) == 3
    apply[-1] = '0' * 64
    assert cli.main(apply) == 2
    assert json.loads(capsys.readouterr().out)['reason'] == 'team_ticket_approval_changed'


def test_document_reader_rejects_duplicate_keys_and_symlinks(tmp_path):
    file = tmp_path / 'plan.json'; file.write_text('{"schema":1,"schema":2}')
    with pytest.raises(DevelopmentError): team_ticket_cli.read_document(file)
    link = tmp_path / 'link'; link.symlink_to(file)
    with pytest.raises(DevelopmentError): team_ticket_cli.read_document(link)
