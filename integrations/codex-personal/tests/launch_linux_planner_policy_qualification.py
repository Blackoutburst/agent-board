"""Reviewed entry point: execute the qualifier from held SHA-pinned public bytes.

The operator must pin THIS launcher externally when deploying/running it. It
makes no self-hash claim. No CLI, model or account is touched by the launcher.
Usage: python3 -I -B -S launcher.py /absolute/qualify_linux_planner_policy.py SHA -- [qualifier args]
"""
import hashlib
import os
from pathlib import Path
import re
import stat
import sys


def held_public(path, maximum):
    if (type(path) is not str or not path.startswith('/') or str(Path(path)) != path
            or any(part in ('.','..','') for part in path.split('/')[1:])):
        raise ValueError('public_path_invalid')
    descriptor=stream=None
    try:
        descriptor=os.open('/',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        for part in Path(path).parts[1:-1]:
            child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=descriptor)
            os.close(descriptor);descriptor=child
        stream=os.open(Path(path).name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=descriptor)
        before=os.fstat(stream)
        def pin(info):return (info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns,info.st_mode,info.st_uid,info.st_nlink)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink!=1 or before.st_uid not in (0,os.geteuid())
                or before.st_mode&0o022 or not 0<before.st_size<=maximum):raise ValueError('public_file_invalid')
        data=b''
        while len(data)<=maximum:
            part=os.read(stream,min(65536,maximum+1-len(data)))
            if not part:break
            data+=part
        if (len(data)>maximum or pin(os.fstat(stream))!=pin(before)
                or pin(os.stat(Path(path).name,dir_fd=descriptor,follow_symlinks=False))!=pin(before)):
            raise ValueError('public_file_changed')
        return data
    finally:
        if stream is not None:os.close(stream)
        if descriptor is not None:os.close(descriptor)


def main(argv):
    if (len(argv)<3 or Path(argv[0]).name!='qualify_linux_planner_policy.py'
            or re.fullmatch('[0-9a-f]{64}',argv[1]) is None or argv[2]!='--'):
        raise ValueError('explicit_qualifier_pin_required')
    data=held_public(argv[0],96*1024)
    if hashlib.sha256(data).hexdigest()!=argv[1]:raise ValueError('qualifier_pin_mismatch')
    namespace={'__name__':'__main__','__file__':argv[0],'__package__':None,
               'VERIFIED_RUNNER_SHA256':argv[1]}
    sys.argv=[argv[0],*argv[3:]]
    exec(compile(data,argv[0],'exec',dont_inherit=True),namespace)


if __name__=='__main__':
    try:main(sys.argv[1:])
    except (OSError,ValueError):
        print('{"result":"refused","reason":"qualification_launcher_refused"}')
        raise SystemExit(2)
