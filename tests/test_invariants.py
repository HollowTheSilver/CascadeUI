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


# // ========================================( Collected view exits )======================================== // #


def _exits_outside_the_helper():
    """``X.exit(...)`` calls whose receiver is neither ``self`` nor ``super()``."""
    offenders = []
    for path in _python_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if func.name == "_exit_or_successor":
                continue
            for node in ast.walk(func):
                if not (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "exit"
                ):
                    continue
                receiver = node.func.value
                if isinstance(receiver, ast.Name) and receiver.id == "self":
                    continue
                if (
                    isinstance(receiver, ast.Call)
                    and isinstance(receiver.func, ast.Name)
                    and receiver.func.id == "super"
                ):
                    continue
                offenders.append(f"{_rel(path)}:{node.lineno}")
    return offenders


class TestCollectedViewsExitThroughTheHelper:
    """A view the library collected can hand its place on before it is closed.

    ``exit()`` on a view that navigated away with ``push()`` or ``pop()``
    does nothing, since a caller holding that view means it. Library code
    that closes a view it found in a registry, a snapshot, or an attachment
    list means the panel, so it calls ``_exit_or_successor``, which exits
    whichever view holds the place once any navigation in flight settles. A
    view closing itself (``self.exit()``, ``super().exit()``) is the other
    legitimate shape. Each site this rule found closed a panel's former view
    and left the one on screen live.
    """

    def test_no_exit_on_a_collected_view_bypasses_the_helper(self):
        offenders = _exits_outside_the_helper()

        assert not offenders, (
            "These call exit() on a view other than self, so a push or pop that "
            "lands while they run leaves the view on screen live. Call "
            f"_exit_or_successor() instead: {offenders}"
        )


def _wait_for_calls(source: str) -> list:
    """Line numbers of calls to ``asyncio.wait_for`` in ``source``, however imported.

    Resolved through the module's imports, so ``import asyncio as aio`` and
    ``from asyncio import wait_for as bounded`` are found, while a method that
    happens to share the name (discord.py's ``bot.wait_for("message")``) is not.
    """
    tree = ast.parse(source)
    modules, submodules, functions = set(), set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "asyncio":
                    modules.add(alias.asname or "asyncio")
                elif alias.name == "asyncio.tasks":
                    if alias.asname:
                        submodules.add(alias.asname)
                    else:
                        modules.add("asyncio")
        elif isinstance(node, ast.ImportFrom) and node.module in ("asyncio", "asyncio.tasks"):
            for alias in node.names:
                if alias.name == "wait_for":
                    functions.add(alias.asname or "wait_for")
                elif alias.name == "tasks" and node.module == "asyncio":
                    submodules.add(alias.asname or "tasks")
    lines = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in functions:
            lines.append(node.lineno)
        elif isinstance(func, ast.Attribute) and func.attr == "wait_for":
            owner = func.value
            if isinstance(owner, ast.Name) and owner.id in submodules:
                lines.append(node.lineno)
                continue
            if isinstance(owner, ast.Attribute) and owner.attr == "tasks":
                owner = owner.value
            if isinstance(owner, ast.Name) and owner.id in modules:
                lines.append(node.lineno)
    return lines


class TestTheLibraryBoundsWithoutWaitFor:
    """``asyncio.wait_for`` on 3.10 and 3.11 returns its result when the caller
    is cancelled as the work finishes, and the cancel is lost: a teardown that
    cancelled a view's render found it still running. The library bounds its
    awaits with ``_bounded_wait`` in ``utils/tasks.py`` instead."""

    def test_no_library_module_calls_asyncio_wait_for(self):
        offenders = [
            f"{_rel(path)}:{line}"
            for path in _python_sources()
            for line in _wait_for_calls(path.read_text(encoding="utf-8"))
        ]

        assert not offenders, (
            "These call asyncio.wait_for, which loses a cancel on Python 3.10 and "
            f"3.11. Use _bounded_wait from cascadeui.utils.tasks: {offenders}"
        )

    def test_the_check_resolves_imports(self):
        found = _wait_for_calls(
            "import asyncio\n"
            "import asyncio as aio\n"
            "import asyncio.tasks as t\n"
            "from asyncio import wait_for as bounded\n"
            "from asyncio import tasks\n"
            "async def f(bot, x):\n"
            "    await asyncio.wait_for(x, 1)\n"
            "    await aio.wait_for(x, 1)\n"
            "    await asyncio.tasks.wait_for(x, 1)\n"
            "    await bounded(x, 1)\n"
            "    await t.wait_for(x, 1)\n"
            "    await tasks.wait_for(x, 1)\n"
            "    await bot.wait_for('message')\n"
            "    await x.wait_for(1)\n"
        )

        assert found == [7, 8, 9, 10, 11, 12]
