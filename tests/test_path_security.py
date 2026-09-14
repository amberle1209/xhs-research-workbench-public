import ctypes
import errno

import pytest

from xhs_workbench.path_security import _acl_has_allow_entry


def test_acl_deny_then_allow_inspects_every_macos_entry() -> None:
    selectors: list[int] = []
    tags = iter([2, 1])

    def get_entry(_acl: object, entry_id: int, _entry: object) -> int:
        selectors.append(entry_id)
        expected = 0 if len(selectors) == 1 else -1
        if entry_id == expected:
            return 0
        ctypes.set_errno(errno.EINVAL)
        return -1

    def get_tag(_entry: object, tag_pointer: object) -> int:
        ctypes.cast(tag_pointer, ctypes.POINTER(ctypes.c_int)).contents.value = next(tags)
        return 0

    assert _acl_has_allow_entry(object(), get_entry, get_tag) is True
    assert selectors == [0, -1]


def test_acl_deny_only_accepts_macos_end_of_list_after_an_entry() -> None:
    selectors: list[int] = []

    def get_entry(_acl: object, entry_id: int, _entry: object) -> int:
        selectors.append(entry_id)
        if len(selectors) == 1:
            return 0
        ctypes.set_errno(errno.EINVAL)
        return -1

    def get_tag(_entry: object, tag_pointer: object) -> int:
        ctypes.cast(tag_pointer, ctypes.POINTER(ctypes.c_int)).contents.value = 2
        return 0

    assert _acl_has_allow_entry(object(), get_entry, get_tag) is False
    assert selectors == [0, -1]


def test_acl_iteration_fails_closed_if_the_first_entry_is_invalid() -> None:
    def get_entry(_acl: object, _entry_id: int, _entry: object) -> int:
        ctypes.set_errno(errno.EINVAL)
        return -1

    def get_tag(_entry: object, _tag_pointer: object) -> int:
        raise AssertionError("tag lookup should not run after an iterator error")

    with pytest.raises(OSError, match="extended ACL inspection failed"):
        _acl_has_allow_entry(object(), get_entry, get_tag)


def test_acl_iteration_still_fails_closed_for_other_errors() -> None:
    def get_entry(_acl: object, _entry_id: int, _entry: object) -> int:
        ctypes.set_errno(errno.EIO)
        return -1

    def get_tag(_entry: object, _tag_pointer: object) -> int:
        raise AssertionError("tag lookup should not run after an iterator error")

    with pytest.raises(OSError, match="extended ACL inspection failed"):
        _acl_has_allow_entry(object(), get_entry, get_tag)
