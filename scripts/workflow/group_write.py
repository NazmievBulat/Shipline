"""Writes a value to a control group file that must already exist, in one write.

    group_write.py <file> <value>

The file is opened without being created, and emptied as `>` would. A write the kernel refuses
with EAGAIN exits 75: for `memory.reclaim` it means the kernel freed only part of what was
asked. Any other failure exits 1. Each failure prints `<file>: <reason>`.

The kernel's errno decides, not the words of whichever `dd` is installed.
"""

import errno
import os
import sys

PARTLY_DONE = 75


def write(file: str, value: str) -> int:
    try:
        fd = os.open(file, os.O_WRONLY | os.O_TRUNC | os.O_CLOEXEC)
    except OSError as error:
        print(f"{file}: {error.strerror}", file=sys.stderr)
        return 1
    try:
        os.write(fd, value.encode())
    except OSError as error:
        print(f"{file}: {error.strerror}", file=sys.stderr)
        return PARTLY_DONE if error.errno == errno.EAGAIN else 1
    finally:
        os.close(fd)
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("usage: group_write.py <file> <value>", file=sys.stderr)
        sys.exit(2)
    sys.exit(write(sys.argv[1], sys.argv[2]))
