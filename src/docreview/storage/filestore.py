"""Read-only local file storage adapter used by the download route."""

from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path
from typing import BinaryIO

from docreview.storage.postgres.errors import FileContentNotFoundError


class LocalFileStore:
    """Resolve storage keys below a configured root and expose async reads."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self._root = Path(root).expanduser().resolve()

    def _path(self, storage_key: str) -> Path:
        key = storage_key.strip()
        if not key or key in {".", ".."} or "\\" in key:
            raise ValueError("文件 key 非法")
        candidate = (self._root / key).resolve()
        try:
            candidate.relative_to(self._root)
        except ValueError as error:
            raise ValueError("文件 key 超出存储目录") from error
        return candidate

    async def save(self, content: bytes) -> StoredFile:
        """Persist bytes under a deterministic SHA-256 content-addressed key."""
        digest = hashlib.sha256(content).hexdigest()
        storage_key = f"{digest[:2]}/{digest}"
        path = self._path(storage_key)

        def write() -> bool:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                return False
            temporary = path.with_name(f".{path.name}.tmp")
            try:
                temporary.write_bytes(content)
                temporary.replace(path)
                return True
            except FileExistsError:
                return False
            finally:
                temporary.unlink(missing_ok=True)

        created = await asyncio.to_thread(write)
        return StoredFile(
            sha256=digest,
            size_bytes=len(content),
            storage_key=storage_key,
            created=created,
        )

    async def stat(self, storage_key: str) -> int | None:
        path = self._path(storage_key)
        try:
            return (await asyncio.to_thread(path.stat)).st_size
        except (FileNotFoundError, OSError):
            return None

    async def delete(self, storage_key: str) -> None:
        """Remove an uncommitted object during upload compensation."""
        path = self._path(storage_key)
        await asyncio.to_thread(path.unlink, missing_ok=True)

    async def open(self, storage_key: str) -> BinaryIO:
        path = self._path(storage_key)
        try:
            return await asyncio.to_thread(path.open, "rb")
        except FileNotFoundError as error:
            raise FileContentNotFoundError from error


class StoredFile:
    def __init__(
        self, *, sha256: str, size_bytes: int, storage_key: str, created: bool = False
    ) -> None:
        self.sha256 = sha256
        self.size_bytes = size_bytes
        self.storage_key = storage_key
        self.created = created


__all__ = ["LocalFileStore", "StoredFile"]
