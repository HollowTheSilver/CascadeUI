
# // ========================================( Modules )======================================== // #


from typing import Any, Callable, Optional

from .types import StateData, SelectorFn
from .singleton import get_store

_SENTINEL = object()


# // ========================================( Classes )======================================== // #


class ComputedValue:
    """A derived value that is lazily recomputed when its input changes.

    Wraps a selector (picks which slice of state to watch) and a compute
    function (transforms that slice into the derived value). On access,
    the selector output is compared to the last-seen value — if unchanged,
    the cached result is returned.
    """

    def __init__(self, name: str, selector: SelectorFn, compute_fn: Callable[[Any], Any]):
        self.name = name
        self._selector = selector
        self._compute_fn = compute_fn
        self._last_input: Any = _SENTINEL
        self._cached: Any = None

    def get(self, state: StateData) -> Any:
        """Get the computed value, recomputing only if the selector output changed."""
        current_input = self._selector(state)
        if self._last_input is not _SENTINEL and current_input == self._last_input:
            return self._cached
        self._last_input = current_input
        self._cached = self._compute_fn(current_input)
        return self._cached

    def invalidate(self):
        """Force recomputation on next access."""
        self._last_input = _SENTINEL


# // ========================================( Decorator )======================================== // #


def computed(selector: SelectorFn):
    """Decorator to register a computed value on the global store.

    Usage:
        @computed(selector=lambda s: s.get("application", {}).get("votes", {}))
        def total_votes(votes):
            return sum(votes.values())

        # Access:
        result = store.computed["total_votes"]
    """
    def decorator(fn: Callable[[Any], Any]):
        cv = ComputedValue(name=fn.__name__, selector=selector, compute_fn=fn)
        store = get_store()
        store.register_computed(fn.__name__, cv)
        return fn
    return decorator
