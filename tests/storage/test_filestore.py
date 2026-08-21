from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from typing import IO, Any, cast

import pytest

from docreview.storage.filestore import (
    FileStoreIOError,
    FileStorePermissionError,
    HashCollisionError,
    LocalFileStore,
    UnsafeStoragePathError,
)
from docreview.storage.postgres.errors import FileContentNotFoundError


def key_for(content: bytes) -> str:
    digest = hashlib.sha256(content).hexdigest()
    return f"{digest[:2]}/{digest}"


@pytest.mark.anyio
async def test_stage_and_promote_use_same_directory_atomic_content_address(tmp_path: Path) -> None:
    store = LocalFileStore(tmp_path / "objects")
    content = b"payload"

    staged = await store.stage(content)

    final_path = store.root / staged.storage_key
    assert staged.path.parent == final_path.parent
    assert staged.path.exists()
    assert not final_path.exists()
    if os.name != "nt":
        assert stat.S_IMODE(staged.path.stat().st_mode) & 0o077 == 0

    stored = await store.promote(staged)

    assert stored.created is True
    assert stored.storage_key == key_for(content)
    assert final_path.read_bytes() == content
    assert not staged.path.exists()
    if os.name != "nt":
        assert stat.S_IMODE(final_path.stat().st_mode) & 0o077 == 0


@pytest.mark.anyio
async def test_duplicate_content_write_is_idempotent(tmp_path: Path) -> None:
    store = LocalFileStore(tmp_path / "objects")

    first = await store.save(b"same content")
    second = await store.save(b"same content")

    assert first.created is True
    assert second.created is False
    assert first.storage_key == second.storage_key
    assert await store.stat(first.storage_key) == len(b"same content")


@pytest.mark.anyio
@pytest.mark.parametrize(
    "key",
    [
        "",
        ".",
        "..",
        "../secret",
        "..\\secret",
        "/absolute/path",
        "C:/absolute/path",
        "aa/not-a-sha256",
        "bb/" + "a" * 64,
        "aa/" + "A" * 64,
    ],
)
async def test_local_file_store_rejects_unsafe_or_non_content_keys(
    tmp_path: Path, key: str
) -> None:
    store = LocalFileStore(tmp_path / "objects")

    with pytest.raises(UnsafeStoragePathError):
        await store.stat(key)


@pytest.mark.anyio
async def test_symbolic_link_cannot_escape_storage_root(tmp_path: Path) -> None:
    store = LocalFileStore(tmp_path / "objects")
    content = b"outside"
    storage_key = key_for(content)
    prefix, digest = storage_key.split("/")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / digest).write_bytes(content)
    link = store.root / prefix
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"symlinks are unavailable: {error}")

    with pytest.raises(UnsafeStoragePathError):
        await store.open(storage_key)


@pytest.mark.anyio
async def test_symbolic_link_component_is_rejected_without_os_privilege(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = LocalFileStore(tmp_path / "objects")
    storage_key = key_for(b"payload")
    prefix = store.root / storage_key.split("/", 1)[0]
    original = Path.is_symlink

    def simulated_symlink(path: Path) -> bool:
        return path == prefix or original(path)

    monkeypatch.setattr(Path, "is_symlink", simulated_symlink)

    with pytest.raises(UnsafeStoragePathError, match="符号链接"):
        await store.open(storage_key)


@pytest.mark.anyio
async def test_failed_atomic_replace_cleans_staging_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = LocalFileStore(tmp_path / "objects")
    calls: list[tuple[Path, Path]] = []

    def fail_replace(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
        calls.append((Path(source), Path(target)))
        raise OSError("disk write failed")

    monkeypatch.setattr(os, "replace", fail_replace)

    with pytest.raises(FileStoreIOError, match="disk write failed"):
        await store.save(b"content")

    assert len(calls) == 1
    assert calls[0][0].parent == calls[0][1].parent
    assert [path for path in store.root.rglob("*") if path.is_file()] == []


@pytest.mark.anyio
async def test_existing_wrong_content_is_reported_as_hash_collision(tmp_path: Path) -> None:
    store = LocalFileStore(tmp_path / "objects")
    staged = await store.stage(b"expected")
    target = store.root / staged.storage_key
    target.write_bytes(b"different")

    with pytest.raises(HashCollisionError):
        await store.promote(staged)

    await store.cleanup_staged(staged)
    assert target.read_bytes() == b"different"


@pytest.mark.anyio
async def test_missing_permission_and_io_failures_are_distinct(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = LocalFileStore(tmp_path / "objects")
    stored = await store.save(b"payload")

    with pytest.raises(FileContentNotFoundError):
        await store.open(key_for(b"missing"))

    original_open = Path.open

    def denied(path: Path, *args: Any, **kwargs: Any) -> IO[Any]:
        if path == store.root / stored.storage_key:
            raise PermissionError("denied")
        return cast(IO[Any], original_open(path, *args, **kwargs))

    monkeypatch.setattr(Path, "open", denied)
    with pytest.raises(FileStorePermissionError, match="denied"):
        await store.open(stored.storage_key)

    def failed(path: Path, *args: Any, **kwargs: Any) -> IO[Any]:
        if path == store.root / stored.storage_key:
            raise OSError("device failed")
        return cast(IO[Any], original_open(path, *args, **kwargs))

    monkeypatch.setattr(Path, "open", failed)
    with pytest.raises(FileStoreIOError, match="device failed"):
        await store.open(stored.storage_key)


@pytest.mark.anyio
async def test_close_cleans_active_staging_and_is_repeatable(tmp_path: Path) -> None:
    store = LocalFileStore(tmp_path / "objects")
    staged = await store.stage(b"payload")

    await store.aclose()
    await store.aclose()

    assert not staged.path.exists()


@pytest.mark.anyio
async def test_orphan_scan_is_read_only_and_uses_authorized_database_keys(tmp_path: Path) -> None:
    store = LocalFileStore(tmp_path / "objects")
    authorized = await store.save(b"authorized")
    orphan = await store.save(b"orphan")
    staged = await store.stage(b"still uploading")

    result = await store.scan_orphans({authorized.storage_key})

    assert [item.storage_key for item in result] == [orphan.storage_key]
    assert (store.root / authorized.storage_key).exists()
    assert (store.root / orphan.storage_key).exists()
    assert staged.path.exists()
    await store.cleanup_staged(staged)


def test_store_rejects_broad_or_non_directory_roots(tmp_path: Path) -> None:
    file_root = tmp_path / "not-a-directory"
    file_root.write_text("content", encoding="utf-8")

    with pytest.raises(UnsafeStoragePathError):
        LocalFileStore(Path.cwd())
    with pytest.raises(FileStoreIOError):
        LocalFileStore(file_root)

    repository_root = tmp_path / "repository"
    (repository_root / ".git").mkdir(parents=True)
    with pytest.raises(UnsafeStoragePathError):
        LocalFileStore(repository_root)
