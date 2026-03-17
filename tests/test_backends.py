"""Tests for 7.1 — Database Persistence Backends."""

import os
import pytest

from cascadeui.persistence.storage import FileStorageBackend
from cascadeui.persistence.migration import migrate_storage


# // ========================================( SQLite )======================================== // #


sqlite_available = False
try:
    import aiosqlite
    sqlite_available = True
except ImportError:
    pass


@pytest.mark.skipif(not sqlite_available, reason="aiosqlite not installed")
class TestSQLiteBackend:
    @pytest.fixture(autouse=True)
    def cleanup_db(self, tmp_path):
        self.db_path = str(tmp_path / "test.db")
        yield
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    async def test_save_load_roundtrip(self):
        from cascadeui.persistence.sqlite import SQLiteBackend

        backend = SQLiteBackend(self.db_path)
        state = {"sessions": {}, "views": {}, "application": {"key": "value"}}

        result = await backend.save_state(state)
        assert result is True

        loaded = await backend.load_state()
        assert loaded is not None
        assert loaded["application"]["key"] == "value"

    async def test_table_auto_creation(self):
        from cascadeui.persistence.sqlite import SQLiteBackend

        backend = SQLiteBackend(self.db_path)
        # First load on empty db returns None
        loaded = await backend.load_state()
        assert loaded is None

    async def test_overwrite_existing(self):
        from cascadeui.persistence.sqlite import SQLiteBackend

        backend = SQLiteBackend(self.db_path)

        await backend.save_state({"application": {"v": 1}})
        await backend.save_state({"application": {"v": 2}})

        loaded = await backend.load_state()
        assert loaded["application"]["v"] == 2


# // ========================================( Migration )======================================== // #


class TestMigration:
    async def test_migrate_file_to_file(self, tmp_path):
        source_path = str(tmp_path / "source.json")
        target_path = str(tmp_path / "target.json")

        source = FileStorageBackend(source_path)
        target = FileStorageBackend(target_path)

        state = {"sessions": {}, "views": {}, "application": {"migrated": True}}
        await source.save_state(state)

        success = await migrate_storage(source, target)
        assert success is True

        loaded = await target.load_state()
        assert loaded is not None
        assert loaded["application"]["migrated"] is True

    async def test_migrate_empty_source(self, tmp_path):
        source_path = str(tmp_path / "empty.json")
        target_path = str(tmp_path / "target2.json")

        source = FileStorageBackend(source_path)
        target = FileStorageBackend(target_path)

        success = await migrate_storage(source, target)
        assert success is False


# // ========================================( setup_persistence backend= )======================================== // #


class TestSetupPersistenceBackend:
    async def test_setup_with_file_backend(self, tmp_path):
        """setup_persistence() with file_path still works (backwards compat)."""
        from cascadeui.state.singleton import get_store
        from cascadeui.views.persistent import setup_persistence

        store = get_store()
        file_path = str(tmp_path / "compat.json")

        result = await setup_persistence(file_path=file_path)
        assert result == {"restored": [], "skipped": [], "failed": [], "removed": []}
        assert store.persistence_enabled is True

    async def test_setup_with_custom_backend(self, tmp_path):
        """setup_persistence() with backend= uses the provided backend."""
        from cascadeui.state.singleton import get_store
        from cascadeui.views.persistent import setup_persistence

        store = get_store()
        backend = FileStorageBackend(str(tmp_path / "custom.json"))

        result = await setup_persistence(backend=backend)
        assert result == {"restored": [], "skipped": [], "failed": [], "removed": []}
        assert store.persistence_enabled is True
        assert store.persistence_backend is backend
