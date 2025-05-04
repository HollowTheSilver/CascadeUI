
# // ========================================( Modules )======================================== // #


import json
from datetime import datetime
from typing import Dict, Any, Optional


# // ========================================( Classes )======================================== // #


class StateSerializer:
    """Handles serialization and deserialization of state."""

    @staticmethod
    def serialize(state: Dict[str, Any]) -> str:
        """Serialize state to JSON string."""
        # Create a copy to avoid modifying original
        state_copy = json.loads(json.dumps(state, default=StateSerializer._json_serialize))
        return json.dumps(state_copy, indent=2)

    @staticmethod
    def deserialize(data: str) -> Dict[str, Any]:
        """Deserialize JSON string to state."""
        return json.loads(data)

    @staticmethod
    def _json_serialize(obj):
        """Handle non-serializable objects."""
        if isinstance(obj, datetime):
            return obj.isoformat()
        raise TypeError(f"Type {type(obj)} not serializable")
