"""候选清理专用的 Linux fd/mount 与 no-replace 文件系统原语。"""

from __future__ import annotations

import ctypes
import os
import re
from typing import Final

from scripts import container_acceptance_candidate_storage as candidate_storage

_FDINFO_MAX_BYTES: Final = 4096
_RENAME_NOREPLACE: Final = 1


def mount_id(descriptor: int) -> int:
    info_fd: int | None = None
    try:
        info_fd = os.open(
            f"/proc/self/fdinfo/{descriptor}",
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
        encoded = os.read(info_fd, _FDINFO_MAX_BYTES + 1)
    except OSError as exc:
        raise candidate_storage.CandidateStorageError("candidate cleanup mount authority is unavailable") from exc
    finally:
        if info_fd is not None:
            os.close(info_fd)
    if len(encoded) > _FDINFO_MAX_BYTES:
        raise candidate_storage.CandidateStorageError("candidate cleanup mount authority is oversized")
    matches = re.findall(rb"(?m)^mnt_id:\s*([0-9]+)$", encoded)
    if len(matches) != 1:
        raise candidate_storage.CandidateStorageError("candidate cleanup mount authority is invalid")
    return int(matches[0])


def rename_noreplace(
    *,
    source_parent_fd: int,
    source_name: str,
    destination_parent_fd: int,
    destination_name: str,
) -> None:
    """由内核保证目录迁移不覆盖已存在的 destination leaf。"""

    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise candidate_storage.CandidateStorageError("candidate cleanup RENAME_NOREPLACE authority is unavailable")
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        source_parent_fd,
        os.fsencode(source_name),
        destination_parent_fd,
        os.fsencode(destination_name),
        _RENAME_NOREPLACE,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))
