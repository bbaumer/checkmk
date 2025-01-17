#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (C) 2019 tribe29 GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.
"""This module cares about Check_MK's file storage accessing. Most important
functionality is the locked file opening realized with the File() context
manager."""

import ast
import enum
import errno
import fcntl
import functools
import logging
import os
import pprint
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, AnyStr, Dict, Iterator, List, Optional, Tuple, Union

from six import ensure_binary

from cmk.utils.exceptions import MKGeneralException, MKTerminate, MKTimeout
from cmk.utils.i18n import _
from cmk.utils.paths import default_config_dir

logger = logging.getLogger("cmk.store")

# TODO: Make all methods handle paths the same way. e.g. mkdir() and makedirs()
# care about encoding a path to UTF-8. The others don't to that.

#   .--Predefined----------------------------------------------------------.
#   |          ____               _       __ _                _            |
#   |         |  _ \ _ __ ___  __| | ___ / _(_)_ __   ___  __| |           |
#   |         | |_) | '__/ _ \/ _` |/ _ \ |_| | '_ \ / _ \/ _` |           |
#   |         |  __/| | |  __/ (_| |  __/  _| | | | |  __/ (_| |           |
#   |         |_|   |_|  \___|\__,_|\___|_| |_|_| |_|\___|\__,_|           |
#   |                                                                      |
#   +----------------------------------------------------------------------+
#   | Predefined locks                                                     |
#   '----------------------------------------------------------------------'


class MKConfigLockTimeout(MKTimeout):
    """Special exception to signalize timeout waiting for the global configuration lock"""


def configuration_lockfile() -> str:
    return default_config_dir + "/multisite.mk"


@contextmanager
def lock_checkmk_configuration() -> Iterator[None]:
    path = configuration_lockfile()
    try:
        aquire_lock(path)
    except MKTimeout as e:
        raise MKConfigLockTimeout(
            _("Couldn't lock the Checkmk configuration. Another "
              "process is running that holds this lock. In order for you to be "
              "able to perform the desired action, you have to wait until the "
              "other process has finished. Please try again later.")) from e

    try:
        yield
    finally:
        release_lock(path)


# TODO: Use lock_checkmk_configuration() and nuke this!
def lock_exclusive() -> None:
    aquire_lock(configuration_lockfile())


#.
#   .--Directories---------------------------------------------------------.
#   |           ____  _               _             _                      |
#   |          |  _ \(_)_ __ ___  ___| |_ ___  _ __(_) ___  ___            |
#   |          | | | | | '__/ _ \/ __| __/ _ \| '__| |/ _ \/ __|           |
#   |          | |_| | | | |  __/ (__| || (_) | |  | |  __/\__ \           |
#   |          |____/|_|_|  \___|\___|\__\___/|_|  |_|\___||___/           |
#   |                                                                      |
#   +----------------------------------------------------------------------+
#   | Some small wrappers around the python standard directory handling    |
#   | functions.                                                           |
#   '----------------------------------------------------------------------'


def mkdir(path: Union[Path, str], mode: int = 0o770) -> None:
    if not isinstance(path, Path):
        path = Path(path)
    path.mkdir(mode=mode, exist_ok=True)


def makedirs(path: Union[Path, str], mode: int = 0o770) -> None:
    if not isinstance(path, Path):
        path = Path(path)
    path.mkdir(mode=mode, exist_ok=True, parents=True)


#.
#   .--.mk Configs---------------------------------------------------------.
#   |                     _       ____             __ _                    |
#   |           _ __ ___ | | __  / ___|___  _ __  / _(_) __ _ ___          |
#   |          | '_ ` _ \| |/ / | |   / _ \| '_ \| |_| |/ _` / __|         |
#   |         _| | | | | |   <  | |__| (_) | | | |  _| | (_| \__ \         |
#   |        (_)_| |_| |_|_|\_\  \____\___/|_| |_|_| |_|\__, |___/         |
#   |                                                   |___/              |
#   +----------------------------------------------------------------------+
#   | Loading and saving of .mk configuration files                        |
#   '----------------------------------------------------------------------'

