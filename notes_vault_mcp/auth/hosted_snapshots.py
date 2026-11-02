from __future__ import annotations

import os
import tempfile
from pathlib import Path

from notes_vault_mcp.auth.hosted_keys import SealedVaults, empty_storage
from notes_vault_mcp.backends import NotFound
from notes_vault_mcp.backends.sealed import SealedBackend
from notes_vault_mcp.index import copy_index

SNAPSHOT_KEY = "index.sqlite"


class IndexSnapshots:
    def __init__(self, sealed: SealedVaults) -> None:
        self.sealed = sealed

    def _blobs(self, vault_id: str) -> SealedBackend | None:
        data_key = self.sealed.keys.existing_key(vault_id)
        return None if data_key is None else SealedBackend(self.sealed.snapshot_storage(vault_id), data_key)

    def save(self, vault_id: str, index_path: Path) -> bool:
        blobs = self._blobs(vault_id)
        if blobs is None or not index_path.exists():
            return False
        with tempfile.TemporaryDirectory(dir=index_path.parent) as folder:
            copy = Path(folder) / SNAPSHOT_KEY
            copy_index(index_path, copy)
            blobs.put_checked(SNAPSHOT_KEY, copy.read_bytes())
        if not self.sealed.keys.has_key(vault_id):
            empty_storage(blobs.inner)
            return False
        return True

    def restore(self, vault_id: str, index_path: Path) -> bool:
        blobs = self._blobs(vault_id)
        if blobs is None:
            return False
        try:
            data, _ = blobs.get_bytes(SNAPSHOT_KEY)
        except NotFound:
            return False
        index_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        partial = index_path.with_name(index_path.name + ".restoring")
        partial.write_bytes(data)
        os.replace(partial, index_path)
        return True
