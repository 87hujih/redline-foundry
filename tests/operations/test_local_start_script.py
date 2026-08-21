from pathlib import Path

SCRIPT = Path(__file__).parents[2] / "scripts" / "start-local.ps1"


def test_local_start_script_owns_required_dependency_sequence() -> None:
    content = SCRIPT.read_text(encoding="utf-8")

    assert "postgres-data-17-v2" in content
    assert "postgresql-17" in content
    assert "127.0.0.1" in content and "55432" in content
    assert "apache/tika:3.3.0.0" in content
    assert "127.0.0.1:9998:9998" in content
    assert "uv run docreview-init-local" in content
    assert "uv run docreview-api" in content
    assert content.index("uv run docreview-init-local") < content.index("uv run docreview-api")


def test_local_start_script_does_not_own_destructive_shutdown() -> None:
    content = SCRIPT.read_text(encoding="utf-8").lower()

    assert "docker rm" not in content
    assert "pg_ctl.exe stop" not in content
    assert "remove-item" not in content