# TODO: These functions could handle paths unicode > str conversion. This would make
#       the using code again shorter in some cases. It would not have to care about
#       encoding anymore.


# This function generalizes reading from a .mk configuration file. It is basically meant to
# generalize the exception handling for all file IO. This function handles all those files
# that are read with exec().
def load_mk_file(path: Union[Path, str], default: Any = None, lock: bool = False) -> Any:
    if not isinstance(path, Path):
        path = Path(path)

    if default is None:
        raise MKGeneralException(
            _("You need to provide a config dictionary to merge with the "
              "read configuration. The dictionary should have all expected "
              "keys and their default values set."))

    if lock:
        aquire_lock(path)

    try:
        try:
            with path.open(mode="rb") as f:
                exec(f.read(), globals(), default)
        except IOError as e:
            if e.errno != errno.ENOENT:  # No such file or directory
                raise
        return default

    except (MKTerminate, MKTimeout):
        raise
    except Exception as e:
        # TODO: How to handle debug mode or logging?
        raise MKGeneralException(_("Cannot read configuration file \"%s\": %s") % (path, e))


# A simple wrapper for cases where you only have to read a single value from a .mk file.
def load_from_mk_file(path: Union[Path, str], key: str, default: Any, lock: bool = False) -> Any:
    return load_mk_file(path, {key: default}, lock=False)[key]


def save_mk_file(path: Union[Path, str], mk_content: str, add_header: bool = True) -> None:
    content = ""

    if add_header:
        content += "# Written by Checkmk store\n\n"

    content += mk_content
    content += "\n"
    save_file(path, content)


# A simple wrapper for cases where you only have to write a single value to a .mk file.
def save_to_mk_file(path: Union[Path, str],
                    key: str,
                    value: Any,
                    pprint_value: bool = False) -> None:
    format_func = repr
    if pprint_value:
        format_func = pprint.pformat

    # mypy complains: "[mypy:] Cannot call function of unknown type"
    if isinstance(value, dict):
        formated = "%s.update(%s)" % (key, format_func(value))
    else:
        formated = "%s += %s" % (key, format_func(value))

    save_mk_file(path, formated)


#.
#   .--load/save-----------------------------------------------------------.
#   |             _                 _    __                                |
#   |            | | ___   __ _  __| |  / /__  __ ___   _____              |
#   |            | |/ _ \ / _` |/ _` | / / __|/ _` \ \ / / _ \             |
#   |            | | (_) | (_| | (_| |/ /\__ \ (_| |\ V /  __/             |
#   |            |_|\___/ \__,_|\__,_/_/ |___/\__,_| \_/ \___|             |
#   |                                                                      |
#   '----------------------------------------------------------------------'


# Handle .mk files that are only holding a python data structure and often
# directly read via file/open and then parsed using eval.
# TODO: Consolidate with load_mk_file?
def load_object_from_file(path: Union[Path, str], default: Any = None, lock: bool = False) -> Any:
    content = _load_bytes_from_file(path, lock=lock).decode("utf-8")
    return ast.literal_eval(content) if content else default


def load_text_from_file(path: Union[Path, str], default: str = "", lock: bool = False) -> str:
    return _load_bytes_from_file(path, lock=lock).decode("utf-8") or default


def load_bytes_from_file(path: Union[Path, str], default: bytes = b"", lock: bool = False) -> bytes:
    return _load_bytes_from_file(path, lock=lock) or default


def _load_bytes_from_file(
    path: Union[Path, str],
    lock: bool = False,
) -> bytes:
    if not isinstance(path, Path):
        path = Path(path)

    if lock:
        aquire_lock(path)

    try:
        try:
            return path.read_bytes()
        except FileNotFoundError:
            return b''

    except (MKTerminate, MKTimeout):
        if lock:
            release_lock(path)
        raise

    except Exception as e:
        if lock:
            release_lock(path)

        # TODO: How to handle debug mode or logging?
        raise MKGeneralException(_("Cannot read file \"%s\": %s") % (path, e))


