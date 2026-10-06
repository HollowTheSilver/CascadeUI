"""Randomized interleavings of renders with sends, checked against a reference.

A render another task makes while its view is being sent is held and replayed
after the send, a state change deferred then is rendered by that replay, and a
click whose one-request edit stalled is sent again once the click is answered.
Each seed drives refreshes, state changes, stalled clicks, reloads, and sends
that post or are vetoed, released through gates, then compares the message
the view ends on with what the same calls leave when nothing is deferred.

The reference: the post of the last send that posted, then every call made
after that send began, in the order the calls were made. Calls made before it
belong to the message it left. A V2 view's tree comes from its state, so the
post shows the state as it stood when the post went out, and a ``refresh()``
with no keywords made before that point, with no call that carried keywords
between the send's start and it, is already in the post. A V2 edit parses
mentions from the whole tree under its own rules (Discord's Edit Message
reference), and a ``refresh()`` with no keywords edits nothing when the tree
is unchanged.

``CASCADEUI_RENDER_SEEDS`` raises the seed count for a deeper run.
"""

# // ========================================( Modules )======================================== // #


import asyncio
import io
import logging
import os
import random
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from helpers import make_interaction, until

from cascadeui.components.base import StatefulButton
from cascadeui.views.layout import StatefulLayoutView
from cascadeui.views.view import StatefulView

# // ========================================( Harness )======================================== // #


_SEEDS = int(os.environ.get("CASCADEUI_RENDER_SEEDS", "40"))

DEFAULT = discord.AllowedMentions.none()
RULES = {
    "users": discord.AllowedMentions(everyone=False, users=True, roles=False, replied_user=False),
    "roles": discord.AllowedMentions(everyone=False, users=False, roles=True, replied_user=False),
}
_RULE_NAMES = {repr(DEFAULT.to_dict()): "default"} | {
    repr(rules.to_dict()): name for name, rules in RULES.items()
}


def _rule_name(rules):
    return None if rules is None else _RULE_NAMES.get(repr(rules.to_dict()))


def _kwargs(spec):
    """Edit keywords from a short spec: ``{"embed": "H", "rules": "users"}``."""
    kwargs = {}
    for key, value in spec.items():
        if key == "embed":
            kwargs["embed"] = discord.Embed(title=value)
        elif key == "embeds":
            kwargs["embeds"] = [discord.Embed(title=value)]
        elif key == "attachments":
            kwargs["attachments"] = [discord.File(io.BytesIO(b"x"), filename=value)]
        elif key == "rules":
            kwargs["allowed_mentions"] = RULES[value]
        else:
            kwargs[key] = value
    return kwargs


def _write(view, kwargs):
    """One post or edit as the message received it."""
    write = {"rules": _rule_name(kwargs.get("allowed_mentions"))}
    if "content" in kwargs:
        write["content"] = kwargs["content"]
    if kwargs.get("embed") is not None:
        write["embeds"] = [kwargs["embed"].title]
    if "embeds" in kwargs:
        write["embeds"] = [e.title for e in kwargs["embeds"]]
    if "attachments" in kwargs:
        write["attachments"] = [f.filename for f in kwargs["attachments"]]
    if isinstance(view, StatefulLayoutView):
        write["tree"] = view.text.content
    return write


def _apply(shown, write, *, layout):
    """Fold one post or edit into what the message shows."""
    for part in ("content", "embeds", "attachments", "tree"):
        if part in write:
            shown[part] = write[part]
    # A V1 edit parses mentions from its content, a V2 edit from the tree.
    if layout or "content" in write:
        shown["rules"] = write["rules"]


def _shown(message, *, layout):
    shown = {}
    for write in message.writes:
        _apply(shown, write, layout=layout)
    return shown


def _message(ident, view, kwargs):
    message = MagicMock(id=ident, channel=MagicMock(id=888))
    message.writes = [_write(view, kwargs)]

    async def edit(**kwargs):
        message.writes.append(_write(kwargs["view"], kwargs))
        return message

    message.edit = AsyncMock(side_effect=edit)
    message.delete = AsyncMock()
    return message


