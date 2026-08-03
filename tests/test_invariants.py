"""Tree-wide structural invariants.

These do not exercise behavior. They assert properties that must hold at
every site in the library, so a shape that has already produced defects
cannot reappear at a seam nobody thought to review. The export walk in
``test_public_api.py`` is the precedent: one test retires a whole class of
recurring changelog entry.

The bar is deliberately high: an invariant that cannot tell a legitimate
site from a violation trains its reader to skip failures, which costs more
than it catches. Each one below names the family it closes and the rule
that makes a site legitimate, so a future exception is judged on the rule
rather than added to a list that rots.
"""

import ast
import pathlib

LIBRARY = pathlib.Path(__file__).resolve().parent.parent / "cascadeui"


def _python_sources():
    return sorted(LIBRARY.rglob("*.py"))


def _rel(path: pathlib.Path) -> str:
    return str(path.relative_to(LIBRARY.parent)).replace("\\", "/")


# // ========================================( Discord call errors )======================================== // #


_DISCORD_CALL_TYPE_TAILS = {"HTTPException", "RateLimited"}
_CATCH_ALL_TAILS = {"Exception", "BaseException"}


def _handler_names(handler: ast.ExceptHandler) -> set:
    """Dotted names this ``except`` clause catches."""
    caught = handler.type
    if caught is None:
        return set()
    parts = caught.elts if isinstance(caught, ast.Tuple) else [caught]
    names = set()
    for part in parts:
        if isinstance(part, ast.Starred):
            part = part.value
        names.add(ast.unparse(part))
    return names


def _tail(name: str) -> str:
    """Last dotted segment of a name.

    A caught type reaches this test as whatever the source wrote, and the
    source is free to import it as ``discord.errors.HTTPException``, alias
    the module (``import discord as d``), or pull the bare name in with
    ``from discord import HTTPException`` -- the last shape already matches
    how several library modules import ``Interaction``, ``ButtonStyle``, and
    other discord.py names. A match on the full dotted string reads only the
    first of these; the tail is what survives all of them.
    """
    return name.rsplit(".", 1)[-1]


def _reads_http_field(handler: ast.ExceptHandler) -> bool:
    """Whether the handler discriminates on the caught exception's ``.code``/``.status``.

    Those fields are what an HTTP error carries and a transport error does
    not, so a handler reading one off the bound exception is deliberately
    narrow rather than accidentally so. The read must resolve to the name
    this handler bound (``except ... as e`` -> ``e.status`` or
    ``getattr(e, "status", ...)``); an unrelated ``.status`` elsewhere in
    the handler's body (a different object, an unrelated dict key sharing
    the word) does not discriminate on the exception at all and must not
    excuse the catch.
    """
    if handler.name is None:
        return False
    for node in ast.walk(handler):
        if (
            isinstance(node, ast.Attribute)
            and node.attr in {"code", "status"}
            and isinstance(node.value, ast.Name)
            and node.value.id == handler.name
        ):
            return True
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id == handler.name
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value in {"code", "status"}
        ):
            return True
    return False


def _has_catch_all(try_node: ast.Try) -> bool:
    """Whether a bare ``except Exception`` handles whatever the narrow one missed.

    Checks both a sibling handler (``except HTTPException: ... except
    Exception: ...``) and a bundled tuple (``except (HTTPException,
    Exception):``) -- the tuple form catches the same ground in one clause
    and is just as complete a backstop.
    """
    for handler in try_node.handlers:
        if handler.type is None:
            return True
        if any(_tail(n) in _CATCH_ALL_TAILS for n in _handler_names(handler)):
            return True
    return False


def _narrow_discord_handlers():
    """Every ``except`` naming a Discord HTTP type without the shared tuple."""
    offenders = []
    for path in _python_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            if _has_catch_all(node):
                continue
            for handler in node.handlers:
                names = _handler_names(handler)
                if "DISCORD_CALL_ERRORS" in names:
                    continue
                if not any(_tail(n) in _DISCORD_CALL_TYPE_TAILS for n in names):
                    continue
                if _reads_http_field(handler):
                    continue
                offenders.append(f"{_rel(path)}:{handler.lineno}")
    return offenders


class TestDiscordCallErrorsIsTheOnlyCatchTuple:
    """A Discord call fails in three sibling ways, not one.

    ``RateLimited`` is a sibling of ``HTTPException`` rather than a
    subclass, and ``aiohttp.ClientError`` is a third sibling carrying no
    HTTP status at all -- a request that never reached Discord raises from
    the transport. Each of the three has escaped a handler that named only
    the others, so the shared tuple exists to be the one place the set is
    written down.

    A narrow ``except discord.HTTPException`` stays legitimate in two
    shapes. It may read ``.code`` or ``.status``, discriminating on a field
    an HTTP error carries and a transport error does not, so widening it
    would route a connection failure into a branch built for a status code.
    Or the same ``try`` may carry a bare ``except Exception``, which already
    handles whatever the narrow clause missed. Anything else must catch
    ``DISCORD_CALL_ERRORS``.
    """

    def test_no_narrow_handler_outside_the_documented_shape(self):
        offenders = _narrow_discord_handlers()

        assert not offenders, (
            "These handlers name a Discord HTTP type without catching "
            "DISCORD_CALL_ERRORS, and without reading .code or .status to "
            "justify the narrowness. A transport failure escapes them: "
            f"{offenders}"
        )

    def test_the_shared_tuple_still_carries_all_three_siblings(self):
        import aiohttp
        import discord

        from cascadeui.utils.responses import DISCORD_CALL_ERRORS

        assert discord.HTTPException in DISCORD_CALL_ERRORS
        assert discord.RateLimited in DISCORD_CALL_ERRORS
        assert aiohttp.ClientError in DISCORD_CALL_ERRORS
        # None of the three subclasses another, which is why naming one is
        # never enough.
        assert not issubclass(discord.RateLimited, discord.HTTPException)
        assert not issubclass(aiohttp.ClientError, discord.HTTPException)
