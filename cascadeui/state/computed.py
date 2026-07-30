# // ========================================( Modules )======================================== // #


import copy
from typing import Any, Callable, Dict, Optional, Tuple

from ..utils.hooks import is_async_callable
from .singleton import get_store
from .types import SelectorFn, StateData

_SENTINEL = object()

# Module-level registry of @computed-decorated functions. Populated at decoration
# time (module import) so registrations survive store resets: every fresh
# StateStore seeds its own _computed dict from this registry during __init__.
# Keyed by function name -> (selector, compute_fn). ComputedValue instances are
# created per-store (cache is per-store), but the recipe lives here.
_COMPUTED_REGISTRY: Dict[str, Tuple[SelectorFn, Callable[[Any], Any]]] = {}


# // ========================================( Classes )======================================== // #


class ComputedValue:
    """A derived value that is lazily recomputed when its input changes.

    Wraps a selector (picks which slice of state to watch) and a compute
    function (transforms that slice into the derived value). On access,
    the selector output is compared to the last-seen value -- if unchanged,
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
        # Keep an independent copy of the input, not a reference to it. A
        # selector returns a slice of live state, and a slice mutated in
        # place carries the stored reference with it, so the comparison
        # above becomes the slice against itself and the stale result is
        # returned forever -- including after a later, correct replacement,
        # because the aliased reference already matches the new value while
        # the cached result predates it. ``access_slot`` mutates in place by
        # design, and ``seed_initial_state`` hands it live state, so the
        # aliasing is reachable through a sanctioned path. Copying costs
        # only on a miss, where the recompute is happening anyway.
        try:
            self._last_input = copy.deepcopy(current_input)
        except Exception:
            # Something in the slice will not copy (a live client object, an
            # open handle). Recompute every time rather than trust a
            # reference this cannot verify.
            self._last_input = _SENTINEL
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

    if not callable(selector):
        raise TypeError(
            f"@computed selector= must be a callable taking the state and returning "
            f"the slice to watch, e.g. lambda s: s['application']['votes']; got "
            f"{type(selector).__name__}: {selector!r}"
        )
    # The selector runs inside the memo comparison, which is synchronous and
    # cannot await. An async one hands back a coroutine that the compute
    # function then tries to use as data, failing somewhere downstream with
    # a message naming neither this decorator nor the argument.
    if is_async_callable(selector):
        raise TypeError(
            "@computed selector= must be synchronous; it runs inside the cache "
            "comparison, which cannot await it."
            "\n  Fix: read the slice from the state argument and return it directly. "
            "A computed value derives from state already in memory."
        )

    def decorator(fn: Callable[[Any], Any]):
        _COMPUTED_REGISTRY[fn.__name__] = (selector, fn)
        cv = ComputedValue(name=fn.__name__, selector=selector, compute_fn=fn)
        store = get_store()
        store._register_computed(fn.__name__, cv)
        return fn

    return decorator
