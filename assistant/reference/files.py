"""Opening a dictionary's files: files of their own, and nothing else.

A dictionary is found by its name, and a pipe or a device can have any name. Opening a
pipe named X.dsl waited for something to write to it, for ever - on the main loop, as a
Look up window or Settings listed the dictionaries, so that every window froze - and a
device such as /dev/zero reads until memory runs out. So what is opened is asked what it
is, as attachments.read_file asks: opened without waiting, so that a path made a pipe
after it was looked at cannot block either, and then asked.
"""

import os
import stat


class NotAFile(ValueError):
    """A dictionary's file that is not a file of its own: a folder, a device or a pipe.
    A ValueError, as what is wrong with a dictionary's own files is (see
    dictionaries.THE_FILES_FAULT): it is not tried again until they change."""


def is_file(path):
    """Whether `path` is a file of its own, once the links on the way are followed, as
    opening it follows them. Asking does not open it."""
    try:
        return stat.S_ISREG(os.stat(path).st_mode)
    except (OSError, ValueError):
        return False


def open_file(path):
    """`path` opened to read, as open(path, "rb") opens it, if it is a file of its own;
    NotAFile if it is not, and OSError as open() raises it."""
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise NotAFile(f"{os.path.basename(path)} is not a file (a folder, a device "
                           f"or a pipe)")
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise
