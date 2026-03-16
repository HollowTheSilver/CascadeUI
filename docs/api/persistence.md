# API: Persistence

## `setup_persistence(bot=None, file_path="cascadeui_state.json")`

Single entry point for all persistence. Call once in `setup_hook`, after loading cogs.

```python
# Data-only
await setup_persistence(file_path="bot_state.json")

# Data + view re-attachment
await setup_persistence(bot, file_path="bot_state.json")
```

**Returns:** `dict` with keys `restored`, `skipped`, `failed`, `removed` (lists of state keys).

---

## `FileStorageBackend`

JSON-based persistence backend. Used internally by `setup_persistence`.

```python
from cascadeui.persistence import FileStorageBackend

backend = FileStorageBackend("my_state.json")
```

### Methods

- `save_state(state)` - serializes and writes to disk (creates `.bak` first)
- `load_state()` - reads and deserializes from disk

---

## `StateSerializer`

Handles serialization of non-standard types:

- `datetime` objects are converted to/from ISO format strings
- `set` objects are converted to/from lists
- Other non-serializable types raise `TypeError`

---

## `StorageBackend` (Protocol)

Interface for custom backends. Implement `save_state(state)` and `load_state()`:

```python
class MyBackend:
    async def save_state(self, state: dict):
        ...

    async def load_state(self) -> dict:
        ...
```

Pass to `store.enable_persistence(backend)` or write a custom setup function.
