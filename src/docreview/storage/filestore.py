"""带 staging 与原子发布的本地内容寻址存储。"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import tempfile
from collections.abc import Collection, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, NoReturn

from docreview.storage.postgres.errors import FileContentNotFoundError

_DIGEST = re.compile(r"[0-9a-f]{64}")
_PREFIX = re.compile(r"[0-9a-f]{2}")


class FileStoreError(RuntimeError):
    pass


class UnsafeStoragePathError(ValueError, FileStoreError):
    pass


class FileStorePermissionError(FileStoreError):
    pass


class FileStoreIOError(FileStoreError):
    pass


class HashCollisionError(FileStoreError):
    pass


@dataclass(frozen=True, slots=True)
class StoredFile:
    sha256: str
    size_bytes: int
    storage_key: str
    created: bool = False


@dataclass(frozen=True, slots=True)
class StagedFile:
    sha256: str
    size_bytes: int
    storage_key: str
    path: Path = field(repr=False)

    def descriptor(self) -> StoredFile:
        return StoredFile(
            sha256=self.sha256,
            size_bytes=self.size_bytes,
            storage_key=self.storage_key,
        )


@dataclass(frozen=True, slots=True)
class OrphanedObject:
    storage_key: str
    size_bytes: int


def _storage_key(digest: str) -> str:
    return f"{digest[:2]}/{digest}"


def _raise_io(action: str, error: OSError) -> NoReturn:
    if isinstance(error, PermissionError):
        raise FileStorePermissionError(f"{action}: {error}") from error
    raise FileStoreIOError(f"{action}: {error}") from error


def _digest_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


class LocalFileStore:
    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        protected_roots: Iterable[str | os.PathLike[str]] = (),
    ) -> None:
        raw = Path(root).expanduser()
        if not raw.is_absolute():
            raise UnsafeStoragePathError("存储根目录必须是绝对路径")
        resolved = raw.resolve()
        protected = {
            Path(resolved.anchor).resolve(),
            Path.home().resolve(),
            Path.cwd().resolve(),
            *(Path(value).expanduser().resolve() for value in protected_roots),
        }
        if resolved in protected or (resolved / ".git").exists():
            raise UnsafeStoragePathError("存储根目录不能是根目录、用户目录或仓库根目录")
        self._root = resolved
        self._staged: dict[Path, StagedFile] = {}
        self._closed = False
        self._prepare_root()

    @property
    def root(self) -> Path:
        return self._root

    @property
    def is_closed(self) -> bool:
        return self._closed

    def _prepare_root(self) -> None:
        try:
            self._root.mkdir(parents=True, mode=0o700, exist_ok=True)
            if not self._root.is_dir():
                raise FileStoreIOError("存储根路径不是目录")
            os.chmod(self._root, 0o700)
        except FileStoreError:
            raise
        except OSError as error:
            _raise_io("初始化存储目录失败", error)
        self._probe_writable()

    def _probe_writable(self) -> None:
        descriptor = -1
        probe: Path | None = None
        operation_error: OSError | None = None
        try:
            descriptor, raw_probe = tempfile.mkstemp(prefix=".write-probe-", dir=self._root)
            probe = Path(raw_probe)
            os.close(descriptor)
            descriptor = -1
            os.chmod(probe, 0o600)
        except OSError as error:
            operation_error = error
        finally:
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError as error:
                    operation_error = operation_error or error
            if probe is not None:
                try:
                    probe.unlink(missing_ok=True)
                except OSError as error:
                    operation_error = operation_error or error
        if operation_error is not None:
            _raise_io("初始化存储目录失败", operation_error)

    def _ensure_open(self) -> None:
        if self._closed:
            raise FileStoreIOError("文件存储已关闭")

    def _path(self, storage_key: str) -> Path:
        key = storage_key.strip()
        if "\\" in key or key.startswith("/"):
            raise UnsafeStoragePathError("文件 键 非法")
        parts = key.split("/")
        if (
            len(parts) != 2
            or _PREFIX.fullmatch(parts[0]) is None
            or _DIGEST.fullmatch(parts[1]) is None
            or not parts[1].startswith(parts[0])
        ):
            raise UnsafeStoragePathError("文件 键 不是合法内容地址")
        candidate = self._root / parts[0] / parts[1]
        resolved = candidate.resolve(strict=False)
        try:
            resolved.relative_to(self._root)
        except ValueError as error:
            raise UnsafeStoragePathError("文件 键 超出存储目录") from error
        current = self._root
        for part in parts:
            current /= part
            if current.is_symlink():
                raise UnsafeStoragePathError("文件 键 经过符号链接")
        return candidate

    async def stage(self, content: bytes) -> StagedFile:
        self._ensure_open()
        digest = hashlib.sha256(content).hexdigest()
        storage_key = _storage_key(digest)
        target = self._path(storage_key)

        def write() -> Path:
            try:
                target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
                if target.parent.is_symlink():
                    raise UnsafeStoragePathError("内容地址目录不能是符号链接")
                os.chmod(target.parent, 0o700)
                # staging 与最终对象同目录，确保 os.replace 不跨文件系统且保持原子性。
                descriptor, temporary = tempfile.mkstemp(
                    prefix=f".{digest}.staging-", dir=target.parent
                )
                try:
                    os.chmod(temporary, 0o600)
                    with os.fdopen(descriptor, "wb") as stream:
                        descriptor = -1
                        stream.write(content)
                        stream.flush()
                        os.fsync(stream.fileno())
                    return Path(temporary)
                except BaseException:
                    if descriptor >= 0:
                        os.close(descriptor)
                    Path(temporary).unlink(missing_ok=True)
                    raise
            except UnsafeStoragePathError:
                raise
            except OSError as error:
                _raise_io("写入 staging 文件失败", error)

        path = await asyncio.to_thread(write)
        staged = StagedFile(digest, len(content), storage_key, path)
        self._staged[path] = staged
        return staged

    def _validate_staged(self, staged: StagedFile) -> Path:
        if self._staged.get(staged.path) != staged:
            raise UnsafeStoragePathError("暂存 文件不属于当前存储实例")
        target = self._path(staged.storage_key)
        if staged.storage_key != _storage_key(staged.sha256) or staged.path.parent != target.parent:
            raise UnsafeStoragePathError("暂存 内容地址不一致")
        if staged.path.is_symlink():
            raise UnsafeStoragePathError("暂存 文件不能是符号链接")
        return target

    async def promote(self, staged: StagedFile) -> StoredFile:
        self._ensure_open()
        target = self._validate_staged(staged)

        def publish() -> bool:
            try:
                staged_digest, staged_size = _digest_file(staged.path)
                if staged_digest != staged.sha256 or staged_size != staged.size_bytes:
                    raise HashCollisionError("暂存 文件内容与内容地址不一致")
                if target.exists():
                    # 相同内容是幂等命中；同一内容地址出现不同内容则必须安全拒绝。
                    if target.is_symlink() or not target.is_file():
                        raise UnsafeStoragePathError("内容地址目标不是普通文件")
                    target_digest, target_size = _digest_file(target)
                    if target_digest != staged.sha256 or target_size != staged.size_bytes:
                        raise HashCollisionError("内容地址已存在不同内容")
                    staged.path.unlink()
                    return False
                os.replace(staged.path, target)
                return True
            except FileStoreError:
                raise
            except OSError as error:
                _raise_io("原子提升文件失败", error)

        created = await asyncio.to_thread(publish)
        self._staged.pop(staged.path, None)
        return StoredFile(staged.sha256, staged.size_bytes, staged.storage_key, created)

    async def save(self, content: bytes) -> StoredFile:
        staged = await self.stage(content)
        try:
            return await self.promote(staged)
        except BaseException as error:
            try:
                await self.cleanup_staged(staged)
            except Exception as cleanup_error:
                error.add_note(f"暂存区清理也失败了: {cleanup_error}")
            raise

    async def cleanup_staged(self, staged: StagedFile) -> None:
        self._validate_staged(staged)
        try:
            await asyncio.to_thread(staged.path.unlink, missing_ok=True)
        except OSError as error:
            _raise_io("清理 staging 文件失败", error)
        self._staged.pop(staged.path, None)

    async def stat(self, storage_key: str) -> int | None:
        self._ensure_open()
        path = self._path(storage_key)
        try:
            return (await asyncio.to_thread(path.stat)).st_size
        except FileNotFoundError:
            return None
        except OSError as error:
            _raise_io("读取文件元信息失败", error)

    async def delete(self, storage_key: str) -> None:
        self._ensure_open()
        path = self._path(storage_key)
        try:
            await asyncio.to_thread(path.unlink, missing_ok=True)
        except OSError as error:
            _raise_io("删除未提交文件失败", error)

    async def open(self, storage_key: str) -> BinaryIO:
        self._ensure_open()
        path = self._path(storage_key)
        try:
            return await asyncio.to_thread(path.open, "rb")
        except FileNotFoundError as error:
            raise FileContentNotFoundError from error
        except OSError as error:
            _raise_io("打开文件失败", error)

    async def scan_orphans(self, authorized_keys: Collection[str]) -> list[OrphanedObject]:
        self._ensure_open()
        authorized = {key.strip() for key in authorized_keys}
        for key in authorized:
            self._path(key)

        def scan() -> list[OrphanedObject]:
            result: list[OrphanedObject] = []
            try:
                for prefix in sorted(self._root.iterdir()):
                    if prefix.name.startswith("."):
                        continue
                    if prefix.is_symlink():
                        raise UnsafeStoragePathError("存储目录包含符号链接")
                    if not prefix.is_dir() or _PREFIX.fullmatch(prefix.name) is None:
                        continue
                    for path in sorted(prefix.iterdir()):
                        if path.name.startswith("."):
                            continue
                        if path.is_symlink():
                            raise UnsafeStoragePathError("存储对象不能是符号链接")
                        key = f"{prefix.name}/{path.name}"
                        if (
                            path.is_file()
                            and _DIGEST.fullmatch(path.name) is not None
                            and path.name.startswith(prefix.name)
                            and key not in authorized
                        ):
                            result.append(OrphanedObject(key, path.stat().st_size))
                return result
            except FileStoreError:
                raise
            except OSError as error:
                _raise_io("扫描孤立文件失败", error)

        return await asyncio.to_thread(scan)

    async def aclose(self) -> None:
        if self._closed:
            return
        for staged in tuple(self._staged.values()):
            await self.cleanup_staged(staged)
        self._closed = True


__all__ = [
    "FileStoreError",
    "FileStoreIOError",
    "FileStorePermissionError",
    "HashCollisionError",
    "LocalFileStore",
    "OrphanedObject",
    "StagedFile",
    "StoredFile",
    "UnsafeStoragePathError",
]
