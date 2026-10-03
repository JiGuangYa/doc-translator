"""Small child-process owner that survives a crashed translation engine long enough
to terminate its own conversion process group. It never opens application data.
"""

import os
import signal
import subprocess
import sys
import time


def kill_group(process: subprocess.Popen) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
    else:
        try:
            # The session id is the child's pid, even after the group leader exits.
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def main(arguments: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if arguments is None else arguments)
    if len(args) < 3 or args[1] != "--":
        return 2
    parent_pid = int(args[0])
    stopped = False

    def stop(_signum, _frame):
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    process = subprocess.Popen(args[2:], start_new_session=os.name != "nt")
    try:
        while True:
            if stopped or os.getppid() != parent_pid:
                return 143
            code = process.poll()
            if code is not None:
                return code
            time.sleep(0.05)
    finally:
        kill_group(process)
        process.wait(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
