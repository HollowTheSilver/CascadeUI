
# // ========================================( Modules )======================================== // #


import asyncio
from typing import Dict, Any, Optional

from .storage import StorageBackend
from .serialization import StateSerializer
from ..utils.logging import AsyncLogger

logger = AsyncLogger(name=__name__, level="DEBUG", path="logs", mode="a", prefix="cascadeui")


# // ========================================( Class )======================================== // #


class RedisBackend(StorageBackend):
    """Persist state in a local Redis instance using redis.asyncio.

    Stores the full state as a single JSON blob under a configurable key.
    Supports optional TTL for automatic expiration.

    Requires the ``redis`` package (optional dependency):
        pip install cascadeui[redis]

    Usage:
        from cascadeui.persistence import RedisBackend
        await setup_persistence(bot, backend=RedisBackend(url="redis://localhost"))
    """

    def __init__(self, url: str = "redis://localhost", key: str = "cascadeui:state",
                 ttl: Optional[int] = None):
        self.url = url
        self.key = key
        self.ttl = ttl
        self._client = None
        self._client_lock = asyncio.Lock()

    async def _get_client(self):
        """Lazily create the Redis client."""
        if self._client is None:
            async with self._client_lock:
                # Double-check after acquiring lock
                if self._client is None:
                    try:
                        import redis.asyncio as aioredis
                    except ImportError:
                        raise ImportError(
                            "redis[asyncio] is required for RedisBackend. "
                            "Install it with: pip install cascadeui[redis]"
                        )
                    self._client = aioredis.from_url(self.url)
        return self._client

    async def save_state(self, state: Dict[str, Any]) -> bool:
        """Save state to Redis."""
        try:
            client = await self._get_client()
            data = StateSerializer.serialize(state)

            if self.ttl:
                await client.setex(self.key, self.ttl, data)
            else:
                await client.set(self.key, data)

            logger.debug(f"State saved to Redis key: {self.key}")
            return True
        except Exception as e:
            logger.error(f"Error saving state to Redis: {e}")
            return False

    async def load_state(self) -> Optional[Dict[str, Any]]:
        """Load state from Redis."""
        try:
            client = await self._get_client()
            data = await client.get(self.key)

            if data is None:
                logger.info(f"No state found in Redis key: {self.key}")
                return None

            if isinstance(data, bytes):
                data = data.decode("utf-8")

            state = StateSerializer.deserialize(data)
            logger.info(f"State loaded from Redis key: {self.key}")
            return state
        except Exception as e:
            logger.error(f"Error loading state from Redis: {e}")
            return None

    async def close(self):
        """Close the Redis connection."""
        if self._client:
            try:
                await self._client.aclose()
            except AttributeError:
                # Fallback for older redis-py versions without aclose()
                await self._client.close()
            self._client = None
