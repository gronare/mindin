from __future__ import annotations

import hashlib
import os
import threading
from datetime import UTC, datetime
from pathlib import Path

from mindin.backends import MUST_NOT_EXIST, Entry, NotFound, VaultError, VersionConflict, is_vault_key

CHECKED_WRITES = threading.Lock()


def _version(data: bytes) -> str:
    return hashlib.md5(data, usedforsecurity=False).hexdigest()


def _folder_version(stat: os.stat_result) -> str:
    return f"{stat.st_mtime_ns}:{stat.st_size}"


def _stamp(stat: os.stat_result) -> tuple[int, int, int, int]:
    return (stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size)


def _entry(key: str, stat: os.stat_result, version: str, size: int | None = None) -> Entry:
    return Entry(
        key=key,
        version=version,
        mtime=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
        size=stat.st_size if size is None else size,
    )


class LocalBackend:
    def __init__(self, root: Path) -> None:
        Path(root).mkdir(parents=True, exist_ok=True)
        self.root = Path(root).resolve()
        self._listed: dict[Path, tuple[tuple[int, int, int, int], str]] = {}

    def _listed_version(self, path: Path, stat: os.stat_result) -> str:
        stamp = _stamp(stat)
        known = self._listed.get(path)
        if known is not None and known[0] == stamp:
            return known[1]
        try:
            version = _version(path.read_bytes())
        except OSError:
            return _folder_version(stat)
        self._listed[path] = (stamp, version)
        return version

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root):
            raise NotFound(f"outside the vault: {key}")
        return path

    def list(self) -> list[Entry]:
        entries: list[Entry] = []
        for path in self.root.rglob("*"):
            if not path.is_file():
                continue
            key = path.relative_to(self.root).as_posix()
            if not is_vault_key(key):
                continue
            stat = path.stat()
            entries.append(_entry(key, stat, self._listed_version(path, stat)))
        return sorted(entries, key=lambda entry: entry.key)

    def _under(self, prefix: str) -> list[Path]:
        if not prefix:
            return list(self.root.rglob("*"))
        parent, _, name = prefix.rpartition("/")
        base = self.root / parent if parent else self.root
        self._path(parent or ".")
        if not base.is_dir():
            return []
        paths = [base] if parent and not name else []
        for child in base.iterdir():
            if child.name.startswith(name):
                paths.append(child)
                if child.is_dir() and not child.is_symlink():
                    paths.extend(child.rglob("*"))
        return paths

    def list_all(self, prefix: str = "") -> list[Entry]:
        entries: list[Entry] = []
        for path in self._under(prefix):
            key = path.relative_to(self.root).as_posix()
            if path.is_dir():
                key += "/"
            if prefix and not key.startswith(prefix):
                continue
            stat = path.stat()
            if path.is_dir():
                entries.append(_entry(key, stat, _folder_version(stat), size=0))
            else:
                entries.append(_entry(key, stat, self._listed_version(path, stat)))
        return sorted(entries, key=lambda entry: entry.key)

    def get(self, key: str) -> tuple[str, str]:
        data, version = self.get_bytes(key)
        return data.decode("utf-8"), version

    def get_bytes(self, key: str) -> tuple[bytes, str]:
        path = self._path(key)
        try:
            data = path.read_bytes()
        except (FileNotFoundError, IsADirectoryError) as exc:
            raise NotFound(key) from exc
        return data, _version(data)

    def current_version(self, key: str) -> str | None:
        path = self._path(key)
        try:
            return _version(path.read_bytes())
        except IsADirectoryError:
            return _folder_version(path.stat())
        except FileNotFoundError:
            return None

    def put(self, key: str, text: str, expected_version: str | None = None) -> str:
        return self.put_checked(key, text.encode("utf-8"), expected_version)

    def put_checked(
        self, key: str, data: bytes, expected_version: str | None = None, content_type: str | None = None
    ) -> str:
        if expected_version is None:
            return self.put_bytes(key, data)
        with CHECKED_WRITES:
            current = self.current_version(key)
            if (current is not None) if expected_version == MUST_NOT_EXIST else (current != expected_version):
                raise VersionConflict(f"{key} changed since it was read (etag {current}, expected {expected_version})")
            return self.put_bytes(key, data)

    def put_bytes(self, key: str, data: bytes, content_type: str | None = None) -> str:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return _version(data)

    def mkdir(self, key: str) -> None:
        self._path(key.rstrip("/")).mkdir(parents=True, exist_ok=True)

    def delete(self, key: str) -> None:
        path = self._path(key)
        if path == self.root:
            raise VaultError("refusing to delete the vault root")
        if path.is_dir():
            try:
                path.rmdir()
            except OSError as exc:
                raise VaultError(f"refusing to delete a folder that is not empty: {key}") from exc
            return
        try:
            path.unlink()
        except FileNotFoundError as exc:
            raise NotFound(key) from exc

    def delete_checked(self, key: str, expected_version: str) -> None:
        with CHECKED_WRITES:
            if self.current_version(key) != expected_version:
                raise VersionConflict(f"{key} changed since it was read (expected {expected_version})")
            self.delete(key)

    def move(self, src: str, dst: str) -> None:
        source = self._path(src)
        target = self._path(dst)
        if not source.exists():
            raise NotFound(src)
        if source.is_dir():
            raise VaultError(f"refusing to move a folder: {src}")
        target.parent.mkdir(parents=True, exist_ok=True)
        source.replace(target)
