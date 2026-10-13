"""Filesystem primitives with safety guarantees.

* :func:`atomic_write` never leaves a half-written file behind;
* :func:`safe_rmtree` refuses to delete ``/``, ``$HOME``, protected paths,
  symlinked roots and (optionally) directories without our marker;
* :func:`safe_extract` validates every tar member before anything is written.
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import lzma
import os
import pwd
import shutil
import stat
import tarfile
import tempfile
import zlib
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ForkLinuxError, IntegrityFailed, UsageError

CLONE_METHODS = ("auto", "hardlink", "copy")

# Errors that mean "this archive is unreadable or corrupt" (OSError covers BadGzipFile).
_READ_ERRORS = (tarfile.TarError, OSError, EOFError, lzma.LZMAError, zlib.error)
# Errors that mean "the archive data is bad" while extracting; plain OSErrors
# (disk full, permission denied) propagate unchanged.
_DATA_ERRORS = (tarfile.TarError, EOFError, lzma.LZMAError, zlib.error, KeyError)
# tarfile's own extraction filter refusing a member (Python >= 3.10.12 / 3.12).
_FILTER_ERRORS: tuple[type[Exception], ...] = tuple(
    getattr(tarfile, name) for name in ("FilterError",) if hasattr(tarfile, name)
)


def atomic_write(path: Path, data: bytes | str, *, mode: int = 0o600) -> None:
    """Write ``data`` to ``path`` atomically: temp file in the same directory, fsync, rename.

    ``str`` data is encoded as UTF-8. Missing parent directories are created
    (0700). An existing symlink at ``path`` is replaced, never followed.
    """
    path = Path(path)
    payload = data.encode("utf-8") if isinstance(data, str) else bytes(data)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # The name is cut so a long target name cannot push the temp name past NAME_MAX.
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name[:64]}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), mode)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)
        raise
    _fsync_dir(path.parent)


def _fsync_dir(directory: Path) -> None:
    """Persist a rename; filesystems that cannot fsync a directory are ignored."""
    with contextlib.suppress(OSError):
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def ensure_dir(path: Path, mode: int = 0o700) -> Path:
    """Create ``path`` (and parents) if needed and make sure it has exactly ``mode``."""
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True, mode=mode)
    if stat.S_IMODE(path.stat().st_mode) != mode:
        os.chmod(path, mode)
    return path


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    """Lowercase hex SHA-256 of a file, read in ``chunk``-byte blocks."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def safe_rmtree(path: Path, *, marker: Path | None = None, forbidden: Iterable[Path] = ()) -> None:
    """Delete ``path`` recursively, refusing anything dangerous (raises :class:`IntegrityFailed`).

    Refused: a symlinked ``path``; a ``path`` that resolves to ``/``, ``$HOME``,
    a ``forbidden`` path or an ancestor of any of them; anything at or below
    ``~/.wine`` (fork-linux never touches it); and, when ``marker`` is given, a
    ``path`` whose marker file is missing. Symlinks inside the tree are
    removed, never followed. A missing ``path`` is not an error.
    """
    path = Path(path)
    if path.is_symlink():
        raise IntegrityFailed(f"refusing to delete {path}: it is a symbolic link")
    target = path.resolve()
    for guarded in _protected_paths(forbidden):
        if target == guarded or guarded.is_relative_to(target):
            raise IntegrityFailed(f"refusing to delete {path}: it is or contains {guarded}")
    for wine in _refused_wine_dirs():
        if target.is_relative_to(wine):
            raise IntegrityFailed(
                f"refusing to delete {path}: it is inside {wine}",
                hint="fork-linux never modifies ~/.wine",
            )
    if not os.path.lexists(target):
        return
    if marker is not None and (not os.path.lexists(marker) or Path(marker).is_symlink()):
        raise IntegrityFailed(
            f"refusing to delete {path}: marker {marker} is missing",
            hint="fork-linux only deletes directories it created itself",
        )
    if not target.is_dir():
        target.unlink()
        return
    try:
        shutil.rmtree(target)
    except PermissionError:
        _grant_owner_access(target)
        shutil.rmtree(target)


def _homes() -> list[Path]:
    """``$HOME`` and the password-database home directory (unresolved)."""
    homes: list[Path] = []
    home = os.environ.get("HOME", "")
    if home:
        homes.append(Path(home))
    with contextlib.suppress(KeyError):
        homes.append(Path(pwd.getpwuid(os.getuid()).pw_dir))
    return homes


