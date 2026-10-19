"""Children of a stopped delegation family never look ready (2.13.1)."""
import sqlite3
import time

from test_delegation_families import family, member, open_family  # noqa: F401  (fixture)


def stopped_child(f):
    b = f['b']
    opened = open_family(f)
    child_id = member(f, opened)['ticket_id']
    b.stop_delegation_family(f['actors'][0], opened['id'], opened['revision'])
    return b, child_id


def store_block(b, ticket_id, reason='', recheck_at=None):
    with sqlite3.connect(b.db_path) as db:
        db.execute("UPDATE tickets SET status='blocked',claimant=NULL,lease_expires_at=NULL,blocked_reason=?,"
                   "recheck_at=? WHERE id=?", (reason, recheck_at, ticket_id))


def listed(b, actor, project_id, ticket_id):
    work = b.get_work(actor, project_id)
    return ticket_id in [item['id'] for item in work['ready'] + work['recovery']]


def test_todo_child_of_a_cancelled_family_is_not_ready(family):
    f = family
    b, child_id = stopped_child(f)
    ticket = b.get_ticket(f['h'], child_id)['ticket']
    assert ticket['status'] == 'todo'
    assert (ticket['ready'], ticket['work_state'], ticket['block_reasons']) == (False, 'blocked', ['delegation'])
    assert not listed(b, f['h'], f['project']['id'], child_id)
    assert not listed(b, f['actors'][1], f['project']['id'], child_id)


def test_reasonless_or_expired_blocks_of_a_cancelled_family_stay_blocked(family):
    f = family
    b, child_id = stopped_child(f)
    store_block(b, child_id)
    b.get_work(f['h'], f['project']['id'])
    ticket = b.get_ticket(f['h'], child_id)['ticket']
    assert ticket['status'] == 'blocked' and 'delegation' in ticket['block_reasons']
    store_block(b, child_id, 'Waiting for the family', time.time() - 1)
    version = b.get_ticket(f['h'], child_id)['ticket']['version']
    b.get_work(f['h'], f['project']['id'])
    ticket = b.get_ticket(f['h'], child_id)['ticket']
    assert ticket['status'] == 'blocked' and ticket['version'] == version and ticket['ready'] is False
    assert not [e for e in b.list_activity(f['h'], ticket_id=child_id) if e['event'] == 'ticket_unblocked']


def test_ordinary_ticket_and_running_family_children_are_unchanged(family):
    f = family
    b = f['b']
    ordinary = b.create_ticket(f['h'], f['project']['id'], 'Ordinary', idempotency_key='ordinary')
    ticket = b.get_ticket(f['h'], ordinary['id'])['ticket']
    assert (ticket['ready'], ticket['work_state'], ticket['block_reasons']) == (True, 'ready', [])
    opened = open_family(f)
    child_id = member(f, opened)['ticket_id']
    assert b.get_ticket(f['h'], child_id)['ticket']['block_reasons'] == []
