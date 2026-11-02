"""PRAGMA user_version으로 스키마 버전을 올리는 마이그레이션 틀을 확인한다."""
import sqlite3

import pytest

from app import db as db_module
from app.db import DB


def user_version(path) -> int:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


def table_names(path) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


def make_v1(path, monkeypatch) -> None:
    """마이그레이션이 하나도 없던 시절(버전 1)의 DB 파일을 만든다."""
    monkeypatch.setattr(db_module, "MIGRATIONS", [])
    DB(path).close()


def test_new_db_starts_at_base_version(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "MIGRATIONS", [])
    path = tmp_path / "new.sqlite3"
    DB(path).close()
    assert user_version(path) == 1
    memory = DB(":memory:")
    assert memory.one("PRAGMA user_version")["user_version"] == 1
    memory.close()


def test_pending_migration_is_applied_once(tmp_path, monkeypatch):
    path = tmp_path / "v1.sqlite3"
    make_v1(path, monkeypatch)
    monkeypatch.setattr(db_module, "MIGRATIONS", [(2, ["CREATE TABLE t2(x INTEGER)"])])
    DB(path).close()
    assert "t2" in table_names(path) and user_version(path) == 2
    DB(path).close()  # 이미 적용한 버전은 다시 실행하지 않는다(CREATE TABLE이 두 번이면 오류)
    assert user_version(path) == 2


def test_new_db_runs_all_migrations(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "MIGRATIONS", [
        (3, ["ALTER TABLE t2 ADD COLUMN y TEXT"]),  # 순서가 섞여 있어도 버전 순서로 적용한다
        (2, ["CREATE TABLE t2(x INTEGER)"]),
    ])
    memory = DB(":memory:")
    assert memory.one("PRAGMA user_version")["user_version"] == 3
    memory.exec("INSERT INTO t2(x, y) VALUES(1, 'a')")
    memory.close()


def test_failed_migration_rolls_back_with_version(tmp_path, monkeypatch):
    path = tmp_path / "v1.sqlite3"
    make_v1(path, monkeypatch)
    monkeypatch.setattr(db_module, "MIGRATIONS", [(2, ["CREATE TABLE t2(x)", "SYNTAX ERROR"])])
    with pytest.raises(sqlite3.Error):
        DB(path)
    assert "t2" not in table_names(path)
    assert user_version(path) == 1


def test_db_from_newer_code_is_refused(tmp_path, monkeypatch):
    path = tmp_path / "v1.sqlite3"
    make_v1(path, monkeypatch)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version = 99")
    conn.close()
    with pytest.raises(RuntimeError, match="더 새로운 버전"):
        DB(path)
