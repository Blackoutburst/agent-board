"""Fixed group leader for the publisher's single bounded Git/gh command.

The two private descriptors never reach the command. Only its integer exit code
leaves this helper; it remains alive until the controller kills the owned group.
"""
import os
import select
import signal
import subprocess
import sys


def main():
    status, life = int(sys.argv[1]), int(sys.argv[2])
    if status <= 2 or life <= 2 or status == life:
        raise ValueError('private descriptors required')
    os.set_inheritable(status, False); os.set_inheritable(life, False)
    try:
        result = b'error\n'
        try:
            arguments = sys.argv[4:]
            if (sys.argv[3] != '--' or not 1 <= len(arguments) <= 64
                    or any(not argument or len(argument.encode()) > 8192 for argument in arguments)):
                raise ValueError('closed argv required')
            command = subprocess.Popen(arguments, stdin=subprocess.DEVNULL, close_fds=True)
            while command.poll() is None:
                if select.select([life],[],[],.05)[0]:
                    return  # Controller disappeared while the command ran.
            code = command.returncode
            if type(code) is int and -255 <= code <= 255:
                result = (str(code)+'\n').encode('ascii')
        except BaseException:
            pass
        try:
            if os.write(status, result) != len(result):
                raise OSError('incomplete private status')
        finally:
            os.close(status)
        # The live leader pins its PID/PGID. Any controller disappearance or
        # status-pipe failure reaches the same group cleanup while it is alive.
        while os.read(life, 1):
            pass
    finally:
        os.killpg(os.getpgrp(), signal.SIGKILL)


if __name__ == '__main__':
    main()