# A simple wrapper for cases where you want to store a python data
# structure that is then read by load_data_from_file() again
def save_object_to_file(path: Union[Path, str], data: Any, pretty: bool = False) -> None:
    if pretty:
        try:
            formatted_data = pprint.pformat(data)
        except UnicodeDecodeError:
            # When writing a dict with unicode keys and normal strings with garbled
            # umlaut encoding pprint.pformat() fails with UnicodeDecodeError().
            # example:
            #   pprint.pformat({'Z\xc3\xa4ug': 'on',  'Z\xe4ug': 'on', u'Z\xc3\xa4ugx': 'on'})
            # Catch the exception and use repr() instead
            formatted_data = repr(data)
    else:
        formatted_data = repr(data)
    save_file(path, "%s\n" % formatted_data)


def save_text_to_file(path: Union[Path, str], content: str, mode: int = 0o660) -> None:
    if not isinstance(content, str):
        raise TypeError("content argument must be Text, not bytes")
    _save_data_to_file(path, content.encode("utf-8"), mode)


def save_bytes_to_file(path: Union[Path, str], content: bytes, mode: int = 0o660) -> None:
    if not isinstance(content, bytes):
        raise TypeError("content argument must be bytes, not Text")
    _save_data_to_file(path, content, mode)


def save_file(path: Union[Path, str], content: AnyStr, mode: int = 0o660) -> None:
    # Just to be sure: ensure_binary
    _save_data_to_file(path, ensure_binary(content), mode=mode)


# Saving assumes a locked destination file (usually done by loading code)
# Then the new file is written to a temporary file and moved to the target path
def _save_data_to_file(path: Union[Path, str], content: bytes, mode: int = 0o660) -> None:
    if not isinstance(path, Path):
        path = Path(path)

    tmp_path = None
    try:
        # Normally the file is already locked (when data has been loaded before with lock=True),
        # but lock it just to be sure we have the lock on the file.
        #
        # Please note that this already creates the file with 0 bytes (in case it is missing).
        aquire_lock(path)

        with tempfile.NamedTemporaryFile("wb",
                                         dir=str(path.parent),
                                         prefix=".%s.new" % path.name,
                                         delete=False) as tmp:

            tmp_path = tmp.name
            os.chmod(tmp_path, mode)
            tmp.write(content)

            # The goal of the fsync would be to ensure that there is a consistent file after a
            # crash. Without the fsync it may happen that the file renamed below is just an empty
            # file. That may lead into unexpected situations during loading.
            #
            # Don't do a fsync here because this may run into IO performance issues. Even when
            # we can specify the fsync on a fd, the disk cache may be flushed completely because
            # the disk does not know anything about fds, only about blocks.
            #
            # For Checkmk 1.4 we can not introduce a good solution for this, because the changes
            # would affect too many parts of Checkmk with possible new issues. For the moment we
            # stick with the IO behaviour of previous Checkmk versions.
            #
            # In the future we'll find a solution to deal better with OS crash recovery situations.
            # for example like this:
            #
            # TODO(lm): The consistency of the file will can be ensured using copies of the
            # original file which are made before replacing it with the new one. After first
            # successful loading of the just written fille the possibly existing copies of this
            # file are deleted.
            # We can archieve this by calling os.link() before the os.rename() below. Then we need
            # to define in which situations we want to check out the backup open(s) and in which
            # cases we can savely delete them.
            #tmp.flush()
            #os.fsync(tmp.fileno())

        os.rename(tmp_path, str(path))

    except (MKTerminate, MKTimeout):
        raise
    except Exception as e:
        # In case an exception happens during saving cleanup the tempfile created for writing
        try:
            if tmp_path:
                os.unlink(tmp_path)
        except IOError as e2:
            if e2.errno != errno.ENOENT:  # No such file or directory
                raise

        # TODO: How to handle debug mode or logging?
        raise MKGeneralException(_("Cannot write configuration file \"%s\": %s") % (path, e))

    finally:
        release_lock(path)