class _Gate:
    def __init__(self, stage, answer=True):
        self.stage, self.answer = stage, answer
        self.reached, self.release = asyncio.Event(), asyncio.Event()


class _Gated:
    """Sends wait at a gate the run opens; a reload waits at its own."""

    # Long enough that a stalled click ends when the run says so.
    auto_defer_delay = 2.9
    allowed_mentions = DEFAULT

    def _setup(self):
        self.gates = {}
        self.click_kwargs = {}
        self.click_text = None

    async def _gate(self, stage):
        gate = self.gates.get(asyncio.current_task())
        if gate is not None and gate.stage == stage:
            gate.reached.set()
            await gate.release.wait()
            return gate.answer
        return True

    async def on_pre_send(self, interaction):
        return await self._gate("presend")

    async def on_load(self):
        await self._gate("load")

    async def _click(self, interaction):
        if self.click_text is not None:
            self.text.content = self.state_text = self.click_text
        await self.refresh(**self.click_kwargs)


class _V1(_Gated, StatefulView):
    """Renders its embed from state once a state change has set one."""

    state_title = None

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._setup()
        self.button = StatefulButton(label="+1", custom_id="render:click", callback=self._click)
        self.add_item(self.button)

    async def on_state_changed(self, state):
        if self.state_title is None:
            await super().on_state_changed(state)
        else:
            await self.refresh(embed=discord.Embed(title=self.state_title))


