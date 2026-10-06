"""Randomized interleavings of everything that can touch a view mid-navigation.

Each seed drives clicks through ``_scheduled_task``, programmatic pushes and
pops, exits, timeouts, refreshes, reloads, and state dispatches against a
small navigation graph, with every navigation edit landing, rate limited, or
stalling past its bound. Once every task has finished, the invariants the
navigation transaction exists to keep are checked: nothing left mid-navigation,
no stopped view still registered, one live owner per message and the message
showing it, no operation hung, and an attached child either closed with its
parent chain or attached to the view that owns the message.

Single-path tests pin one ordering each; these reach orderings nobody wrote
down. A failure names its seed, and the run is deterministic for that seed on
one machine. ``CASCADEUI_INTERLEAVING_SEEDS`` raises the seed count for a
deeper run.
"""

# // ========================================( Modules )======================================== // #


import asyncio
import os
import random
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord.ui import ActionRow
from helpers import RenderableLayoutView, make_interaction

from cascadeui.components.base import StatefulButton

# // ========================================( Harness )======================================== // #


_SEEDS = int(os.environ.get("CASCADEUI_INTERLEAVING_SEEDS", "40"))

# Refusals a racing operation is expected to meet: a push or pop from a view
# that already moved on or closed, or that a failed push or pop returned.
_EXPECTED_REFUSALS = ("handed its panel on", "has closed", "failed push() or pop() returned")


class _Run:
    """The state one seed's run shares with the views it drives."""

    rng = random.Random(0)
    edits = []


async def _ticks(n):
    for _ in range(n):
        await asyncio.sleep(0)


