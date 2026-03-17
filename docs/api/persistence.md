# API: Persistence

## `setup_persistence(bot=None, *, file_path=None, backend=None)`

Single entry point for all persistence. Call once in `setup_hook`, after loading cogs.

```python
from cascadeui import setup_persistence
from cascadeui.persistence import SQLiteBackend

# SQLite backend (recommended)
await setup_persistence(bot, backend=SQLiteBackend("cascadeui.db"))

# JSON file backend
await setup_persistence(bot, file_path="bot_state.json")

# Data-only (no view re-attachment)
await setup_persistence(backend=SQLiteBackend("cascadeui.db"))
```

**Returns:** `dict` with keys `restored`, `skipped`, `failed`, `removed` (lists of state keys).

---

## Storage Backends

### `FileStorageBackend`

JSON-based persistence backend. Used by default when `file_path` is passed.

```python
from cascadeui.persistence import FileStorageBackend

backend = FileStorageBackend("my_state.json")
```

#### Methods

- `save_state(state)` - serializes and writes to disk (creates `.bak` first)
- `load_state()` - reads and deserializes from disk

### `SQLiteBackend`

SQLite-based persistence via `aiosqlite`. Uses WAL mode for safe concurrent access.

```python
from cascadeui.persistence import SQLiteBackend

backend = SQLiteBackend("cascadeui.db")
```

**Requires:** `pip install cascadeui[sqlite]`

#### Methods

- `save_state(state)` - serializes to JSON and writes to a single-row table
- `load_state()` - reads and deserializes from the table (auto-creates if missing)

### `RedisBackend`

Redis-based persistence via `redis.asyncio`.

```python
from cascadeui.persistence import RedisBackend

backend = RedisBackend(url="redis://localhost", key="cascadeui:state", ttl=None)
```

**Requires:** `pip install cascadeui[redis]`

#### Constructor Parameters

- `url` (str): Redis connection URL
- `key` (str): Redis key for the state blob (default: `"cascadeui:state"`)
- `ttl` (int | None): Optional TTL in seconds for the key

---

## `migrate_storage(source, target)`

Copies state from one backend to another:

```python
from cascadeui.persistence import migrate_storage, FileStorageBackend, SQLiteBackend

await migrate_storage(
    source=FileStorageBackend("old_state.json"),
    target=SQLiteBackend("cascadeui.db"),
)
```

---

## `StorageBackend` (Protocol)

Interface for custom backends. Implement `save_state(state)` and `load_state()`:

```python
class MyBackend:
    async def save_state(self, state: dict) -> bool:
        ...  # Return True on success

    async def load_state(self) -> dict:
        ...  # Return empty dict if no saved state
```

---

## `StateSerializer`

Handles serialization of non-standard types:

- `datetime` objects are converted to/from ISO format strings
- `set` objects are converted to/from lists
- Other non-serializable types raise `TypeError`
