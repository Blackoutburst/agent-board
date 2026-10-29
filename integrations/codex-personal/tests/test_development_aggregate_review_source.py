"""Actual published aggregate -> actual review Lifecycle; only external seams fake."""
from contextlib import ExitStack, closing
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import uuid

import pytest

from blackoutburst_agents.development_aggregate_review_source import PublishedAggregateReviewSource, PublishedAggregateReadView
from blackoutburst_agents.development_pool_selection import file_document
from blackoutburst_agents.dev_journal import PrivateDirectory, decode
from blackoutburst_agents.dev_manifest import DevelopmentError, canonical, digest
from blackoutburst_agents.review_client import DeliveryReviewBoardClient
from blackoutburst_agents.review_lifecycle_client import DeliveryReviewLifecycleClient
from blackoutburst_agents.review_lifecycle import DeliveryReviewLifecycle
from blackoutburst_agents.review_pool import DeliveryReviewPoolDriver, HistoricalPoolDeliveryTests
from blackoutburst_agents.worker_pool import WorkerPool
from blackoutburst_agents.git_publication import GitHubDraftPublisher, API, REMOTE
from test_runtime_integration import ControlClient
from test_development_aggregate_delivery import aggregate_fixture, publisher_fixture, linux_lock_observation


def actual_lifecycle(f, aggregate, publisher, monkeypatch, stack):
    monkeypatch.setattr(PublishedAggregateReadView, '_run', lambda _reader, argv, **kwargs: publisher._command(argv, **kwargs))
    source = PublishedAggregateReviewSource(aggregate, publisher)
    device = f.board.create_device(f.human, 'Independent aggregate reviewer', [])
    profile = f.board.create_agent_profile(f.human, 'Independent review', project_ids=[f.project['id']], policy={
        'allow_read': True, 'allow_messages': False, 'allow_requests': True, 'actions': [], 'resources': [], 'autonomy': 'requested'})
    f.board.bind_agent_profile(f.human, profile['id'], profile['version'], device['id'], 'reviewer', 'codex')
    raw = f.board.authenticate(device['token'])
    connection = f.base/'review-connection.json'
    with PrivateDirectory(f.base) as private:
        private.create(connection.name, canonical({'mcp_url': f.seed.endpoint, 'token': device['token'],
            'client': 'codex', 'identity_key': 'reviewer', 'name': 'Reviewer'}))
    driver = DeliveryReviewPoolDriver(f.base/'review-selections', connection_file=connection,
        owner_id='owner', agent_id=profile['id'], board_endpoint=f.seed.endpoint)
    pool = stack.enter_context(closing(WorkerPool(f.base/'review-pool', concurrency=1, drivers={'review': driver.binding()})))
    calls, turns = [], []

    def client_factory(cls):
        def create(path, *, session_id, timeout):
            client = cls(decode(file_document(path)[0]), session_id, timeout)
            client.connection_identity = tuple(file_document(path)[1]['identity'])
            client._initialize = lambda: None
            def tool(name, args):
                calls.append((name, deepcopy(args)))
                if name == 'register_agent':
                    return f.board.register_agent(raw, **args)
                actor = f.board.actor_session(raw, session_id)
                values = {key: value for key, value in args.items() if key != 'session_id'}
                if name == 'update_ticket':
                    values.update(values.pop('changes'))
                value = getattr(f.board, name)(actor, **values)
                return {'items': value} if name == 'list_activity' else value
            client._tool = tool
            return client
        return create

    monkeypatch.setattr(DeliveryReviewLifecycleClient, 'from_connection_file', client_factory(DeliveryReviewLifecycleClient))
    monkeypatch.setattr(DeliveryReviewBoardClient, 'from_connection_file', client_factory(DeliveryReviewBoardClient))
    def control(client):
        result = ControlClient(f.board, raw, client.session_id)
        result.close = lambda: None
        return result
    monkeypatch.setattr('blackoutburst_agents.review_pool.RuntimeBoardClient.from_worker', control)
    def transport(settings, prompt, workspace, **callbacks):
        callbacks['before_prompt']()
        callbacks['on_started'](999999999)
        sid = callbacks['resume_session_id'] or str(uuid.uuid4())
        callbacks['on_session'](sid)
        turns.append(prompt)
        return {'status': 'completed', 'session_id': sid,
            'text': json.dumps({'verdict': 'corrections', 'text': 'Add a boundary test for the aggregate.'}),
            'verification': {'cleanup_confirmed': True, 'tool_free': True}}
    monkeypatch.setattr('blackoutburst_agents.linux_review_binding.execute_turn', transport)
    lifecycle = DeliveryReviewLifecycle(f.base/'review-lifecycle', pool=pool, driver=driver, source=source,
        executable=f.tools['codex'], cli_version='codex-cli 0.163.0', interpreter=f.tools['python'])
    ticket = aggregate.final_proof.scope['ticket_id']
    job = pool.submit(request_id='independent-aggregate-review', driver_id='review',
        payload={'project_id': f.project['id'], 'ticket_id': ticket})
    return source, lifecycle, pool, job, calls, turns, profile


