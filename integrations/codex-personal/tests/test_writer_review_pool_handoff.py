"""Raccord réel Pool coder -> publication -> Pool reviewer -> writer source.

The Board, Git, journals, Runtime and SDK components are real. Model transports,
native qualification observations and GitHub network are synthetic fixtures.
"""
from contextlib import ExitStack
import threading

import pytest

import test_review_pool as fixture
from blackoutburst_agents.dev_manifest import DevelopmentError
from blackoutburst_agents.team_writer_source import TeamWriterSource


@pytest.fixture
def completed_review():
    helper=fixture.ReviewPoolTests()
    try:
        f=helper.fixture()
        result=helper.run_review(f)
        assert result['state']=='settled' and result['outcome']=='complete' and result['cleanup_confirmed'] is True
        f.writer_review_context=f.review_pool._context(next(row for row in f.review_pool.journal.read()['jobs']
            if row['id']==f.review_job['id']))
        yield f
    finally:helper.doCleanups()


def read_factory(f):
    resources=ExitStack()
    try:
        deliveries,worker=f.review_driver.open_completed_source(resources,f.writer_review_context)
        return TeamWriterSource(resources,deliveries,worker,delivery_operation_id=f.operation,
            review_export_id=f.review_job['id'],review_operation_id=f.review_job['id'])
    except BaseException:resources.close();raise


def counts(f):
    return (f.pushes,f.posts,len(f.inferences),len(f.backend.calls),len(f.review_turns),
        sum(len(client.calls) for _,client in f.review_clients),
        sum(len(client.calls) for _,client in f.review_controls))


def test_completed_real_review_handoff_is_historical_source_without_effects(completed_review):
    f=completed_review;before=counts(f)
    with read_factory(f) as source:
        value=source.read()
        assert value['historical_data'] is True
        assert value['review_operation_id']==f.review_job['id']
        assert value['selection']['delivery_sha']==f.pipeline.status(f.operation)['delivery_sha']
        assert value['opinion']['verdict']=='corrections'
        assert value['reviewer_agent_id']!=value['selection']['author_agent_id']
    assert counts(f)==before


def test_review_handoff_factory_reopens_actual_handles_on_consumer_thread(completed_review):
    f=completed_review;before=counts(f);values=[];errors=[]
    def read():
        try:
            with read_factory(f) as source:values.append(source.read())
        except BaseException as error:errors.append(error)
    thread=threading.Thread(target=read);thread.start();thread.join(timeout=30)
    assert not thread.is_alive() and not errors and len(values)==1,errors
    assert counts(f)==before


def test_corrupted_actual_review_record_cannot_become_writer_material(completed_review):
    f=completed_review;before=counts(f)
    with read_factory(f) as source:
        op=source.worker.journal.get(f.review_job['id'])
        source.worker.run_store._db.execute("UPDATE turns SET status='blocked' WHERE id=?",(op['payload']['turn_id'],))
        with pytest.raises(DevelopmentError):source.read()
    assert counts(f)==before