async def _quiesce(limit=10.0):
    """Wait until every task but this one has finished; False if some never do."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + limit
    current = asyncio.current_task()
    while True:
        others = [t for t in asyncio.all_tasks() if t is not current and not t.done()]
        if not others:
            return True
        remaining = deadline - loop.time()
        if remaining <= 0:
            return False
        await asyncio.wait(others, timeout=remaining)


def _navigation_interaction():
    """A click whose navigation edit lands, is rate limited, or stalls."""
    roll = _Run.rng.random()
    kind = "stall" if roll < 0.12 else ("fail" if roll < 0.35 else "ok")
    nav = make_interaction(user_id=1, guild_id=100, is_done=(kind == "stall"))

    async def fast(**kwargs):
        await _ticks(_Run.rng.randint(0, 3))
        if kind == "fail":
            raise discord.RateLimited(0.05)
        _Run.edits.append(kwargs["view"])
        nav.response.is_done.return_value = True

    async def original(**kwargs):
        if kind == "stall":
            # The request left: Discord may have applied it.
            _Run.edits.append(kwargs["view"])
            await asyncio.sleep(5)
        await _ticks(_Run.rng.randint(0, 2))
        _Run.edits.append(kwargs["view"])
        return MagicMock()

    nav.response.edit_message = AsyncMock(side_effect=fast)
    nav.edit_original_response = AsyncMock(side_effect=original)
    return nav


class _Node(RenderableLayoutView):
    subscribed_actions = None
    edit_timeout = 0.03

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        view = self

        async def go(interaction):
            await view.push(_Run.rng.choice([_Left, _Right]), interaction=interaction)

        async def back(interaction):
            await view.pop(interaction=interaction)

        self.go = StatefulButton(label="go", custom_id="go", callback=go)
        self.back = StatefulButton(label="back", custom_id="back", callback=back)
        self.add_item(ActionRow(self.go, self.back))

    async def on_load(self):
        await _ticks(_Run.rng.randint(0, 3))


class _Root(_Node):
    pass


class _Left(_Node):
    serialize_interactions = False


class _Right(_Node):
    pass


class _Kid(RenderableLayoutView):
    pass


def _reachable(views):
    """``views`` and every view their pushes and pops handed a message to."""
    found = set(views)
    for view in list(found):
        successor = view._successor
        while successor is not None:
            found.add(successor)
            successor = successor._successor
    return found


async def _run(seed):
    _Run.rng = random.Random(seed)
    _Run.edits = []
    rng = _Run.rng

    root = _Root(interaction=make_interaction(user_id=1, guild_id=100))
    await root.send()
    message = root._message

    async def channel_edit(**kwargs):
        await _ticks(rng.randint(0, 2))
        _Run.edits.append(kwargs.get("view"))
        return message

    message.edit = AsyncMock(side_effect=channel_edit)
    store = root.state_store
    kid = _Kid(interaction=make_interaction(user_id=1, guild_id=100), parent=root)
    await kid.send()
    views = [root]
    errors = []

    def track(view):
        if view is not None and view not in views:
            views.append(view)

    async def refused(call, name):
        try:
            track(await call)
        except RuntimeError as e:
            if not any(expected in str(e) for expected in _EXPECTED_REFUSALS):
                errors.append((name, repr(e)))

    async def click(view):
        await _ticks(rng.randint(0, 4))
        item = view.go if rng.random() < 0.6 else view.back
        await view._scheduled_task(item, _navigation_interaction())

    async def push(view):
        await _ticks(rng.randint(0, 4))
        destination = rng.choice([_Left, _Right])
        await refused(view.push(destination, interaction=_navigation_interaction()), "push")

    async def pop(view):
        await _ticks(rng.randint(0, 4))
        await refused(view.pop(interaction=_navigation_interaction()), "pop")

    async def close(view):
        await _ticks(rng.randint(0, 8))
        await view.exit()

    async def refresh(view):
        await _ticks(rng.randint(0, 8))
        await view.refresh()

    async def reload(view):
        await _ticks(rng.randint(0, 8))
        await view.reload()

    async def dispatch(view):
        await _ticks(rng.randint(0, 8))
        await store.dispatch("INTERLEAVING_TICK", {"n": rng.random()})

    async def expire(view):
        await _ticks(rng.randint(0, 8))
        if not view.is_finished():
            view._dispatch_timeout()

    operations = [reload, click, click, click, push, pop, refresh, dispatch, close, expire]
    tasks = []
    for _ in range(rng.randint(4, 10)):
        target = rng.choice(sorted(_reachable(views), key=lambda v: v.id))
        tasks.append(asyncio.create_task(rng.choice(operations)(target)))
        await _ticks(rng.randint(0, 5))

    if not await _quiesce():
        errors.append(("hung", [t for t in tasks if not t.done()]))
    for task in tasks:
        if task.done() and not task.cancelled() and task.exception() is not None:
            errors.append(("raised", repr(task.exception())))

    everyone = _reachable(views)
    everyone.update(v for v in store._active_views.values() if v._message is message)
    for view in everyone:
        if view._away_for_navigation or view._arriving_from is not None:
            errors.append(("left mid-navigation", type(view).__name__))
        if not view._navigation_settled.is_set():
            errors.append(("navigation never settled", type(view).__name__))
        if view.is_finished() and not view._torn_down() and view.id in store._active_views:
            errors.append(("stopped but registered", type(view).__name__))

    owners = [v for v in everyone if not v.is_finished() and v._message is message]
    if len(owners) > 1:
        errors.append(("two live owners", [type(v).__name__ for v in owners]))
    if owners:
        owner = owners[0]
        shown = _Run.edits[-1] if _Run.edits else root
        if shown is not owner:
            errors.append(("message shows", type(shown).__name__, "owner", type(owner).__name__))
        if not kid._torn_down() and kid._attached_to is not owner:
            errors.append(("child attached to", type(kid._attached_to).__name__))
        try:
            await asyncio.wait_for(owner.exit(), 5.0)
        except asyncio.TimeoutError:
            errors.append(("owner exit hung",))
    elif not kid._torn_down():
        errors.append(("child outlived its parent chain",))

    for task in tasks:
        task.cancel()
    return errors


# // ========================================( Tests )======================================== // #


class TestNavigationInterleavings:
    """Every interleaving keeps one owner per message, settled and shown."""

    @pytest.mark.parametrize("seed", range(_SEEDS))
    async def test_seed(self, seed):
        errors = await _run(seed)

        assert errors == [], f"seed {seed}: {errors}"