def _protected_paths(extra: Iterable[Path]) -> list[Path]:
    """``/``, ``$HOME``, the password-database home and ``extra``, all resolved."""
    protected = [Path("/"), *_homes(), *(Path(item) for item in extra)]
    return [item.resolve() for item in protected]


def _refused_wine_dirs() -> list[Path]:
    """The user's own ``~/.wine`` (resolved) for every known home: never deleted from."""
    return [(home / ".wine").resolve() for home in _homes()]


def _grant_owner_access(root: Path) -> None:
    """Give the owner rwx on every real directory under ``root`` so it can be emptied."""
    _add_owner_rwx(str(root))
    for current, dirs, _files in os.walk(root):
        for name in dirs:
            child = os.path.join(current, name)
            if not os.path.islink(child):
                _add_owner_rwx(child)


def _add_owner_rwx(directory: str) -> None:
    """chmod u+rwx on one directory if it lacks any of those bits."""
    mode = os.lstat(directory).st_mode
    if mode & stat.S_IRWXU != stat.S_IRWXU:
        os.chmod(directory, stat.S_IMODE(mode) | stat.S_IRWXU)


def safe_extract(archive: Path, dest: Path, *, strip_components: int = 0) -> list[Path]:
    """Extract a tar archive (plain, gz, xz or bz2) into ``dest``; return the extracted paths.

    Every member is validated before anything is written: absolute names,
    ``..`` components, NUL bytes, device/FIFO members, members whose type
    clashes with an earlier one (a file over a directory, anything below a
    file), symlinks or hard links that point outside ``dest`` and members
    reached through an archive symlink all raise :class:`IntegrityFailed`.
    setuid/setgid/sticky and group/other write bits are stripped and
    ownership is never restored. The first
    ``strip_components`` path components are removed (like GNU tar);
    shallower members are skipped. Extract into a fresh staging directory:
    after a failure part of the archive may already be on disk.
    """
    if strip_components < 0:
        raise UsageError("strip_components must not be negative")
    archive = Path(archive)
    dest = Path(dest)
    try:
        tar = tarfile.open(archive, "r:*")
    except _READ_ERRORS as exc:
        raise _corrupt(archive, exc) from exc
    with tar:
        try:
            members = tar.getmembers()
        except _READ_ERRORS as exc:
            raise _corrupt(archive, exc) from exc
        plan = _plan_members(members, strip_components)
        dest.mkdir(parents=True, exist_ok=True)
        root = Path(os.path.realpath(dest))
        options = _extract_options()
        for member in plan:
            _check_on_disk(member, root)
            if member.isreg() or member.islnk():
                _unlink_existing(root / member.name)
            _extract_member(tar, member, root, options, archive)
    _verify_symlinks(plan, root)
    return [dest / member.name for member in plan]


def _extract_member(
    tar: tarfile.TarFile, member: tarfile.TarInfo, root: Path, options: dict[str, str], archive: Path
) -> None:
    """Extract one validated member, mapping tarfile failures to :class:`IntegrityFailed`."""
    try:
        tar.extract(member, root, **options)
    except _FILTER_ERRORS as exc:
        raise _unsafe(member, f"rejected by the tarfile filter: {exc}") from exc
    except _DATA_ERRORS as exc:
        raise _corrupt(archive, exc) from exc


def _extract_options() -> dict[str, str]:
    """tarfile's own extraction filter as a second line of defence, when it has one.

    Early backports of the 'data' filter (e.g. Python 3.10.12) resolve symlink
    targets against the destination root instead of the link's directory and
    so reject legitimate ``dir/link -> ../file`` members; there the 'tar'
    filter (name and mode checks only) is used, our own checks still apply.
    """
    if not hasattr(tarfile, "data_filter"):
        return {}
    return {"filter": "data" if _data_filter_handles_relative_links() else "tar"}


def _data_filter_handles_relative_links() -> bool:
    """Probe whether ``tarfile.data_filter`` accepts ``a/b -> ../c`` (inside the destination)."""
    probe = tarfile.TarInfo("a/b")
    probe.type = tarfile.SYMTYPE
    probe.linkname = "../c"
    try:
        tarfile.data_filter(probe, "/fork-linux-filter-probe/dest")
    except tarfile.TarError:
        return False
    return True


def _corrupt(archive: Path, exc: BaseException) -> IntegrityFailed:
    """The error raised for an unreadable or corrupt archive."""
    return IntegrityFailed(
        f"cannot read archive {archive}: {exc}",
        hint="the download may be truncated or corrupt; delete it and try again",
    )