class _V2(_Gated, StatefulLayoutView):
    """Renders its text from state; every change of the text goes through it."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._setup()
        self.state_text = "t0"
        self.text = discord.ui.TextDisplay("t0")
        self.button = StatefulButton(label="+1", custom_id="render:click", callback=self._click)
        self.add_item(self.text)
        self.add_item(discord.ui.ActionRow(self.button))

    async def on_state_changed(self, state):
        self.text.content = self.state_text
        await self.refresh()


class _Run:
    """One seeded run: operations on a view, and the calls they made, in order."""

    def __init__(self, view_cls, rng):
        self.rng, self.layout = rng, view_cls is _V2
        self.n = 0
        # Numbers every call and send start, in the order they were made.
        self.origin = 0
        self.log = []
        # (number, the edit the call asks for, whether it carried no keywords)
        self.calls = []
        # [number of the send's start, the message it posted or None]
        self.sends = []
        context = MagicMock()
        context.author = MagicMock(id=1)
        context.guild = MagicMock(id=100)
        context.send = AsyncMock(side_effect=self._post)
        self.view = view_cls(context=context)
        self.sending = None
        self.queue = []
        self.click = self.stall = self.clicking = None
        self.reload = self.reloading = None

    def _next(self):
        self.n += 1
        return self.n

    async def _post(self, **kwargs):
        message = _message(1000 + self._next(), self.view, kwargs)
        message.state_text = getattr(self.view, "state_text", None)
        message.posted_at = self.origin
        await self.view._gate("post")
        return message

    # Calls

    def _rules(self):
        return None if self.rng.random() < 0.5 else self.rng.choice(sorted(RULES))

    def _call_kwargs(self, *, bare_ok):
        n, kwargs = self._next(), {}
        if self.layout:
            parts = ["attachments"] if self.rng.random() < 0.4 else []
        else:
            parts = [
                part
                for part in ("content", "embed", "embeds", "attachments")
                if self.rng.random() < 0.35
            ]
            if "embed" in parts and "embeds" in parts:
                parts.remove(self.rng.choice(["embed", "embeds"]))
        for part in parts:
            if part == "content":
                kwargs["content"] = f"c{n}"
            elif part == "embed":
                kwargs["embed"] = discord.Embed(title=f"e{n}")
            elif part == "embeds":
                kwargs["embeds"] = [discord.Embed(title=f"e{n}")]
            else:
                kwargs["attachments"] = [discord.File(io.BytesIO(b"x"), filename=f"a{n}.txt")]
        rules = self._rules()
        if rules is not None:
            kwargs["allowed_mentions"] = RULES[rules]
        if not bare_ok and not self.layout and not any(k != "allowed_mentions" for k in kwargs):
            # A V1 click with nothing to send is skipped before its edit.
            kwargs["embed"] = discord.Embed(title=f"e{n}")
        return kwargs

    def _record(self, kwargs, tree=None):
        self.origin += 1
        write = _write(None, kwargs)
        if write["rules"] is None:
            write["rules"] = "default"
        if self.layout:
            write["tree"] = tree if tree is not None else self.view.state_text
        self.calls.append((self.origin, write, not kwargs))
        return write

    async def refresh(self, spec=None, tree=None):
        kwargs = self._call_kwargs(bare_ok=True) if spec is None else _kwargs(spec)
        if spec is None and self.layout and self.rng.random() < 0.5:
            tree = f"t{self._next()}"
        if tree is not None:
            self.view.text.content = self.view.state_text = tree
        self.log.append(("refresh", self._record(kwargs)))
        await self.view.refresh(**kwargs)

    async def state(self):
        """A state change: the view renders from state, now or after the send."""
        n = self._next()
        if self.layout:
            self.view.state_text = f"s{n}"
            write = self._record({}, self.view.state_text)
        else:
            self.view.state_title = f"st{n}"
            write = self._record({"embed": discord.Embed(title=self.view.state_title)})
        self.log.append(("state", write))
        await self.view._render_from_state()

    async def click_begin(self, spec=None, tree=None):
        kwargs = self._call_kwargs(bare_ok=False) if spec is None else _kwargs(spec)
        if self.layout:
            tree = self.view.click_text = tree or f"t{self._next()}"
        self.view.click_kwargs = kwargs
        self.log.append(("click", self._record(kwargs, tree)))
        self.stall = asyncio.Event()

        async def stuck(**kwargs):
            await self.stall.wait()
            raise asyncio.TimeoutError()

        self.click = make_interaction(user_id=1, message=self.view.message)
        self.click.response.edit_message = AsyncMock(side_effect=stuck)
        self.clicking = asyncio.create_task(self.view._scheduled_task(self.view.button, self.click))
        await until(lambda: self.click.response.edit_message.await_count == 1, timeout=2)

    async def click_end(self):
        self.log.append(("click released",))
        self.stall.set()
        await asyncio.wait_for(self.clicking, 5)
        self.click = self.stall = self.clicking = None

    # Sends

    def _send_kwargs(self, rules=None):
        n, kwargs = self._next(), {}
        if not self.layout:
            kwargs["embed"] = discord.Embed(title=f"s{n}")
            if self.rng.random() < 0.3:
                kwargs["content"] = f"sc{n}"
        rules = rules or self._rules()
        if rules is not None:
            kwargs["allowed_mentions"] = RULES[rules]
        return kwargs

    async def _sends(self, gates):
        results = []
        for gate, kwargs in gates:
            self.view.gates[asyncio.current_task()] = gate
            results.append(await self.view.send(**kwargs))
        return results

    async def send_begin(self, kinds=None, rules=None):
        if kinds is None:
            kinds = [self.rng.choice(["veto", "load", "post"])]
            if kinds[0] == "veto" and self.rng.random() < 0.5:
                # One task sending again right after a veto.
                kinds.append(self.rng.choice(["veto", "load", "post"]))
        self.queue = [
            (_Gate("presend", False) if kind == "veto" else _Gate(kind), self._send_kwargs(rules))
            for kind in kinds
        ]
        self.log.append(("send", kinds))
        self.sending = asyncio.create_task(self._sends(list(self.queue)))
        await self._reach_next_gate()

    async def _reach_next_gate(self):
        gate, _ = self.queue[0]
        self.origin += 1
        self.sends.append([self.origin, None])
        await until(lambda: gate.reached.is_set(), timeout=2)

    async def send_release(self):
        gate, _ = self.queue.pop(0)
        self.log.append(("send released", gate.stage))
        gate.release.set()
        if self.queue:
            await self._reach_next_gate()
            return
        results = await asyncio.wait_for(self.sending, 5)
        for record, result in zip(self.sends[-len(results) :], results):
            record[1] = result
        self.sending = None

    # A reload takes the view's turn. Started only while a send posts, so the
    # replay after the send waits for it.

    async def reload_begin(self):
        self.log.append(("reload",))
        self.reload = _Gate("load")
        claims = self.view._turn_claims

        async def run():
            self.view.gates[asyncio.current_task()] = self.reload
            return await self.view.reload()

        self.reloading = asyncio.create_task(run())
        await until(lambda: self.view._turn_claims > claims, timeout=2)

    async def reload_end(self):
        await until(lambda: self.reload.reached.is_set(), timeout=2)
        self.log.append(("reload released",))
        self.origin += 1
        if self.layout:
            self.calls.append(
                (self.origin, {"rules": "default", "tree": self.view.state_text}, True)
            )
        self.reload.release.set()
        await asyncio.wait_for(self.reloading, 5)
        self.reload = self.reloading = None

    # Driving

    def _choices(self):
        choices = [self.refresh, self.refresh, self.state]
        if self.queue:
            choices += [self.send_release, self.send_release]
            if self.queue[0][0].stage == "post" and self.reload is None:
                choices.append(self.reload_begin)
        else:
            if self.reload is None:
                choices.append(self.send_begin)
            elif self.reload.reached.is_set():
                choices.append(self.reload_end)
            if self.click is None:
                choices.append(self.click_begin)
        if self.click is not None:
            choices.append(self.click_end)
        return choices

    async def drive(self, steps=0, script=()):
        await self.view.send(**self._send_kwargs())
        self.sends.append([0, self.view.message])
        for _ in range(steps):
            await self.rng.choice(self._choices())()
        for name, *args in script:
            await getattr(self, name)(*args)
        while self.queue:
            await self.send_release()
        if self.click is not None:
            await self.click_end()
        if self.reload is not None:
            await self.reload_end()
        for _ in range(3):
            await asyncio.wait_for(self.view.task_manager.wait_tasks(self.view.id), 5)
            await self.view.state_store._flush_notifications()
            for _ in range(20):
                await asyncio.sleep(0)

    def expected(self):
        """The message the view should end on, and what it should show."""
        start, message = next((s, m) for s, m in reversed(self.sends) if m is not None)
        shown = {}
        _apply(shown, message.writes[0], layout=self.layout)
        if self.layout:
            shown["tree"] = message.state_text
        keywords_seen = False
        for origin, write, bare in self.calls:
            if origin < start:
                continue
            if self.layout and bare and not keywords_seen and origin <= message.posted_at:
                continue
            keywords_seen = keywords_seen or not bare
            if self.layout and bare and write["tree"] == shown.get("tree"):
                continue
            _apply(shown, write, layout=self.layout)
        return message, shown

    def check(self, caplog):
        message, expected = self.expected()
        assert self.view.message is message, f"the view ended on another message\n{self.log}"
        actual = _shown(message, layout=self.layout)
        assert actual == expected, (
            f"the message shows the wrong thing\n  actual:   {actual}\n  expected: {expected}\n"
            f"  calls: {self.log}\n  writes: {message.writes}"
        )
        logged = [r.getMessage() for r in caplog.records if r.name.startswith("cascadeui")]
        assert not logged, logged


# Shapes found by review, kept as scripts so the reference is known to see them.
_SCRIPTS = {
    # A stalled click's older embed covered a newer one held during a send.
    "stall_sent_again_over_a_newer_hold": (
        _V1,
        [
            ("click_begin", {"embed": "C"}),
            ("send_begin", ["veto", "veto"]),
            ("refresh", {"embed": "H"}),
            ("send_release",),
            ("click_end",),
            ("send_release",),
        ],
    ),
    # Held content the replay sent took a number above a click that stalled
    # meanwhile, and the click's newer content was never sent.
    "replay_over_a_newer_stall_v1": (
        _V1,
        [
            ("send_begin", ["post"]),
            ("refresh", {"embed": "old"}),
            ("reload_begin",),
            ("send_release",),
            ("click_begin", {"embed": "new"}),
            ("reload_end",),
            ("click_end",),
        ],
    ),
    "replay_over_a_newer_stall_v2": (
        _V2,
        [
            ("send_begin", ["post"]),
            ("refresh", {"attachments": "old.txt"}),
            ("reload_begin",),
            ("send_release",),
            ("click_begin", {"attachments": "new.txt", "rules": "users"}, "t-click"),
            ("reload_end",),
            ("click_end",),
        ],
    ),
    # Mention rules passed with nothing else during a send.
    "rules_alone_during_a_send": (
        _V2,
        [("send_begin", ["load"]), ("refresh", {"rules": "users"}), ("send_release",)],
    ),
    # A later refresh() that changed the tree passed no rules.
    "rules_then_a_changed_tree": (
        _V2,
        [
            ("send_begin", ["load"]),
            ("refresh", {"attachments": "a.txt", "rules": "users"}),
            ("refresh", {}, "t-later"),
            ("send_release",),
        ],
    ),
    # A state change made before the post went out is in the post, under its
    # rules; replaying it after the post put the tree under the view's own.
    "a_state_change_before_the_post_is_in_it": (
        _V2,
        [("send_begin", ["load"], "users"), ("state",), ("send_release",)],
    ),
    # A stalled render sent again while a send runs is held with it: kept on the
    # message a vetoed send leaves the view on, replaced by a post, and its rules
    # never replace those of a newer call held beside it.
    "stall_sent_again_during_a_vetoed_send": (
        _V2,
        [
            ("click_begin", {"attachments": "a.txt", "rules": "users"}, "t-click"),
            ("send_begin", ["veto"]),
            ("click_end",),
            ("send_release",),
        ],
    ),
    "stall_sent_again_after_a_newer_held_call": (
        _V2,
        [
            ("click_begin", {"attachments": "a.txt", "rules": "users"}, "t-click"),
            ("send_begin", ["load"]),
            ("refresh", {"attachments": "b.txt", "rules": "roles"}),
            ("click_end",),
            ("send_release",),
        ],
    ),
    "stall_sent_again_during_a_send_that_posts": (
        _V2,
        [
            ("click_begin", {"attachments": "a.txt", "rules": "users"}, "t-click"),
            ("send_begin", ["post"]),
            ("click_end",),
            ("send_release",),
        ],
    ),
    # A stalled render sent again over a later render's rules.
    "stall_sent_again_over_later_rules": (
        _V2,
        [
            ("click_begin", {"attachments": "a.txt", "rules": "users"}, "t-click"),
            ("refresh", {}, "t-later"),
            ("click_end",),
        ],
    ),
}

# // ========================================( Tests )======================================== // #


class TestRenderInterleavings:
    """The message a view ends on shows what its calls leave, in the order made."""

    @pytest.mark.parametrize("view_cls", [_V1, _V2], ids=["v1", "v2"])
    @pytest.mark.parametrize("seed", range(_SEEDS))
    async def test_the_message_shows_what_the_calls_leave(self, view_cls, seed, caplog):
        caplog.set_level(logging.WARNING, logger="cascadeui")
        rng = random.Random(seed)
        run = _Run(view_cls, rng)
        await run.drive(steps=rng.randint(6, 14))
        run.check(caplog)

    @pytest.mark.parametrize("name", sorted(_SCRIPTS))
    async def test_a_shape_found_by_review_leaves_what_its_calls_leave(self, name, caplog):
        caplog.set_level(logging.WARNING, logger="cascadeui")
        view_cls, script = _SCRIPTS[name]
        run = _Run(view_cls, random.Random(0))
        await run.drive(script=script)
        run.check(caplog)
