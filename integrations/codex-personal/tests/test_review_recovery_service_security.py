"""Independent crash/ACK counters; synthetic review seam, real local Pool/queue.

No actual owner account, server, native execution or model is accessed.
"""
import json

import pytest

from blackoutburst_agents import review_recovery_service as module
from blackoutburst_agents.dev_journal import PrivateDirectory
from blackoutburst_agents.dev_manifest import DevelopmentError,canonical
from test_review_recovery_service import recovery,case


def registry(f):
    return f.root/'review-recoveries'/f.value['recovery_id']/'registry.json'


def archive(f):
    return f.root/'admissions'/('completed-by-review-'+f.value['recovery_id'])


def pending_never_replays(f):
    before=list(f.calls);next_before=list(f.next_calls)
    result=f.service().run_once()
    assert result=={'status':'needs_review','reason':'review_recovery_pending','activated':False}
    assert f.calls==before and f.next_calls==next_before


def test_existing_archive_is_never_overwritten_and_ambiguous_ack_never_advances(recovery):
    f=recovery;f.install_seam();archive(f).mkdir(mode=0o700)
    sentinel=archive(f)/'sentinel';sentinel.write_bytes(b'PRESERVE_EXISTING_ARCHIVE')
    with pytest.raises(AssertionError,match='archive must not replace'):f.service().run_once()
    assert sentinel.read_bytes()==b'PRESERVE_EXISTING_ARCHIVE'
    assert f.queue_path.exists() and not f.next_calls
    assert f.calls.count('model')==f.calls.count('publish')==1
    pending_never_replays(f)


def test_crash_after_ack_before_archive_retains_queue_and_never_redispatches(recovery,monkeypatch):
    f=recovery;f.install_seam()
    def crash(*args):raise RuntimeError('Synthetic failure after durable ACK')
    monkeypatch.setattr(module,'_rename_noreplace',crash)
    with pytest.raises(RuntimeError,match='after durable ACK'):f.service().run_once()
    assert f.queue_path.exists() and not archive(f).exists() and not f.next_calls
    assert (registry(f).parent/'queue-ack.json').exists()
    assert json.loads(registry(f).read_bytes())['phase']=='pending'
    pending_never_replays(f)


def test_crash_after_archive_before_registry_completion_never_replays_review(recovery,monkeypatch):
    f=recovery;f.install_seam();replace=PrivateDirectory.replace
    def crash(private,name,raw):
        if name=='registry.json' and json.loads(raw).get('phase')=='complete':
            raise RuntimeError('Synthetic crash after archive')
        return replace(private,name,raw)
    monkeypatch.setattr(PrivateDirectory,'replace',crash)
    with pytest.raises(RuntimeError,match='after archive'):f.service().run_once()
    assert not f.queue_path.exists() and archive(f).exists() and not f.next_calls
    assert json.loads(registry(f).read_bytes())['phase']=='pending'
    assert f.calls.count('model')==1
    pending_never_replays(f)


def test_stop_after_publication_keeps_old_admission_and_does_not_ack(recovery):
    f=recovery;f.install_seam()
    with pytest.raises(DevelopmentError,match='review_recovery_stopped'):
        f.service().run_once(should_stop=lambda:'publish' in f.calls)
    assert f.queue_path.exists() and not archive(f).exists() and not f.next_calls
    assert not (registry(f).parent/'queue-ack.json').exists()
    assert f.calls.count('model')==f.calls.count('publish')==1
    pending_never_replays(f)


def test_second_fresh_cleanup_refusal_after_publication_blocks_archive(recovery,monkeypatch):
    f=recovery;f.install_seam();clean=module.ReviewRecoveryService._clean;checks=[]
    def check(source):
        checks.append(1)
        if len(checks)==2:raise DevelopmentError('review_recovery_cleanup_unconfirmed')
        return clean(source)
    monkeypatch.setattr(module.ReviewRecoveryService,'_clean',check)
    with pytest.raises(DevelopmentError,match='review_recovery_cleanup_unconfirmed'):f.service().run_once()
    assert f.queue_path.exists() and not archive(f).exists() and not f.next_calls
    assert f.calls.count('publish')==1 and not (registry(f).parent/'queue-ack.json').exists()
    pending_never_replays(f)


@pytest.mark.parametrize('changed',['ack','archived_tick'])
def test_changed_ack_or_archived_state_cannot_advance_cached_following_queue(recovery,changed):
    f=recovery;f.install_seam();f.service().run_once()
    before=list(f.calls);next_before=list(f.next_calls)
    p=registry(f).parent/'queue-ack.json' if changed=='ack' else archive(f)/'tick.json'
    p.write_bytes(p.read_bytes()+b' ')
    with pytest.raises(DevelopmentError,match='review_recovery_changed'):f.service().run_once()
    assert f.calls==before and f.next_calls==next_before
    assert f.calls.count('model')==1


def test_valid_but_changed_following_selection_blocks_before_registry_or_review(recovery):
    f=recovery;f.install_seam()
    service=f.service();path=f.value['next_configuration_file']
    document=json.loads(open(path,'rb').read());document['provider_backoff_seconds']=120
    with open(path,'wb') as out:out.write(canonical(document))
    with pytest.raises(DevelopmentError):service.run_once()
    assert not (f.root/'review-recoveries').exists()
    assert f.queue_path.exists() and not f.calls and not f.next_calls