def _unsafe(member: tarfile.TarInfo, reason: str) -> IntegrityFailed:
    """The error raised for a member that must not be extracted."""
    return IntegrityFailed(
        f"unsafe archive member {member.name!r}: {reason}",
        hint="the archive may have been tampered with; it was not extracted",
    )


@dataclass
class _Plan:
    """What the members validated so far created, by stripped name."""

    symlinks: set[str] = field(default_factory=set)
    files: set[str] = field(default_factory=set)
    leaves: set[str] = field(default_factory=set)
    directories: set[str] = field(default_factory=set)


def _check_kind(member: tarfile.TarInfo) -> None:
    """Only regular files, directories and links, with no NUL byte in their names."""
    if not (member.isreg() or member.isdir() or member.issym() or member.islnk()):
        raise _unsafe(member, "devices, FIFOs and other special files are not allowed")
    if "\x00" in member.name or "\x00" in member.linkname:
        raise _unsafe(member, "NUL byte in its name or link target")


def _check_placement(member: tarfile.TarInfo, name: str, parts: list[str], plan: _Plan) -> None:
    """Refuse a member that would go through a symlink or change the type of an earlier one."""
    if _crosses_symlink(name, plan.symlinks, include_self=not member.issym()):
        raise _unsafe(member, "it would be written through a symbolic link")
    if not member.isdir() and name in plan.directories:
        raise _unsafe(member, "it would replace a directory")
    if member.isdir() and name in plan.leaves:
        raise _unsafe(member, "it would replace a file with a directory")
    if any("/".join(parts[:index]) in plan.leaves for index in range(1, len(parts))):
        raise _unsafe(member, "its parent directory is a file in the archive")


def _record(member: tarfile.TarInfo, clean: tarfile.TarInfo, name: str, strip: int, plan: _Plan) -> None:
    """Note what ``member`` (extracted as ``name``) creates; set the link target of ``clean``."""
    plan.files.discard(name)
    plan.leaves.discard(name)
    if member.issym():
        clean.linkname = _check_symlink_target(member, name)
        plan.symlinks.add(name)
    elif member.islnk():
        link = "/".join(_relative_parts(member.linkname, member, "link target")[strip:])
        if link not in plan.files:
            raise _unsafe(member, "a hard link must point to a regular file extracted before it")
        clean.linkname = link
        plan.leaves.add(name)
    elif member.isreg():
        plan.files.add(name)
        plan.leaves.add(name)


def _plan_members(members: list[tarfile.TarInfo], strip: int) -> list[tarfile.TarInfo]:
    """Validate all members and return sanitised copies with stripped names.

    Besides the path checks, member types must stay consistent: nothing may
    replace a directory, become a directory over a file, or live below a file.
    """
    planned: list[tarfile.TarInfo] = []
    plan = _Plan()
    for member in members:
        _check_kind(member)
        parts = _relative_parts(member.name, member, "name")[strip:]
        name = "/".join(parts)
        if not name:
            continue
        _check_placement(member, name, parts, plan)
        plan.directories.update("/".join(parts[:index]) for index in range(1, len(parts)))
        if member.isdir():
            plan.directories.add(name)
        clean = copy.copy(member)
        clean.name = name
        clean.mode = _safe_mode(member)
        clean.uid, clean.gid, clean.uname, clean.gname = os.getuid(), os.getgid(), "", ""
        _record(member, clean, name, strip, plan)
        planned.append(clean)
    return planned


def _relative_parts(raw: str, member: tarfile.TarInfo, what: str) -> list[str]:
    """Split an archive path into components, rejecting absolute paths and ``..``."""
    if raw.startswith("/"):
        raise _unsafe(member, f"absolute {what} {raw!r}")
    parts = [part for part in raw.split("/") if part not in ("", ".")]
    if ".." in parts:
        raise _unsafe(member, f"'..' in {what} {raw!r}")
    return parts


def _crosses_symlink(name: str, symlinks: set[str], *, include_self: bool) -> bool:
    """True if ``name`` (or, with ``include_self``, the path itself) is an archive symlink."""
    parts = name.split("/")
    last = len(parts) if include_self else len(parts) - 1
    return any("/".join(parts[:index]) in symlinks for index in range(1, last + 1))


def _check_symlink_target(member: tarfile.TarInfo, name: str) -> str:
    """Reject empty, absolute or escaping symlink targets; return the normalised target.

    Normalising (as CPython's own 'data' filter does) collapses inner ``..``
    so a target cannot climb out through other archive symlinks.
    """
    target = member.linkname
    if not target or target.startswith("/"):
        raise _unsafe(member, f"symbolic link with an empty or absolute target {target!r}")
    resolved = os.path.normpath(os.path.join(os.path.dirname(name), target))
    if resolved == ".." or resolved.startswith("../"):
        raise _unsafe(member, f"symbolic link pointing outside the destination ({target!r})")
    return os.path.normpath(target)


