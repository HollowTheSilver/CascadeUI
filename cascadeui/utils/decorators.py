# // ========================================( Modules )======================================== // #


import asyncio
import copy
import logging
from functools import wraps
from typing import Any, Callable, Dict, Optional

from .hooks import await_maybe

logger = logging.getLogger(__name__)


# // ========================================( Helpers )======================================== // #


_COPY_ATOMS = (str, int, float, bool, type(None))


def _copy_state(value: Any, memo: Optional[Dict[int, Any]] = None) -> Any:
    """Deep-copy JSON-shaped state without ``copy.deepcopy``'s dispatch cost.

    Store state is dicts, lists and scalars, because it round-trips through
    ``json.dumps`` on the persistence path. ``copy.deepcopy`` cannot know
    that: it consults ``__reduce_ex__`` and the copy dispatch table for every
    node. Walking the two container shapes directly, passing scalars through,
    and delegating anything else is most of the saving.

    The memo is not part of the saving and is carried anyway, because dropping
    it changes results rather than timing. ``copy.deepcopy`` keeps one to
    terminate on a self-referential structure and to preserve a reference that
    appears twice in the tree as one object on the other side; without it the
    first recurses until the stack ends and the second silently becomes two
    objects. Neither shape is reachable through the library's own writes, and
    both are reachable through a value a caller stored.

    Type tests are exact rather than ``isinstance`` so an ``IntEnum`` member,
    a ``discord.Colour``, or a ``dict`` subclass keeps its own class through
    the fallback instead of being flattened to the builtin it derives from.
    """
    kind = type(value)
    if kind in _COPY_ATOMS:
        return value
    if memo is None:
        memo = {}
    seen = memo.get(id(value))
    if seen is not None:
        return seen
    if kind is dict:
        out = {}
        memo[id(value)] = out
        for key, item in value.items():
            out[key] = item if type(item) in _COPY_ATOMS else _copy_state(item, memo)
        return out
    if kind is list:
        out = []
        memo[id(value)] = out
        for item in value:
            out.append(item if type(item) in _COPY_ATOMS else _copy_state(item, memo))
        return out
    return copy.deepcopy(value, memo)


# // ========================================( Functions )======================================== // #


def cascade_reducer(action_type: str):
    """Decorator to register a reducer function with the state store.

    The decorated function receives ``(action, state)`` where ``state`` is
    already a deep copy -- mutate it freely and return it.  Do not cache
    ``state_store.state`` references across dispatches; the reducer's
    snapshot is per-call and outside refs grow stale.

    Raises ``ValueError`` at decoration time when ``action_type`` collides
    with a built-in action (VIEW_CREATED, NAVIGATION_PUSH, UNDO, etc.).
    Reach for middleware or a store hook instead -- shadowing the built-in
    reducer would silently break sessions, navigation, and undo bookkeeping.
    """
    from ..state.reducers import _BUILTIN_REDUCER_ACTIONS

    if action_type in _BUILTIN_REDUCER_ACTIONS:
        raise ValueError(
            f"Cannot register a custom reducer for built-in action {action_type!r}. "
            f"Built-in actions drive CascadeUI's session, navigation, and undo "
            f"machinery. Use middleware (install via setup_middleware(YourMiddleware())) "
            f"for cross-cutting observation, or store.on({action_type!r}, ...) for side-effects."
        )

    def decorator(func: Callable):
        @wraps(func)
        async def wrapper(action: Dict[str, Any], state: Dict[str, Any]):
            # A reducer is a pure transformation and has no reason to be
            # async, so plenty are written with a plain ``def``. Awaiting
            # unconditionally turned those into a swallowed no-op: the
            # decoration succeeded, the dispatch succeeded, the returned
            # dict failed on being awaited, and the store logged the
            # TypeError and kept the old state. Same polarity as every
            # other seam that runs a caller's function.
            result = await await_maybe(func(action, _copy_state(state)))
            if not isinstance(result, dict):
                # The store assigns whatever comes back, so a reducer that
                # forgot its ``return`` used to install ``None`` as the whole
                # state and every later read failed somewhere else entirely.
                # Raising here is caught by the store's own reducer guard,
                # which logs and keeps the previous state, so the mistake
                # names itself and costs one dispatch instead of the session.
                raise TypeError(
                    f"Reducer {func.__name__!r} for {action_type!r} returned "
                    f"{type(result).__name__}, not a state dict. "
                    f"Fix: end {func.__name__!r} with `return state`."
                )
            return result

        # Import lazily to avoid circular imports
        from ..state.singleton import get_store

        get_store()._register_reducer(action_type, wrapper)
        logger.debug(f"Registered reducer for action type: {action_type}")

        return wrapper

    return decorator


def cascade_component(component_id: str = None):
    """Decorator to register a component callback."""

    def decorator(func: Callable):
        @wraps(func)
        async def wrapper(self, interaction):
            # Get component ID
            nonlocal component_id
            actual_id = component_id or func.__name__

            # Dispatch interaction action
            await self.dispatch(
                "COMPONENT_INTERACTION",
                {
                    "component_id": actual_id,
                    "view_id": self.id,
                    "user_id": interaction.user.id,
                    "handler": func.__name__,
                },
            )

            # Call original function
            return await await_maybe(func(self, interaction))

        return wrapper

    return decorator