#.
#   .--File locking--------------------------------------------------------.
#   |          _____ _ _        _            _    _                        |
#   |         |  ___(_) | ___  | | ___   ___| | _(_)_ __   __ _            |
#   |         | |_  | | |/ _ \ | |/ _ \ / __| |/ / | '_ \ / _` |           |
#   |         |  _| | | |  __/ | | (_) | (__|   <| | | | | (_| |           |
#   |         |_|   |_|_|\___| |_|\___/ \___|_|\_\_|_| |_|\__, |           |
#   |                                                     |___/            |
#   +----------------------------------------------------------------------+
#   | Helper functions to lock files (between processes) for disk IO       |
#   | Currently only exclusive locks are implemented and they always will  |
#   | wait forever.                                                        |
#   '----------------------------------------------------------------------'

LockDict = Dict[str, int]

# This will hold our path to file descriptor dicts.
_locks = threading.local()


def with_lock_dict(func):
    """Decorator to make access to global locking dict thread-safe.

    Only the thread which acquired the lock should see the file descriptor in the locking
    dictionary. In order to do this, the locking dictionary(*) is now an attribute on a
    threading.local() object, which has to be created at runtime. This decorator handles
    the creation of these dicts.

    (*) The dict is a mapping from path-name to file descriptor.

    Additionally, this decorator passes the locking dictionary as the first parameter to the
    functions, which manipulate the locking dictionary.
    """
    @functools.wraps(func)
    def wrapper(*args):
        if not hasattr(_locks, 'acquired_locks'):
            _locks.acquired_locks = {}
        return func(*args, locks=_locks.acquired_locks)

    return wrapper


@with_lock_dict
def _set_lock(
    name: str,
    fd: int,
    locks: LockDict,
) -> None:
    locks[name] = fd


@with_lock_dict
def _get_lock(
    name: str,
    locks: LockDict,
) -> Optional[int]:
    return locks.get(name)


@with_lock_dict
def _del_lock(
    name: str,
    locks: LockDict,
) -> None:
    locks.pop(name, None)


@with_lock_dict
def _del_all_locks(locks: LockDict) -> None:
    locks.clear()


@with_lock_dict
def _get_lock_keys(locks: LockDict) -> List[str]:
    return list(locks.keys())


@with_lock_dict
def _get_lock_map(locks: LockDict) -> Dict[str, int]:
    return locks


@with_lock_dict
def _has_lock(
    name: str,
    locks: LockDict,
) -> bool:
    return name in locks


@contextmanager
def locked(path: Union[Path, str], blocking: bool = True) -> Iterator[None]:
    try:
        aquire_lock(path, blocking)
        yield
    finally:
        release_lock(path)


def aquire_lock(path: Union[Path, str], blocking: bool = True) -> None:
    if not isinstance(path, Path):
        path = Path(path)

    if have_lock(path):
        return  # No recursive locking

    logger.debug("Trying to acquire lock on %s", path)

    # Create file (and base dir) for locking if not existent yet
    makedirs(path.parent, mode=0o770)

    fd = os.open(str(path), os.O_RDONLY | os.O_CREAT, 0o660)

    # Handle the case where the file has been renamed in the meantime
    while True:
        flags = fcntl.LOCK_EX
        if not blocking:
            flags |= fcntl.LOCK_NB

        try:
            fcntl.flock(fd, flags)
        except IOError:
            os.close(fd)
            raise

        fd_new = os.open(str(path), os.O_RDONLY | os.O_CREAT, 0o660)
        if os.path.sameopenfile(fd, fd_new):
            os.close(fd_new)
            break
        os.close(fd)
        fd = fd_new

    _set_lock(str(path), fd)
    logger.debug("Got lock on %s", path)


