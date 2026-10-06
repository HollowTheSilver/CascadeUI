# // ========================================( Modules )======================================== // #


import functools
import types
from typing import Any

# // ========================================( Constants )======================================== // #


# Stand-ins inside a signature: a method bound to the item itself, a closure
# cell that has not been assigned yet, and a callable reached again while it
# is still being read (a callback that calls itself captures itself).
_SELF = ("self",)
_EMPTY_CELL = ("empty",)
_CYCLE = ("cycle",)

# Attributes a stateful component sets to its own dispatch plumbing.
_CALLBACK_ATTRS = frozenset({"callback", "original_callback"})

_CALLABLES = (functools.partial, types.MethodType, types.FunctionType)


# // ========================================( Classes )======================================== // #


class _Equivalent:
    """A captured value that compares equal to another exactly when the values do.

    Two values of different types never match, so ``1`` and ``True`` stay
    apart even though they compare equal. A value with no equality of its
    own compares by identity, which is Python's default.
    """

    __slots__ = ("value", "_hash")

    def __init__(self, value: Any):
        self.value = value
        try:
            self._hash = hash(value)
        except TypeError:
            # Unhashable values share their type's bucket; ``==`` tells them apart.
            self._hash = hash(type(value))

    def __hash__(self) -> int:
        return self._hash

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, _Equivalent):
            return NotImplemented
        a, b = self.value, other.value
        if a is b:
            return True
        if type(a) is not type(b):
            return False
        try:
            return bool(a == b)
        except Exception:
            # An array-like ``==`` answers elementwise, and its truth value raises.
            return False


# // ========================================( Functions )======================================== // #


def _signature_of(value: Any, item: Any, reading: tuple) -> Any:
    if value is item:
        return _SELF
    if not isinstance(value, _CALLABLES):
        return _Equivalent(value)
    if any(value is seen for seen in reading):
        return _CYCLE
    reading = (*reading, value)
    if isinstance(value, functools.partial):
        return (
            "partial",
            _signature_of(value.func, item, reading),
            tuple(_signature_of(arg, item, reading) for arg in value.args),
            tuple(sorted((k, _signature_of(v, item, reading)) for k, v in value.keywords.items())),
        )
    if isinstance(value, types.MethodType):
        return (
            "method",
            _signature_of(value.__func__, item, reading),
            _signature_of(value.__self__, item, reading),
        )
    cells = []
    for cell in value.__closure__ or ():
        try:
            contents = cell.cell_contents
        except ValueError:
            cells.append(_EMPTY_CELL)
            continue
        cells.append(_signature_of(contents, item, reading))
    return (
        "function",
        value.__code__,
        tuple(cells),
        tuple(_signature_of(d, item, reading) for d in value.__defaults__ or ()),
        tuple(
            sorted(
                (k, _signature_of(v, item, reading))
                for k, v in (value.__kwdefaults__ or {}).items()
            )
        ),
    )


def item_signature(item: Any) -> tuple:
    """What a click on ``item`` would do, as a value two renders can compare.

    Built from the item's class, its callback (the function's code, what
    its closure captured, its default arguments, a ``partial``'s bound
    arguments), and its own public attributes, which is where a
    ``discord.ui.Button`` subclass keeps the row it acts on. Two items
    whose signatures are equal would do the same thing when clicked.
    """
    state = getattr(item, "__dict__", {})
    return (
        type(item),
        _signature_of(getattr(item, "original_callback", None), item, ()),
        tuple(
            sorted(
                (name, _signature_of(value, item, ()))
                for name, value in state.items()
                if not name.startswith("_") and name not in _CALLBACK_ATTRS
            )
        ),
    )