def test_real_lifecycle_reviews_published_aggregate_without_reexecuting_author(
        tmp_path, monkeypatch, linux_lock_observation):
    def check(f, aggregate, worker, directory, author_turns):
        seal = aggregate.seal(message='Final aggregate')
        aggregate.complete_local()
        publisher, public = publisher_fixture(f, aggregate, seal)
        aggregate.publish_and_handoff(publisher, title='Final aggregate', body='Final source and tests.')
        with ExitStack() as stack:
            source, lifecycle, pool, job, calls, turns, profile = actual_lifecycle(f, aggregate, publisher, monkeypatch, stack)
            before = source.status(aggregate.operation_id)
            assert before['accepted'] is False and before['author_scope']['agent_id'] != profile['id']
            immutable = {name: (directory/'broker'/name).read_bytes() for name in ('manifest.json', 'state.json', 'planner.json')}
            prepared = lifecycle.prepare(job, source_operation_id=aggregate.operation_id,
                instruction='Independently review the exact aggregate.', material='one.py: value = 1 becomes value = 3.',
                model='explicit-independent-review-model', timeout=60, skills=['review'])
            assert prepared['status'] == 'prepared'
            ids = pool.dispatch_pending()
            assert ids == [job['id']]
            pool.threads[job['id']].join(timeout=300)
            assert not pool.threads[job['id']].is_alive()
            settled = next(row for row in pool.status()['jobs'] if row['id'] == job['id'])
            assert (settled['state'], settled['outcome'], settled['cleanup_confirmed']) == ('settled', 'complete', True), settled
            result = lifecycle.publish(job['id'])
            assert result['status'] == 'complete'
            ticket = f.board.get_ticket(f.human, aggregate.final_proof.scope['ticket_id'])['ticket']
            assert ticket['status'] == 'review' and ticket['claimant'] is None
            assert 'Add a boundary test for the aggregate.' in ticket['outcome']
            assert '/pull/17' in ticket['evidence'] and seal['delivery_sha'] in ticket['evidence']
            assert len(turns) == 1 and len(author_turns) == 3
            assert public['pushes'] == public['posts'] == 1
            assert {name: (directory/'broker'/name).read_bytes() for name in immutable} == immutable
            assert source.status(aggregate.operation_id) == before
            assert lifecycle.publish(job['id']) == result
    aggregate_fixture(tmp_path, monkeypatch, linux_lock_observation, check)


def test_declared_receipt_cannot_be_an_aggregate_review_source(tmp_path):
    with pytest.raises(DevelopmentError, match='aggregate_review_concrete_source_required'):
        PublishedAggregateReviewSource({'published': True}, {'pull_request': 17})
    with pytest.raises(DevelopmentError, match='review_pool_concrete_source_required'):
        HistoricalPoolDeliveryTests({'status': 'complete'}, str(uuid.uuid4()), {})


