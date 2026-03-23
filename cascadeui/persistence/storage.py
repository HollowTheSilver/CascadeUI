# // ========================================( Modules )======================================== // #


import asyncio
import json
import os
from typing import Any, Dict, Optional

from ..utils.errors import RetryConfig, safe_execute, with_error_boundary, with_retry

# Package-level logger
from ..utils.logging import AsyncLogger
from ..utils.tasks import get_task_manager
from .serialization import StateSerializer

logger = AsyncLogger(name=__name__, level="DEBUG", path="logs", mode="a", prefix="cascadeui")


# // ========================================( Classes )======================================== // #


class StorageBackend:
    """Base class for state storage backends."""

    async def save_state(self, state: Dict[str, Any]) -> bool:
        """Save state to persistent storage."""
        raise NotImplementedError("Subclasses must implement save_state")

    async def load_state(self) -> Optional[Dict[str, Any]]:
        """Load state from persistent storage."""
        raise NotImplementedError("Subclasses must implement load_state")


class FileStorageBackend(StorageBackend):
    """Stores state in a local JSON file."""

    def __init__(self, file_path: str = "cascadeui_state.json"):
        self.file_path = file_path
        self.task_manager = get_task_manager()

    @with_retry(RetryConfig(max_retries=3, exceptions_to_retry=(IOError, OSError)))
    @with_error_boundary("save_state")
    async def save_state(self, state: Dict[str, Any]) -> bool:
        """Save state to file with retries."""
        try:
            # Back up the current file before overwriting
            await self.create_backup()

            # Serialize state
            data = StateSerializer.serialize(state)

            # Use a temporary file to avoid corruption if interrupted
            temp_path = f"{self.file_path}.tmp"

            # Write to file (use run_in_executor to make file I/O non-blocking)
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, lambda: self._write_file(temp_path, data))

            # Rename temp file to actual file (atomic operation on most platforms)
            await loop.run_in_executor(None, lambda: os.replace(temp_path, self.file_path))

            logger.info(f"State saved successfully to {self.file_path}")
            return True
        except Exception as e:
            logger.error(f"Error saving state to file: {e}")
            return False

    @with_error_boundary("load_state")
    async def load_state(self) -> Optional[Dict[str, Any]]:
        """Load state from file with error boundary."""
        try:
            # Check if file exists
            if not os.path.exists(self.file_path):
                logger.info(f"State file not found: {self.file_path}")
                return None

            # Handle backup file if main file is corrupted
            backup_path = f"{self.file_path}.bak"

            # Read from file (use run_in_executor to make file I/O non-blocking)
            loop = asyncio.get_running_loop()
            try:
                data = await loop.run_in_executor(None, lambda: self._read_file(self.file_path))
                # Create backup of good file
                await loop.run_in_executor(None, lambda: self._write_file(backup_path, data))
            except (IOError, json.JSONDecodeError) as e:
                logger.warning(f"Error reading main state file, trying backup: {e}")
                if os.path.exists(backup_path):
                    data = await loop.run_in_executor(None, lambda: self._read_file(backup_path))
                else:
                    raise

            # Deserialize state
            state = StateSerializer.deserialize(data)
            logger.info(f"State loaded successfully from {self.file_path}")
            return state
        except Exception as e:
            logger.error(f"Error loading state from file: {e}")
            return None

    def _write_file(self, path: str, data: str) -> None:
        """Write data to file (synchronous)."""
        # Ensure directory exists
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)

        # Write to file
        with open(path, "w", encoding="utf-8") as f:
            f.write(data)

    def _read_file(self, path: str) -> str:
        """Read data from file (synchronous)."""
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    async def create_backup(self) -> bool:
        """Create a backup of the current state file."""
        try:
            if not os.path.exists(self.file_path):
                return False

            backup_path = f"{self.file_path}.bak"
            loop = asyncio.get_running_loop()

            # Copy file to backup
            import shutil

            await loop.run_in_executor(None, lambda: shutil.copy2(self.file_path, backup_path))

            logger.info(f"Backup created at {backup_path}")
            return True
        except Exception as e:
            logger.error(f"Error creating backup: {e}")
            return False
