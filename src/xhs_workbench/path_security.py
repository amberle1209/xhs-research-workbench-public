"""Small, fail-closed filesystem trust checks for macOS-owned artifacts."""

from __future__ import annotations

import ctypes
import errno
import os
import platform
from collections.abc import Callable
from pathlib import Path
from typing import cast

_ACL_TYPE_EXTENDED = 0x00000100
_ACL_FIRST_ENTRY = 0
_ACL_NEXT_ENTRY = -1
_MAX_ACL_ENTRIES = 169
_ACL_EXTENDED_ALLOW = 1
_ACL_EXTENDED_DENY = 2
_AclGetter = Callable[[object, int], int | None]
_AclEntryGetter = Callable[[object, int, object], int]
_AclTagGetter = Callable[[object, object], int]
_AclFree = Callable[[object], int]


def has_extended_acl(path: Path) -> bool:
    """Return whether a macOS path has any extended ACL entries.

    The installer is Darwin-only.  Returning ``False`` on another platform
    preserves import-time portability for tests while callers keep their
    Darwin gate.  On macOS, an unreadable or malformed ACL is an error rather
    than an implicit approval.
    """
    if platform.system() != "Darwin":
        return False
    get_acl, get_entry, free_acl = _acl_functions("acl_get_file")

    encoded_path = os.fsencode(Path(path))
    acl = get_acl(encoded_path, _ACL_TYPE_EXTENDED)
    if not acl:
        # Darwin signals an otherwise valid path with no extended ACL as
        # ENOENT on APFS.  Re-stat the path before accepting that special
        # case, so an actual missing/rebound path still fails closed.
        error_number = ctypes.get_errno()
        if error_number in {errno.ENOENT, errno.ENODATA}:
            try:
                os.lstat(path)
            except OSError as error:
                raise OSError("extended ACL inspection failed") from error
            return False
        raise OSError("extended ACL inspection failed")
    try:
        entry = ctypes.c_void_p()
        for entry_index in range(_MAX_ACL_ENTRIES + 1):
            result = get_entry(acl, _ACL_FIRST_ENTRY if entry_index == 0 else 1, ctypes.byref(entry))
            if result == 0:
                return True
            if result == 1:
                return False
            raise OSError("extended ACL inspection failed")
        raise OSError("extended ACL entry count is invalid")
    finally:
        if free_acl(acl) != 0:
            raise OSError("extended ACL release failed")


def has_extended_acl_fd(descriptor: int) -> bool:
    """Return whether a held macOS descriptor has an extended ACL entry."""
    if platform.system() != "Darwin":
        return False
    get_acl, get_entry, free_acl = _acl_functions("acl_get_fd_np")
    acl = get_acl(descriptor, _ACL_TYPE_EXTENDED)
    if not acl:
        error_number = ctypes.get_errno()
        if error_number in {errno.ENOENT, errno.ENODATA}:
            return False
        raise OSError("extended ACL inspection failed")
    try:
        return _acl_has_entry(acl, get_entry)
    finally:
        if free_acl(acl) != 0:
            raise OSError("extended ACL release failed")


def has_acl_allow_entry(path: Path) -> bool:
    """Return whether a macOS path ACL contains an access-granting entry.

    Restrictive deny-only ACLs do not delegate access. This distinction is
    needed only for the macOS HOME directory, which normally carries an
    inherited ``everyone deny delete`` entry. Other trusted artifacts continue
    to reject every extended ACL through :func:`has_extended_acl`.
    """
    if platform.system() != "Darwin":
        return False
    get_acl, get_entry, free_acl = _acl_functions("acl_get_file")
    acl = get_acl(os.fsencode(Path(path)), _ACL_TYPE_EXTENDED)
    if not acl:
        error_number = ctypes.get_errno()
        if error_number in {errno.ENOENT, errno.ENODATA}:
            try:
                os.lstat(path)
            except OSError as error:
                raise OSError("extended ACL inspection failed") from error
            return False
        raise OSError("extended ACL inspection failed")
    try:
        return _acl_has_allow_entry(acl, get_entry, _acl_tag_function())
    finally:
        if free_acl(acl) != 0:
            raise OSError("extended ACL release failed")


def has_acl_allow_entry_fd(descriptor: int) -> bool:
    """Return whether a held macOS descriptor ACL grants access."""
    if platform.system() != "Darwin":
        return False
    get_acl, get_entry, free_acl = _acl_functions("acl_get_fd_np")
    acl = get_acl(descriptor, _ACL_TYPE_EXTENDED)
    if not acl:
        error_number = ctypes.get_errno()
        if error_number in {errno.ENOENT, errno.ENODATA}:
            return False
        raise OSError("extended ACL inspection failed")
    try:
        return _acl_has_allow_entry(acl, get_entry, _acl_tag_function())
    finally:
        if free_acl(acl) != 0:
            raise OSError("extended ACL release failed")


def _acl_functions(name: str) -> tuple[_AclGetter, _AclEntryGetter, _AclFree]:
    try:
        library = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        get_acl = getattr(library, name)
        get_acl.argtypes = [ctypes.c_char_p, ctypes.c_int] if name == "acl_get_file" else [ctypes.c_int, ctypes.c_int]
        get_acl.restype = ctypes.c_void_p
        get_entry = library.acl_get_entry
        get_entry.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)]
        get_entry.restype = ctypes.c_int
        free_acl = library.acl_free
        free_acl.argtypes = [ctypes.c_void_p]
        free_acl.restype = ctypes.c_int
        return cast(_AclGetter, get_acl), cast(_AclEntryGetter, get_entry), cast(_AclFree, free_acl)
    except (AttributeError, OSError) as error:
        raise OSError("extended ACL inspection is unavailable") from error


def _acl_tag_function() -> _AclTagGetter:
    try:
        library = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        get_tag = library.acl_get_tag_type
        get_tag.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
        get_tag.restype = ctypes.c_int
        return cast(_AclTagGetter, get_tag)
    except (AttributeError, OSError) as error:
        raise OSError("extended ACL inspection is unavailable") from error


def _acl_has_entry(acl: object, get_entry: _AclEntryGetter) -> bool:
    entry = ctypes.c_void_p()
    for entry_index in range(_MAX_ACL_ENTRIES + 1):
        result = get_entry(acl, _ACL_FIRST_ENTRY if entry_index == 0 else 1, ctypes.byref(entry))
        if result == 0:
            return True
        if result == 1:
            return False
        raise OSError("extended ACL inspection failed")
    raise OSError("extended ACL entry count is invalid")


def _acl_has_allow_entry(
    acl: object, get_entry: _AclEntryGetter, get_tag: _AclTagGetter
) -> bool:
    entry = ctypes.c_void_p()
    for entry_index in range(_MAX_ACL_ENTRIES + 1):
        ctypes.set_errno(0)
        result = get_entry(
            acl,
            _ACL_FIRST_ENTRY if entry_index == 0 else _ACL_NEXT_ENTRY,
            ctypes.byref(entry),
        )
        if result == -1 and ctypes.get_errno() == errno.EINVAL and entry_index > 0:
            return False
        if result != 0:
            raise OSError("extended ACL inspection failed")
        tag = ctypes.c_int()
        if get_tag(entry, ctypes.byref(tag)) != 0:
            raise OSError("extended ACL inspection failed")
        if tag.value == _ACL_EXTENDED_ALLOW:
            return True
        if tag.value != _ACL_EXTENDED_DENY:
            raise OSError("extended ACL tag is invalid")
    raise OSError("extended ACL entry count is invalid")