def test_historical_reader_never_gains_mutation_or_ignores_fresh_drift(
        tmp_path, monkeypatch, linux_lock_observation):
    def check(f, aggregate, worker, directory, author_turns):
        seal = aggregate.seal(message='Final aggregate')
        aggregate.complete_local()
        publisher, public = publisher_fixture(f, aggregate, seal)
        aggregate.publish_and_handoff(publisher, title='Final aggregate', body='Final source and tests.')
        monkeypatch.setattr(PublishedAggregateReadView, '_run', lambda _reader, argv, **kwargs: publisher._command(argv, **kwargs))
        source = PublishedAggregateReviewSource(aggregate, publisher)
        reader = source.reader
        before = source.status(aggregate.operation_id)
        publication_bytes = (publisher.root/aggregate.operation_id/'publication.json').read_bytes()
        arguments = dict(operation_id=aggregate.operation_id, delivery_sha=seal['delivery_sha'], title='Forbidden', body='Forbidden')
        for call in (lambda: reader.publish(**arguments), lambda: GitHubDraftPublisher.publish(reader, **arguments),
                     lambda: reader._authorize_effect(None),
                     lambda: GitHubDraftPublisher._authorize_effect(reader, None, None, None, None, None)):
            with pytest.raises(DevelopmentError, match='git_publication_readonly_view'):
                call()
        launches = []
        run = reader._run
        monkeypatch.setattr(reader, '_run', lambda *args, **kwargs: launches.append(args) or pytest.fail('mutating reader launched'))
        forbidden = [([str(reader.gh), 'api', '--hostname', 'github.com', '--method', 'POST', API+'/pulls'], False),
            ([str(reader.gh), 'api', '--hostname', 'github.com', '--method', 'DELETE', API], False),
            ([str(reader.git), 'push', REMOTE, 'HEAD:main'], True)]
        for argv, git in forbidden:
            with pytest.raises(DevelopmentError, match='aggregate_review_command_refused'):
                reader._command(argv, cwd=reader.root/aggregate.operation_id, git=git)
        assert launches == []
        monkeypatch.setattr(reader, '_run', run)
        # Even an unbound call to Publisher's bounded command runner must
        # enforce this view's fixed GET/read allowlist before its launcher.
        original_popen = subprocess.Popen
        with monkeypatch.context() as direct:
            def checked_popen(argv, *args, **kwargs):
                if str(reader.guardian) in argv:
                    launches.append(argv)
                    pytest.fail('unbound mutating command launched')
                return original_popen(argv, *args, **kwargs)
            direct.setattr(subprocess, 'Popen', checked_popen)
            for argv, git in forbidden:
                with pytest.raises(DevelopmentError, match='aggregate_review_command_refused'):
                    GitHubDraftPublisher._command(reader, argv,
                        cwd=reader.root/aggregate.operation_id, git=git)
        assert launches == []
        assert (publisher.root/aggregate.operation_id/'publication.json').read_bytes() == publication_bytes
        # Each reversible fixture mutation is restored before the next case;
        # the append-only logical Stop is deliberately last and is not undone.
        final_path = Path(source.frame['final_root'])/'one.py'
        raw = final_path.read_bytes()
        final_path.write_text('unverified source\n')
        with pytest.raises(DevelopmentError):source.status(aggregate.operation_id)
        final_path.write_bytes(raw)
        with PrivateDirectory(source.frame['fence_root']) as private:
            fence = decode(private.read('fence.json', 2*1024*1024))
            changed = deepcopy(fence)
            changed['records'][source.frame['fence_key']]['operation_id'] = str(uuid.uuid4())
            private.replace('fence.json', canonical(changed))
        with pytest.raises(DevelopmentError):source.status(aggregate.operation_id)
        with PrivateDirectory(source.frame['fence_root']) as private:private.replace('fence.json', canonical(fence))
        branch = reader.request['branch']
        reader.request['branch'] = 'codex/a-different-delivery'
        with pytest.raises(DevelopmentError):source.status(aggregate.operation_id)
        reader.request['branch'] = branch
        head = public['prs'][0]['head']['sha']
        public['prs'][0]['head']['sha'] = '0'*40
        with pytest.raises(DevelopmentError):source.status(aggregate.operation_id)
        public['prs'][0]['head']['sha'] = head
        f.board.set_policy(f.human, True, True, True, False, f.actions, ['repo:fixture'])
        with pytest.raises(DevelopmentError):source.status(aggregate.operation_id)
        f.board.set_policy(f.human, True, True, True, True, f.actions, ['repo:fixture'])
        assert source.status(aggregate.operation_id) == before
        state = aggregate.task.snapshot()['state']
        aggregate.task.stop(expected_revision=state['revision'], request_id='stop-historical-source')
        with pytest.raises(DevelopmentError):source.status(aggregate.operation_id)
        assert public['pushes'] == public['posts'] == 1 and len(author_turns) == 3
        assert before['accepted'] is False
    aggregate_fixture(tmp_path, monkeypatch, linux_lock_observation, check)
