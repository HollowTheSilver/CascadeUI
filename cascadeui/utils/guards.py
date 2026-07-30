# // ========================================( Modules )======================================== // #


from typing import Any, Dict

# // ========================================( Functions )======================================== // #


def normalize_mapping(value: Any, *, owner: str, param: str) -> Dict[Any, Any]:
    """Resolve a ``{key: value}`` argument, absorbing the pair-sequence form.

    Several builders take a mapping of label to callback or label to text.
    A sequence of two-item pairs is the same data in the order the caller
    wrote it, and reads naturally enough that it is a common first guess;
    left alone it fails on ``.items()`` with an ``AttributeError`` naming a
    method rather than a parameter. The pair form is unambiguous, so it is
    converted rather than refused.

    Any mapping passes through untouched, not only ``dict``: the builders
    have always accepted one, and narrowing to a concrete type here would
    reject every ``Mapping`` subclass that works today.

    Args:
        value: The caller's argument.
        owner: Builder or class name for the error message.
        param: Parameter name for the error message.

    Returns:
        The mapping itself, or a new ``dict`` built from a pair sequence.

    Raises:
        TypeError: The value is neither a mapping nor a sequence of pairs.
    """
    if hasattr(value, "items"):
        return value
    try:
        return dict(value)
    except (TypeError, ValueError):
        raise TypeError(
            f"{owner} {param} must be a mapping of {{key: value}}, or a sequence "
            f"of (key, value) pairs; got {type(value).__name__}: {value!r}"
        ) from None