@contextmanager
def try_locked(path: Union[Path, str]) -> Iterator[bool]:
    try:
        yield try_aquire_lock(path)
    finally:
        release_lock(path)


def try_aquire_lock(path: Union[Path, str]) -> bool:
    try:
        aquire_lock(path, blocking=False)
        return True
    except IOError as e:
        if e.errno != errno.EAGAIN:  # Try again
            raise
        return False


def release_lock(path: Union[Path, str]) -> None:
    if not isinstance(path, Path):
        path = Path(path)

    if not have_lock(path):
        return  # no unlocking needed
    logger.debug("Releasing lock on %s", path)

    fd = _get_lock(str(path))
    if fd is None:
        return
    try:
        os.close(fd)
    except OSError as e:
        if e.errno != errno.EBADF:  # Bad file number
            raise
    _del_lock(str(path))
    logger.debug("Released lock on %s", path)


def have_lock(path: Union[str, Path]) -> bool:
    return _has_lock(str(path))


def release_all_locks() -> None:
    logger.debug("Releasing all locks")
    logger.debug("Acquired locks: %r", _get_lock_map())
    for path in _get_lock_keys():
        release_lock(path)
    _del_all_locks()


@contextmanager
def cleanup_locks() -> Iterator[None]:
    """Context-manager to release all memorized locks at the end of the block.

    This is a hack which should be removed. In order to make this happen, every lock shall
    itself only be used as a context-manager.
    """
    try:
        yield
    finally:
        try:
            release_all_locks()
        except Exception:
            logger.exception("Error while releasing locks after block.")
            raise


class RawStorageLoader:
    """This is POC class: minimal working functionality. OOP and more clear API is planned"""
    __slots__ = ['_data', '_loaded']

    def __init__(self) -> None:
        self._data: str = ""
        self._loaded: Dict[str, Any] = {}

    def read(self, filename: Path) -> None:
        with filename.open() as f:
            self._data = f.read()

    def parse(self) -> None:
        to_run = "loaded.update(" + self._data + ")"

        exec(to_run, {'__builtins__': None}, {"loaded": self._loaded})

    def apply(self, variables: Dict[str, Any]) -> bool:
        """Stub"""
        isinstance(variables, dict)
        return True

    def _all_hosts(self) -> List[str]:
        return self._loaded.get("all_hosts", [])

    def _host_tags(self) -> Dict[str, Any]:
        return self._loaded.get("host_tags", {})

    def _host_labels(self) -> Dict[str, Any]:
        return self._loaded.get("host_labels", {})

    def _attributes(self) -> Dict[str, Dict[str, Any]]:
        return self._loaded.get("attributes", {})

    def _host_attributes(self) -> Dict[str, Any]:
        return self._loaded.get("host_attributes", {})

    def _explicit_host_conf(self) -> Dict[str, Dict[str, Any]]:
        return self._loaded.get("explicit_host_conf", {})

    def _extra_host_conf(self) -> Dict[str, List[Tuple[str, List[str]]]]:
        return self._loaded.get("extra_host_conf", {})


class StorageFormat(enum.Enum):
    STANDARD = "standard"
    RAW = "raw"

    def __str__(self) -> str:
        return str(self.value)

    @classmethod
    def from_str(cls, value: str) -> 'StorageFormat':
        return cls[value.upper()]

    def extension(self) -> str:
        # This typing error is a false positive.  There are tests to demonstrate that.
        return {  # type: ignore[return-value]
            StorageFormat.STANDARD: ".mk",
            StorageFormat.RAW: ".cfg",
        }[self]

    def hosts_file(self) -> str:
        return "hosts" + self.extension()

    def is_hosts_config(self, filename: str) -> bool:
        """Unified method to determine that the file is hosts config."""
        return filename.startswith("/wato/") and filename.endswith("/" + self.hosts_file())
