"""A readonly guard must reject page maintenance invisible to row snapshots."""
import pytest

from test_message_append_children import roster
from test_message_append_host5 import proposal,proof
from blackoutburst_agents.message_append_children import locked_message_append_children
from blackoutburst_agents.host_config import HostError


@pytest.mark.parametrize('target',['host','messages','runs','runtime'])
def test_source_guard_cannot_vacuum_any_owned_journal(roster,target):
    f=roster
    child={'host':f.host._db,'messages':f.messages.db,
        'runs':f.message_runs._db,'runtime':f.message_runtime.db}[target]
    child.execute('PRAGMA auto_vacuum=INCREMENTAL');child.execute('VACUUM')
    assert child.execute('PRAGMA auto_vacuum').fetchone()[0]==2
    child.execute('CREATE TABLE temporary_freelist_fixture(data BLOB)')
    child.execute('INSERT INTO temporary_freelist_fixture VALUES(zeroblob(65536))')
    child.execute('DROP TABLE temporary_freelist_fixture')
    before=child.execute('PRAGMA freelist_count').fetchone()[0];assert before>0
    with f.locked(),locked_message_append_children(f.host,f.messages,f.message_runs,f.message_runtime) as children:
        p=proposal(f,children)
        def maintenance():child.execute('PRAGMA incremental_vacuum').fetchall()
        with pytest.raises(HostError):
            f.host.begin_messages_append(p,proof(p,'prepared'),children=children,recheck=maintenance)
        assert child.execute('PRAGMA freelist_count').fetchone()[0]==before