def _safe_mode(member: tarfile.TarInfo) -> int:
    """Strip setuid/setgid/sticky and group/other write; owner always gets rw (rwx for dirs)."""
    mode = member.mode & 0o755
    if member.isdir():
        return mode | stat.S_IRWXU
    if not mode & stat.S_IXUSR:
        mode &= ~0o111
    return mode | 0o600


def _inside(path: str, root: Path) -> bool:
    """True if the absolute ``path`` is ``root`` or below it."""
    return os.path.commonpath([path, str(root)]) == str(root)


def _check_on_disk(member: tarfile.TarInfo, root: Path) -> None:
    """Re-check a member against what is already on disk (symlinks extracted earlier)."""
    parent = os.path.dirname(member.name)
    if member.issym():
        location = os.path.realpath(os.path.join(root, parent))
        pointee = os.path.realpath(os.path.join(root, parent, member.linkname))
    else:
        location = os.path.realpath(os.path.join(root, member.name))
        pointee = os.path.realpath(os.path.join(root, member.linkname or member.name))
    if not (_inside(location, root) and _inside(pointee, root)):
        raise _unsafe(member, "it resolves outside the destination")


def _unlink_existing(path: Path) -> None:
    """Remove a file already at ``path`` so writing never goes through an old hard link."""
    with contextlib.suppress(FileNotFoundError, IsADirectoryError):
        os.unlink(path)


def _verify_symlinks(plan: list[tarfile.TarInfo], root: Path) -> None:
    """After extraction, every symlink (whole chain resolved) must stay inside ``root``."""
    for member in plan:
        if not member.issym():
            continue
        link = os.path.join(root, member.name)
        if not _inside(os.path.realpath(link), root):
            os.unlink(link)
            raise _unsafe(member, "symbolic link chain resolves outside the destination")


def clone_tree(src: Path, dst: Path, method: str = "auto") -> str:
    """Recreate directory ``src`` at ``dst`` (which must not exist); return ``'hardlink'`` or ``'copy'``.

    ``auto`` hard-links each file and falls back to ``shutil.copy2`` when
    linking fails; ``hardlink`` fails instead of copying; ``copy`` always
    copies. Symlinks are recreated, never followed; sockets, FIFOs and
    devices are skipped. Returns ``'copy'`` if any file was copied.
    """
    if method not in CLONE_METHODS:
        raise UsageError(f"unknown clone method {method!r}", hint="use auto, hardlink or copy")
    src = Path(src)
    dst = Path(dst)
    if src.is_symlink() or not src.is_dir():
        raise UsageError(f"cannot clone {src}: not a directory")
    if dst.resolve().is_relative_to(src.resolve()):
        raise UsageError(f"cannot clone {src} into itself ({dst})")
    dst.mkdir(parents=True)
    copied = _clone_dir(src, dst, method)
    return "copy" if copied or method == "copy" else "hardlink"


def _clone_dir(src: Path, dst: Path, method: str) -> bool:
    """Clone the contents of one directory; True if any file was copied."""
    copied = False
    with os.scandir(src) as entries:
        for entry in entries:
            target = dst / entry.name
            if entry.is_symlink():
                os.symlink(os.readlink(entry.path), target)
            elif entry.is_dir(follow_symlinks=False):
                target.mkdir()
                copied = _clone_dir(Path(entry.path), target, method) or copied
            elif entry.is_file(follow_symlinks=False):
                copied = clone_file(entry.path, target, method) or copied
    shutil.copystat(src, dst, follow_symlinks=False)
    return copied


def clone_file(src: str | Path, dst: Path, method: str) -> bool:
    """Hard-link or copy one file; True if it was copied."""
    if method != "copy":
        try:
            os.link(src, dst)
            return False
        except OSError as exc:
            if method == "hardlink":
                raise ForkLinuxError(
                    f"cannot hard-link {src}: {exc.strerror or exc}",
                    hint="set [snapshots] method=auto or copy",
                ) from exc
    shutil.copy2(src, dst, follow_symlinks=False)
    return True


def disk_free(path: Path) -> int:
    """Bytes available to us on the filesystem holding ``path`` (or its nearest existing parent)."""
    probe = Path(path).absolute()
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return shutil.disk_usage(probe).free
