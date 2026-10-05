from pathlib import Path

import pytest

from copilot_core.migrate import load_migrations

REPO_MIGRATIONS = Path(__file__).resolve().parents[3] / "migrations"


def test_repo_migrations_load_in_order() -> None:
    migrations = load_migrations(REPO_MIGRATIONS)
    assert migrations[0].name == "0001_initial"
    assert [m.version for m in migrations] == sorted(m.version for m in migrations)


def test_checksum_ignores_line_endings(tmp_path: Path) -> None:
    (tmp_path / "0001_a.sql").write_bytes(b"select 1;\r\nselect 2;\r\n")
    crlf = load_migrations(tmp_path)[0].checksum
    (tmp_path / "0001_a.sql").write_bytes(b"select 1;\nselect 2;\n")
    assert load_migrations(tmp_path)[0].checksum == crlf


@pytest.mark.parametrize("name", ["1_short.sql", "0001-dash.sql", "0001_Upper.sql"])
def test_rejects_bad_filenames(tmp_path: Path, name: str) -> None:
    (tmp_path / name).write_text("select 1;")
    with pytest.raises(SystemExit, match="bad migration filename"):
        load_migrations(tmp_path)


def test_rejects_duplicate_versions(tmp_path: Path) -> None:
    (tmp_path / "0001_a.sql").write_text("select 1;")
    (tmp_path / "0001_b.sql").write_text("select 2;")
    with pytest.raises(SystemExit, match="duplicate"):
        load_migrations(tmp_path)
