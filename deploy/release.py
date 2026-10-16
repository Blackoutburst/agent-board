#!/usr/bin/env python3
"""Deploy a clean, tested Git checkout to the existing supervised board service."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backup import create_backup


def command(*args):
    return subprocess.check_output(args,text=True).strip()


def service_pid():
    return int(command('systemctl','show','collaboration-board.service','--property=MainPID','--value') or 0)


def verify_process(pid,live):
    if pid<=1:
        raise RuntimeError('The expected board service is not running')
    process=Path('/proc')/str(pid)
    if process.stat().st_uid!=os.getuid() or (process/'cwd').resolve()!=live:
        raise RuntimeError('Refusing to signal a process outside the current user/live source directory')
    arguments=(process/'cmdline').read_bytes().replace(b'\0',b' ').decode()
    if 'uvicorn app:app' not in arguments or '--port 9140' not in arguments:
        raise RuntimeError('The service PID is not the expected board backend')


def atomic_copy(source,destination):
    destination.parent.mkdir(parents=True,exist_ok=True)
    temporary=destination.with_name('.'+destination.name+'.release-temp')
    try:
        shutil.copy2(source,temporary)
        os.replace(temporary,destination)
    finally:
        if temporary.exists(): temporary.unlink()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,default=Path(__file__).resolve().parents[1])
    parser.add_argument('--live',type=Path,default=Path('/home/ubuntu/collaboration-board'))
    parser.add_argument('--data-dir',type=Path,default=Path('/home/ubuntu/.local/share/collaboration-board'))
    parser.add_argument('--backup-root',type=Path,default=Path('/home/ubuntu/.local/share/collaboration-board-source-backups'))
    parser.add_argument('--supervised-restart',action='store_true',help='Allow a validated same-user process restart through systemd Restart=on-failure')
    args=parser.parse_args()
    source=args.source.resolve(strict=True);live=args.live.resolve(strict=True);data=args.data_dir.resolve(strict=True)
    if source==live or not (source/'.git').exists():
        raise RuntimeError('Use a separate Git source checkout')
    if command('git','-C',str(source),'status','--porcelain'):
        raise RuntimeError('Commit and verify a clean source checkout before deploying')
    revision=command('git','-C',str(source),'rev-parse','HEAD')
    files=command('git','-C',str(source),'ls-files','-z').split('\0')
    files=[name for name in files if name]
    for name in files:
        path=source/name
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(source):
            raise RuntimeError('Source inventory contains a symlink or nonregular file')
    pid=service_pid();verify_process(pid,live)
    args.backup_root.mkdir(parents=True,exist_ok=True,mode=0o700)
    args.backup_root.chmod(0o700)
    with (args.backup_root/'.release.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        snapshot=args.backup_root/('before-v2-'+stamp)
        snapshot.mkdir(mode=0o700)
        backup=create_backup(data,snapshot/'data.zip')
        old_files=[]
        for name in files:
            original=live/name
            if original.exists():
                if original.is_symlink(): raise RuntimeError('Live source contains a symlink')
                atomic_copy(original,snapshot/'source'/name)
                old_files.append(name)
        metadata={'revision':revision,'live':str(live),'data_dir':str(data),'previous_source_files':old_files,
                  'added_files':sorted(set(files)-set(old_files)),'backup_schema':backup['schema']}
        (snapshot/'release.json').write_text(json.dumps(metadata,indent=2)+'\n')
        # Existing service modules are already loaded. Replace files atomically and
        # restart immediately so new browser assets and backend share one release.
        for name in files:
            atomic_copy(source/name,live/name)
        restarted=subprocess.run(['systemctl','--no-ask-password','restart','collaboration-board.service'],
                                 capture_output=True,text=True,timeout=15)
        if restarted.returncode:
            if not args.supervised_restart:
                raise RuntimeError('Service restart needs authorization; use the documented same-user supervisor option only when intended. Backup: '+str(snapshot))
            pid=service_pid();verify_process(pid,live)
            os.kill(pid,signal.SIGKILL)
        deadline=time.monotonic()+40
        health=None
        while time.monotonic()<deadline:
            try:
                with urllib.request.urlopen('http://127.0.0.1:9140/health',timeout=2) as response:
                    health=json.load(response)
                if health.get('version')=='2.6.0' and health.get('schema_version')==8:
                    break
            except (OSError,ValueError):
                pass
            time.sleep(.4)
        else:
            raise RuntimeError('Health did not recover; matching source/data rollback snapshot: '+str(snapshot))
        with sqlite3.connect(data.joinpath('board.sqlite3').as_uri()+'?mode=ro',uri=True) as db:
            integrity=db.execute('PRAGMA integrity_check').fetchone()[0]
            foreign_keys=db.execute('PRAGMA foreign_key_check').fetchall()
            counts={table:db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0] for table in
                    ('projects','tickets','messages','attachments','ticket_attachments','agent_identities','agent_profiles','agent_profile_bindings','agent_profile_versions','sessions')}
        mismatches=[name for name in files if hashlib.sha256((source/name).read_bytes()).digest()!=hashlib.sha256((live/name).read_bytes()).digest()]
        if integrity!='ok' or foreign_keys or mismatches:
            raise RuntimeError('Post-release integrity or source verification failed; snapshot: '+str(snapshot))
        receipt={'revision':revision,'backup':str(snapshot),'health':health,'service_pid':service_pid(),
                 'verified_source_files':len(files),'source_mismatches':mismatches,'database_integrity':integrity,
                 'foreign_key_errors':foreign_keys,'record_counts':counts}
        (snapshot/'verified-release.json').write_text(json.dumps(receipt,indent=2)+'\n')
        print(json.dumps(receipt,indent=2))


if __name__=='__main__':
    main()
