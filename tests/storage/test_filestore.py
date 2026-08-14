from __future__ import annotations

from pathlib import Path

import pytest

from docreview.storage.filestore import LocalFileStore
from docreview.storage.postgres.errors import FileContentNotFoundError


@pytest.mark.anyio
async def test_local_file_store_reads_and_stats_a_key(tmp_path: Path) -> None:
    store = LocalFileStore(tmp_path)
    target = tmp_path / "aa" / "file"
    target.parent.mkdir()
    target.write_bytes(b"payload")

    assert await store.stat("aa/file") == 7
    stream = await store.open("aa/file")
    try:
        assert stream.read() == b"payload"
    finally:
        stream.close()


@pytest.mark.anyio
@pytest.mark.parametrize("key", ["", ".", "..", "../secret", "..\\secret", "C:/secret"])
async def test_local_file_store_rejects_unsafe_keys(tmp_path: Path, key: str) -> None:
    store = LocalFileStore(tmp_path)

    with pytest.raises(ValueError):
        await store.stat(key)


@pytest.mark.anyio
async def test_local_file_store_maps_missing_content(tmp_path: Path) -> None:
    store = LocalFileStore(tmp_path)

    with pytest.raises(FileContentNotFoundError):
        await store.open("missing")


@pytest.mark.anyio
async def test_failed_atomic_replace_removes_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = LocalFileStore(tmp_path)

    def fail_replace(source: Path, target: Path) -> Path:
        raise OSError("disk write failed")

    monkeypatch.setattr(Path, "replace", fail_replace)

    with pytest.raises(OSError, match="disk write failed"):
        await store.save(b"content")

    assert [path for path in tmp_path.rglob("*") if path.is_file()] == []
