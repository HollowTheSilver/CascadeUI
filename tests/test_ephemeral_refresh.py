"""Tests for auto_refresh_ephemeral: button arming, emoji validation, and
session-limit replace behavior on ephemeral views.

Covers the v2.2.0 fixes:
- Default refresh_button_emoji is a valid Discord button emoji
- _arm_refresh_button retries without the emoji if Discord rejects it (50035)
- Replace-policy session limiting deletes old ephemeral messages instead of
  freezing them
"""

import asyncio
import contextlib
import importlib
import logging
import sys
import time
import warnings
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import discord
import pytest
from discord.ui import ActionRow, Button, TextDisplay
from helpers import RenderableLayoutView, arm
from helpers import make_interaction as _make_interaction
from helpers import refresh_timers, refused_by_closed_session, until, wait_for_timer

from cascadeui import InstanceLimitError, RenderOutcome
from cascadeui.components.base import StatefulButton
from cascadeui.state.actions import ActionCreators
from cascadeui.state.singleton import get_store
from cascadeui.testing import stub_client
from cascadeui.views.layout import StatefulLayoutView
from cascadeui.views.view import StatefulView

# // ========================================( Default Emoji Validity )======================================== // #


class TestDefaultRefreshEmoji:
    """Default refresh_button_emoji is a valid Unicode emoji accepted by Discord."""

    def test_default_emoji_is_in_emoji_range(self):
        """The default refresh_button_emoji must be a Unicode emoji code
        point (U+1F000+), not an arrow symbol like U+21BB which Discord
        rejects as an invalid button emoji with error 50035.
        """
        default = StatefulView.refresh_button_emoji
        assert len(default) == 1, "Default emoji should be a single code point"
        code_point = ord(default)
        # Emoji live in the Supplementary Multilingual Plane (U+1F000+).
        # The U+2100-U+27FF symbol blocks contain glyphs that look like
        # emoji but are rejected by Discord without VS16.
        assert code_point >= 0x1F000, (
            f"Default emoji U+{code_point:04X} is outside the U+1F000+ "
            f"Unicode emoji range. Discord rejects symbols from the U+2100-"
            f"U+27FF blocks as invalid button emoji."
        )

    def test_default_emoji_matches_across_v1_and_v2(self):
        """V1 and V2 share the same default via _StatefulMixin."""
        assert StatefulView.refresh_button_emoji == StatefulLayoutView.refresh_button_emoji


# // ========================================( Arm Refresh Button )======================================== // #


def _make_emoji_error() -> discord.HTTPException:
    """Construct a 50035 HTTPException matching Discord's emoji rejection."""
    response = MagicMock(status=400, reason="Bad Request")
    err = discord.HTTPException(
        response,
        {
            "code": 50035,
            "message": "Invalid Form Body",
            "errors": {
                "components": {"0": {"components": {"0": {"emoji": {"name": {"_errors": []}}}}}}
            },
        },
    )
    err.code = 50035
    return err


async def _tab():
    return [TextDisplay("tab")]


class TestRefreshButtonHook:
    """build_refresh_button() builds the Continue button; the guide taught the
    private _build_refresh_button(), which keeps working with a warning."""

    @staticmethod
    async def _armed_labels(view):
        view._message = MagicMock()
        view._message.edit = AsyncMock()
        await view._arm_refresh_button()
        return [c.label for c in view.walk_children() if isinstance(c, Button)]

    async def test_an_override_builds_the_armed_button(self):
        # The new name raises no DeprecationWarning, at definition or when armed.
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)

            class _View(StatefulLayoutView):
                auto_refresh_ephemeral = True

                def build_refresh_button(self):
                    button = super().build_refresh_button()
                    button.label = "Keep going"
                    return button

            labels = await self._armed_labels(_View(interaction=_make_interaction()))

        assert labels == ["Keep going"]

    @pytest.mark.parametrize("base_name", ["StatefulLayoutView", "TabLayoutView"])
    async def test_an_override_of_the_old_name_still_builds_it_and_warns(self, base_name):
        import cascadeui

        base = getattr(cascadeui, base_name)
        kwargs = {"tabs": {"A": _tab}} if base_name == "TabLayoutView" else {}

        with pytest.warns(DeprecationWarning, match="rename it build_refresh_button") as caught:

            class _Legacy(base):
                auto_refresh_ephemeral = True

                def _build_refresh_button(self):
                    return StatefulButton(label="Legacy", callback=self._reopen_ephemeral)

        # Attributed to the module defining the class, which is what Python's
        # default filters decide by; a tab view's own class hook calls the
        # one that warns, so a fixed stacklevel would name library code there.
        assert caught[0].filename == __file__
        view = _Legacy(interaction=_make_interaction(), **kwargs)
        assert await self._armed_labels(view) == ["Legacy"]

    async def test_super_of_the_old_name_returns_the_default_button(self):
        with pytest.warns(DeprecationWarning):

            class _Legacy(StatefulLayoutView):
                auto_refresh_ephemeral = True

                def _build_refresh_button(self):
                    button = super()._build_refresh_button()
                    button.label = f"{button.label}!"
                    return button

        with pytest.warns(DeprecationWarning, match="call build_refresh_button"):
            labels = await self._armed_labels(_Legacy(interaction=_make_interaction()))

        assert labels == [f"{StatefulLayoutView.refresh_button_label}!"]

    async def test_a_class_defining_both_names_uses_the_new_one(self):
        with pytest.warns(DeprecationWarning, match="is the one called"):

            class _Both(StatefulLayoutView):
                auto_refresh_ephemeral = True

                def build_refresh_button(self):
                    return StatefulButton(label="New", callback=self._reopen_ephemeral)

                def _build_refresh_button(self):
                    return StatefulButton(label="Old", callback=self._reopen_ephemeral)

        assert await self._armed_labels(_Both(interaction=_make_interaction())) == ["New"]

    async def test_an_old_name_override_on_a_plain_mixin_is_used(self):
        """The mapping read only the new class's own body, so an override on a
        mixin shared by V1 and V2 views was skipped: the default button went up,
        with no warning, where the old name had always been honoured."""

        class _ContinueMixin:
            def _build_refresh_button(self):
                return StatefulButton(label="Resume", callback=self._reopen_ephemeral)

        with pytest.warns(
            DeprecationWarning, match="_ContinueMixin overrides _build_refresh_button"
        ):

            class _View(_ContinueMixin, StatefulLayoutView):
                auto_refresh_ephemeral = True

        assert await self._armed_labels(_View(interaction=_make_interaction())) == ["Resume"]

    async def test_a_subclass_of_a_mapped_view_does_not_warn_again(self):
        with pytest.warns(DeprecationWarning):

            class _Legacy(StatefulLayoutView):
                auto_refresh_ephemeral = True

                def _build_refresh_button(self):
                    return StatefulButton(label="Legacy", callback=self._reopen_ephemeral)

        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)

            class _Child(_Legacy):
                pass

        assert await self._armed_labels(_Child(interaction=_make_interaction())) == ["Legacy"]

    async def test_an_override_that_raises_leaves_the_view_live_and_says_so(self, caplog):
        """The flag was set and the tree cleared before the button was built,
        so a raising override froze the panel and logged only at DEBUG."""

        class _View(StatefulLayoutView):
            auto_refresh_ephemeral = True

            def build_ui(self):
                self.clear_items()
                self.add_item(TextDisplay("live"))

            def build_refresh_button(self):
                raise KeyError("refresh_label")

        view = _View(interaction=_make_interaction())
        view.build_ui()
        view._message = MagicMock()
        view._message.edit = AsyncMock()

        with caplog.at_level(logging.WARNING, logger="cascadeui"):
            await view._arm_refresh_button()

        assert view._refresh_armed is False
        assert [c.content for c in view.children] == ["live"]
        record = next(r for r in caplog.records if "not armed" in r.getMessage())
        assert isinstance(record.exc_info[1], KeyError)

    @pytest.mark.parametrize("shape", ["row", "long_label"])
    async def test_a_button_that_cannot_be_placed_leaves_the_view_live(self, shape, caplog):
        """Only a raising override was caught before arming: a button Discord
        would refuse (a row where a button belongs, a label past 80
        characters) armed the view over an empty tree, logged at DEBUG."""

        class _View(StatefulLayoutView):
            auto_refresh_ephemeral = True
            refresh_button_label = "x" * 89 if shape == "long_label" else "Continue"

            def build_ui(self):
                self.clear_items()
                self.add_item(TextDisplay("live"))

            def build_refresh_button(self):
                button = super().build_refresh_button()
                return ActionRow(button) if shape == "row" else button

        view = _View(interaction=_make_interaction())
        view.build_ui()
        view._message = MagicMock()
        view._message.edit = AsyncMock()

        with caplog.at_level(logging.WARNING, logger="cascadeui"):
            await view._arm_refresh_button()

        assert view._refresh_armed is False
        assert [c.content for c in view.children] == ["live"]
        view._message.edit.assert_not_awaited()
        assert any("not armed" in r.getMessage() for r in caplog.records)

    async def test_an_async_override_builds_the_armed_button(self):
        class _View(StatefulLayoutView):
            auto_refresh_ephemeral = True

            async def build_refresh_button(self):
                button = super().build_refresh_button()
                button.label = "Keep going"
                return button

        assert await self._armed_labels(_View(interaction=_make_interaction())) == ["Keep going"]

    def test_a_user_package_named_like_the_library_is_named_in_the_warning(self, tmp_path):
        """Frames were skipped by prefix, so a package called ``cascadeui_panels``
        read as library code and the warning named whatever imported it."""
        module = tmp_path / "cascadeui_panels.py"
        module.write_text(
            "\n".join(
                [
                    "from cascadeui import StatefulLayoutView",
                    "class Panel(StatefulLayoutView):",
                    "    def _build_refresh_button(self):",
                    "        return super()._build_refresh_button()",
                    "",
                ]
            )
        )
        sys.path.insert(0, str(tmp_path))
        try:
            with pytest.warns(DeprecationWarning) as caught:
                importlib.import_module("cascadeui_panels")
        finally:
            sys.path.remove(str(tmp_path))
            sys.modules.pop("cascadeui_panels", None)

        assert Path(caught[0].filename) == module


class TestArmRefreshButton:
    """Arming the ephemeral refresh button installs a working reopen mechanism."""

    async def test_arm_with_default_emoji_succeeds(self):
        """Arming with the default (valid) emoji should not raise and
        should leave the view with a single refresh button installed.
        """

        class _View(StatefulLayoutView):
            auto_refresh_ephemeral = True

        view = _View(interaction=_make_interaction())
        view._message = MagicMock()
        view._message.edit = AsyncMock()

        await view._arm_refresh_button()

        assert view._refresh_armed is True
        view._message.edit.assert_awaited_once()

    async def test_arm_ships_inside_an_active_cooldown_window(self):
        """The arming edit answers to the 900s token expiry, not to the
        library's own pacing.

        A view carrying ``refresh_cooldown_ms`` would otherwise queue its
        handoff behind the cooldown window, and a window longer than the
        90-second arming margin would strand it entirely.
        """

        class _View(StatefulLayoutView):
            auto_refresh_ephemeral = True
            refresh_cooldown_ms = 120000  # longer than the 90s arming margin

        view = _View(interaction=_make_interaction())
        view._message = MagicMock()
        view._message.edit = AsyncMock()
        view._cooldown_not_before = time.monotonic() + 120

        await view._arm_refresh_button()

        view._message.edit.assert_awaited_once()
        assert view._deferred_refresh_task is None

    async def test_deferred_refresh_does_not_rebuild_over_the_armed_tree(self):
        """An armed view is frozen on its refresh button.

        ``_handle_state_notification`` enforces that freeze, but
        ``_deferred_refresh`` calls ``on_state_changed`` directly and used to
        walk past it: the rebuild cleared the button, and the armed flag then
        dropped every notification that could put it back, leaving a dead
        panel at the token expiry.
        """
        rebuilds = []

        class _View(StatefulLayoutView):
            auto_refresh_ephemeral = True

            def build_ui(self):
                rebuilds.append(1)
                self.clear_items()
                self.add_item(ActionRow(StatefulButton(label="Normal", custom_id="n")))

        view = _View(interaction=_make_interaction())
        view.build_ui()
        view._message = MagicMock()
        view._message.edit = AsyncMock()
        await arm(view)
        view._last_tree_digest = None  # as an arming edit that did not land leaves it
        armed_tree = list(view.children)
        rebuilds.clear()

        await view._deferred_refresh(0.01)

        assert rebuilds == []  # build_ui must not run
        assert list(view.children) == armed_tree
        view._message.edit.assert_awaited_once()

    @pytest.mark.parametrize("version", ["v1", "v2"])
    async def test_a_callers_render_keeps_the_continue_button(self, version):
        """A render from the caller's own code (a modal submitted late, a
        rebuild then refresh) replaced the Continue button with a panel that
        stops working when the token expires, and the armed view renders
        nothing after that could put the button back."""
        base = StatefulLayoutView if version == "v2" else StatefulView

        class _View(base):
            auto_refresh_ephemeral = True

            def build_ui(self):
                self.clear_items()
                save = StatefulButton(label="Save", custom_id="save")
                self.add_item(ActionRow(save) if version == "v2" else save)

        view = _View(interaction=_make_interaction())
        view.build_ui()
        view._message = MagicMock()
        view._message.edit = AsyncMock()
        await view._arm_refresh_button()
        armed_tree = list(view.children)
        view._message.edit.reset_mock()

        view.build_ui()
        outcome = await view.refresh()

        assert outcome == RenderOutcome.SKIPPED
        assert list(view.children) == armed_tree
        view._message.edit.assert_not_awaited()

    @pytest.mark.parametrize("version", ["v1", "v2"])
    async def test_a_final_card_after_the_close_ships(self, version):
        """A closed view's controls are already gone, and its own final card
        was swapped for the Continue button and never sent."""
        base = StatefulLayoutView if version == "v2" else StatefulView

        class _View(base):
            auto_refresh_ephemeral = True

        view = _View(interaction=_make_interaction())
        view.add_item(TextDisplay("panel") if version == "v2" else Button(label="x", custom_id="x"))
        view._message = MagicMock()
        view._message.edit = AsyncMock()
        await arm(view)
        await view.exit()
        view._message.edit.reset_mock()
        card = discord.Embed(title="Session closed")
        if version == "v2":
            view.clear_items()
            view.add_item(TextDisplay("Session closed"))
            outcome = await view.refresh()
        else:
            outcome = await view.refresh(embed=card)

        assert outcome == RenderOutcome.RENDERED
        sent = view._message.edit.await_args.kwargs
        if version == "v2":
            assert [c.content for c in sent["view"].children] == ["Session closed"]
        else:
            assert sent["embed"] is card

    async def test_a_view_sent_again_is_not_armed(self):
        """A view armed on one message and sent again stayed armed, so every
        render of the new panel put the old message's Continue button back."""

        class _View(StatefulLayoutView):
            auto_refresh_ephemeral = True
            count = 0

            async def on_load(self):
                self.build_ui()

            def build_ui(self):
                self.clear_items()
                self.add_item(TextDisplay(f"count {self.count}"))
                self.add_item(
                    ActionRow(StatefulButton(label="+1", custom_id="inc", callback=self.inc))
                )

            async def inc(self, interaction):
                self.count += 1
                self.build_ui()
                await self.refresh()

        view = _View(interaction=_make_interaction())
        view.build_ui()
        view._message = MagicMock(id=1)
        view._message.edit = AsyncMock(return_value=view._message)
        view._message.delete = AsyncMock()
        await arm(view)
        view.interaction = _make_interaction()
        posted = await view.send()
        click = _make_interaction(message=MagicMock(id=posted.id))

        await view._scheduled_task(view.children[1].children[0], click)

        assert view._refresh_armed is False
        assert view.children[0].content == "count 1"
        click.response.edit_message.assert_awaited_once()

    async def test_the_emoji_retry_keeps_the_button_while_it_builds(self):
        """The retry cleared the tree before its build awaited, so a render
        in that window put the rejected button back and the retry then added
        its own beside it."""
        building = asyncio.Event()
        gate = asyncio.Event()

        class _View(StatefulLayoutView):
            auto_refresh_ephemeral = True
            refresh_button_emoji = "\N{CLOCKWISE RIGHTWARDS AND LEFTWARDS OPEN CIRCLE ARROWS}"
            builds = 0

            async def build_refresh_button(self):
                self.builds += 1
                if self.builds == 2:
                    building.set()
                    await gate.wait()
                return super().build_refresh_button()

        view = _View()
        view.add_item(TextDisplay("panel"))
        view._message = MagicMock()
        sent = []

        async def edit(**kwargs):
            emojis = [b.emoji for row in kwargs["view"].children for b in row.children]
            sent.append(emojis)
            if any(emojis):
                raise _make_emoji_error()
            return view._message

        view._message.edit = edit
        arming = asyncio.create_task(view._arm_refresh_button())
        await building.wait()
        armed_tree = list(view.children)
        with contextlib.suppress(discord.HTTPException):
            await view.refresh()  # another task's render while the retry builds
        gate.set()
        await arming

        assert len(armed_tree) == 1
        assert len(view.children) == 1
        assert sent[-1] == [None]

    async def test_a_render_after_a_failed_arming_ships_the_continue_button(self):
        """The arming edit never landed, and a caller's render that could
        have sent the button was turned into nothing."""

        class _View(StatefulLayoutView):
            auto_refresh_ephemeral = True

            def build_ui(self):
                self.clear_items()
                self.add_item(TextDisplay("panel"))
                self.add_item(ActionRow(StatefulButton(label="go", custom_id="go")))

        view = _View(interaction=_make_interaction())
        view.build_ui()
        view._message = MagicMock()
        view._message.edit = AsyncMock(side_effect=[view._message, _http_error(503), view._message])
        await view.refresh()
        view._queue_deferred_refresh = lambda wait: None
        await view._arm_refresh_button()

        view.build_ui()
        outcome = await view.refresh()

        assert outcome == RenderOutcome.RENDERED
        shipped = view._message.edit.await_args.kwargs["view"]
        labels = [b.label for row in shipped.children for b in getattr(row, "children", [])]
        assert labels == [view.refresh_button_label]

    async def test_arm_retries_without_emoji_on_50035(self):
        """If Discord rejects the button emoji with error 50035, the
        library should retry the arm once without the emoji and succeed.
        """

        class _View(StatefulLayoutView):
            auto_refresh_ephemeral = True
            refresh_button_emoji = "\u21bb"  # deliberately bad

        view = _View(interaction=_make_interaction())
        view._message = MagicMock()
        # First edit raises the emoji rejection, second edit succeeds
        view._message.edit = AsyncMock(side_effect=[_make_emoji_error(), None])

        await view._arm_refresh_button()

        # Two edits means the retry path ran
        assert view._message.edit.await_count == 2

    async def test_arm_does_not_retry_on_non_emoji_error(self):
        """A 50035 without 'emoji' in the message should NOT trigger the
        retry path; it logs and swallows instead.
        """

        class _View(StatefulLayoutView):
            auto_refresh_ephemeral = True

        view = _View(interaction=_make_interaction())
        view._message = MagicMock()

        response = MagicMock(status=400, reason="Bad Request")
        other_err = discord.HTTPException(response, {"code": 50035, "message": "Invalid Form Body"})
        other_err.code = 50035
        view._message.edit = AsyncMock(side_effect=other_err)

        await view._arm_refresh_button()

        # Only the initial attempt, no retry
        assert view._message.edit.await_count == 1

    async def test_arm_is_idempotent(self):
        """_refresh_armed gate should prevent double-arming."""

        class _View(StatefulLayoutView):
            auto_refresh_ephemeral = True

        view = _View(interaction=_make_interaction())
        view._message = MagicMock()
        view._message.edit = AsyncMock()

        await view._arm_refresh_button()
        await view._arm_refresh_button()

        # Second call should be a no-op
        view._message.edit.assert_awaited_once()


# // ========================================( Armed View Freeze )======================================== // #


class TestArmedViewFreeze:
    """An armed ephemeral view ignores subsequent state notifications.

    Once ``_arm_refresh_button`` swaps the view's children for the
    refresh button, any state-driven rebuild would clobber it -- leaving
    the user with no recovery path once the interaction token expires.
    The notification handler short-circuits to keep the button visible
    inside the 90-second pre-warning window.
    """

    async def test_armed_view_drops_state_notifications(self):
        """After arming, _handle_state_notification must skip on_state_changed."""

        class _View(StatefulLayoutView):
            auto_refresh_ephemeral = True

        view = _View(interaction=_make_interaction())
        view._message = MagicMock()
        view._message.edit = AsyncMock()
        view.on_state_changed = AsyncMock()

        view._refresh_armed = True

        await view._handle_state_notification({}, {"type": "DEMO_ACTION"})

        view.on_state_changed.assert_not_awaited()

    async def test_unarmed_view_still_runs_on_state_changed(self):
        """The guard must only fire when armed; normal flow is preserved."""

        class _View(StatefulLayoutView):
            auto_refresh_ephemeral = True

        view = _View(interaction=_make_interaction())
        view._message = MagicMock()
        view._message.edit = AsyncMock()
        view.on_state_changed = AsyncMock()

        await view._handle_state_notification({}, {"type": "DEMO_ACTION"})

        view.on_state_changed.assert_awaited_once()


# // ========================================( Replace-Path replace_policy )======================================== // #


class TestReplacePolicyExitBehavior:
    """replace_policy controls whether instance replacement deletes or disables the old message."""

    def test_default_is_delete(self):
        """replace_policy should default to "delete" on StatefulView so
        the replace path cleanly supplants the old message.
        """
        assert StatefulView.replace_policy == "delete"
        assert StatefulLayoutView.replace_policy == "delete"

    async def test_replace_deletes_by_default(self):
        """Under replace policy with default replace_policy="delete",
        exiting the old view should pass delete_message=True.
        """

        class _View(StatefulView):
            instance_limit = 1
            instance_scope = "user_guild"

        view1 = _View(interaction=_make_interaction())
        from cascadeui.state.singleton import get_store

        get_store()._register_view(view1)

        view2 = _View(interaction=_make_interaction())
        with patch.object(view1, "exit", new_callable=AsyncMock) as mock_exit:
            await view2._enforce_instance_limit()
            mock_exit.assert_awaited_once_with(delete_message=True)

    async def test_replace_disables_when_opted_in(self):
        """Views that opt into the frozen pattern via replace_policy="disable"
        should see the old view exited with delete_message=False, leaving
        the frozen message visible in channel history.
        """

        class _View(StatefulView):
            instance_limit = 1
            instance_scope = "user_guild"
            replace_policy = "disable"

        view1 = _View(interaction=_make_interaction())
        from cascadeui.state.singleton import get_store

        get_store()._register_view(view1)

        view2 = _View(interaction=_make_interaction())
        with patch.object(view1, "exit", new_callable=AsyncMock) as mock_exit:
            await view2._enforce_instance_limit()
            mock_exit.assert_awaited_once_with(delete_message=False)


# // ========================================( Bare exit() exit_policy )======================================== // #


class TestExitPolicy:
    """exit_policy controls whether bare exit() freezes or deletes the message."""

    def test_default_is_disable(self):
        """exit_policy should default to "disable" on StatefulView so
        bare exit() calls preserve the historical safe-by-default
        freeze behavior.
        """
        assert StatefulView.exit_policy == "disable"
        assert StatefulLayoutView.exit_policy == "disable"

    async def test_bare_exit_freezes_by_default(self):
        """Bare exit() with default exit_policy="disable" should
        resolve delete_message to False, preserving the message.
        """

        class _View(StatefulView):
            pass

        view = _View(interaction=_make_interaction())
        view._message = MagicMock()
        view._message.edit = AsyncMock()
        view._message.delete = AsyncMock()

        await view.exit()

        view._message.delete.assert_not_awaited()
        view._message.edit.assert_awaited()

    async def test_bare_exit_deletes_when_opted_in(self):
        """Views that opt into exit_policy="delete" should have
        bare exit() calls remove the message.
        """

        class _View(StatefulView):
            exit_policy = "delete"

        view = _View(interaction=_make_interaction())
        view._message = MagicMock()
        view._message.edit = AsyncMock()
        view._message.delete = AsyncMock()

        await view.exit()

        view._message.delete.assert_awaited_once()

    def test_instance_limit_error_default_message_singular(self):
        """default_message should use singular phrasing when limit == 1."""
        err = InstanceLimitError("MyView", 1)
        assert "already have one" in err.default_message
        assert "MyView" not in err.default_message  # No internal class names

    def test_instance_limit_error_default_message_plural(self):
        """default_message should use plural phrasing when limit > 1."""
        err = InstanceLimitError("MyView", 3)
        assert "3" in err.default_message
        assert "MyView" not in err.default_message

    @pytest.mark.parametrize(
        "kwargs, singular, plural",
        [
            ({"scope": "guild"}, "already open in this server", "This server can only have 3"),
            ({"scope": "global"}, "One of these is already open.", "Only 3 of these"),
            ({"blocked_user_id": 7}, "already taking part", "only take part in 3"),
            ({"scope": "user_guild"}, "already have one", "only have 3"),
        ],
    )
    def test_default_message_is_worded_for_who_the_limit_counts(self, kwargs, singular, plural):
        assert singular in InstanceLimitError("MyView", 1, **kwargs).default_message
        assert plural in InstanceLimitError("MyView", 3, **kwargs).default_message

    async def test_explicit_argument_overrides_policy(self):
        """An explicit delete_message argument to exit() must override
        whatever exit_policy says.
        """

        class _View(StatefulView):
            exit_policy = "delete"  # would delete by default

        view = _View(interaction=_make_interaction())
        view._message = MagicMock()
        view._message.edit = AsyncMock()
        view._message.delete = AsyncMock()

        # Explicit False must override the "delete" policy
        await view.exit(delete_message=False)

        view._message.delete.assert_not_awaited()
        view._message.edit.assert_awaited()

    async def test_exit_swallows_not_found(self, caplog):
        """404 on the cleanup call is expected lifecycle (dismissed
        ephemeral, admin delete, channel delete). exit() must swallow
        silently -- no ERROR log, no raised exception.
        """
        import logging

        class _View(StatefulView):
            exit_policy = "delete"

        view = _View(interaction=_make_interaction())
        view._message = MagicMock()
        response = MagicMock(status=404, reason="Not Found")
        view._message.delete = AsyncMock(side_effect=discord.NotFound(response, "Unknown Message"))

        with caplog.at_level(logging.ERROR, logger="cascadeui.views.base"):
            await view.exit()

        assert not any("Error cleaning up message" in record.message for record in caplog.records)

    async def test_exit_debug_logs_ephemeral_token_expired(self, caplog):
        """401 on ephemeral cleanup is the webhook-token cliff -- expected
        lifecycle past the 15-minute wall. exit() demotes to DEBUG and
        never fires an ERROR log for this case.
        """
        import logging

        class _View(StatefulView):
            pass

        view = _View(interaction=_make_interaction())
        view._message = MagicMock()
        view._ephemeral = True
        response = MagicMock(status=401, reason="Unauthorized")
        view._message.edit = AsyncMock(
            side_effect=discord.HTTPException(response, "Invalid Webhook Token")
        )

        with caplog.at_level(logging.DEBUG, logger="cascadeui.views.base"):
            await view.exit()

        error_records = [
            r for r in caplog.records if r.levelname == "ERROR" and r.name.startswith("cascadeui")
        ]
        debug_records = [r for r in caplog.records if r.levelname == "DEBUG"]
        assert not error_records
        assert any("webhook token expired" in r.message for r in debug_records)

    async def test_exit_error_logs_unexpected_http_error(self, caplog):
        """A genuine HTTP error (not 404, not ephemeral 401) must still
        land in the ERROR log so operators notice real problems.
        """
        import logging

        class _View(StatefulView):
            exit_policy = "delete"

        view = _View(interaction=_make_interaction())
        view._message = MagicMock()
        response = MagicMock(status=500, reason="Internal Server Error")
        view._message.delete = AsyncMock(
            side_effect=discord.HTTPException(response, "Server error")
        )

        with caplog.at_level(logging.ERROR, logger="cascadeui.views.base"):
            await view.exit()

        assert any(
            "Error cleaning up message" in r.message
            for r in caplog.records
            if r.levelname == "ERROR"
        )


# // ========================================( exit_policy Through Helpers )======================================== // #


class TestExitPolicyThroughHelpers:
    """The exit controls the library ships honor exit_policy.

    make_exit_button / add_exit_button / make_nav_row forward
    ``delete_message=None`` so the class attribute stays reachable. A
    concrete default here would occupy the explicit-argument tier and
    silently strand the policy.
    """

    @staticmethod
    def _armed(view):
        """Attach a mock message whose edit/delete calls are observable."""
        view._message = MagicMock()
        view._message.delete = AsyncMock()
        view._message.edit = AsyncMock()
        return view._message

    async def test_v2_add_exit_button_deletes_under_delete_policy(self):
        class _View(StatefulLayoutView):
            exit_policy = "delete"

        view = _View(interaction=_make_interaction())
        button = view.add_exit_button()
        message = self._armed(view)

        await button.callback(_make_interaction())

        assert message.delete.await_count == 1
        assert message.edit.await_count == 0

    async def test_v2_add_exit_button_freezes_under_disable_policy(self):
        class _View(StatefulLayoutView):
            exit_policy = "disable"

        view = _View(interaction=_make_interaction())
        button = view.add_exit_button()
        message = self._armed(view)

        await button.callback(_make_interaction())

        assert message.delete.await_count == 0
        assert message.edit.await_count == 1

    async def test_v1_add_exit_button_deletes_under_delete_policy(self):
        class _View(StatefulView):
            exit_policy = "delete"

        view = _View(interaction=_make_interaction())
        button = view.add_exit_button()
        message = self._armed(view)

        await button.callback(_make_interaction())

        assert message.delete.await_count == 1

    async def test_make_exit_button_deletes_under_delete_policy(self):
        class _View(StatefulLayoutView):
            exit_policy = "delete"

        view = _View(interaction=_make_interaction())
        button = view.make_exit_button()
        view.add_item(ActionRow(button))
        message = self._armed(view)

        await button.callback(_make_interaction())

        assert message.delete.await_count == 1

    async def test_make_nav_row_exit_deletes_under_delete_policy(self):
        class _View(StatefulLayoutView):
            exit_policy = "delete"

        view = _View(interaction=_make_interaction())
        row = view.make_nav_row(back=False)
        view.add_item(row)
        message = self._armed(view)

        await row.children[0].callback(_make_interaction())

        assert message.delete.await_count == 1

    async def test_explicit_true_overrides_disable_policy(self):
        """The explicit argument is the winning tier in both directions."""

        class _View(StatefulLayoutView):
            exit_policy = "disable"

        view = _View(interaction=_make_interaction())
        button = view.add_exit_button(delete_message=True)
        message = self._armed(view)

        await button.callback(_make_interaction())

        assert message.delete.await_count == 1

    async def test_explicit_false_overrides_delete_policy(self):
        class _View(StatefulLayoutView):
            exit_policy = "delete"

        view = _View(interaction=_make_interaction())
        button = view.add_exit_button(delete_message=False)
        message = self._armed(view)

        await button.callback(_make_interaction())

        assert message.delete.await_count == 0
        assert message.edit.await_count == 1

    async def test_on_timeout_freezes_regardless_of_delete_policy(self):
        """An expiry is not a close gesture, so exit_policy does not apply."""

        class _View(StatefulLayoutView):
            exit_policy = "delete"

        view = _View(interaction=_make_interaction())
        view.add_exit_button()
        message = self._armed(view)

        await view.on_timeout()

        assert message.delete.await_count == 0
        assert message.edit.await_count == 1


# // ========================================( on_instance_limit Hook )======================================== // #


class TestOnInstanceLimit:
    """on_instance_limit hook sends ephemeral rejection with default or custom messages."""

    async def test_default_sends_ephemeral_with_default_message(self):
        """The default on_instance_limit should send error.default_message
        as an ephemeral on the originating interaction when no
        instance_limit_message is set.
        """

        class _View(StatefulView):
            instance_limit = 1
            instance_scope = "user_guild"
            instance_policy = "reject"

        view1 = _View(interaction=_make_interaction())
        await view1.send()

        interaction2 = _make_interaction()
        view2 = _View(interaction=interaction2)
        result = await view2.send()

        assert result is None
        # Default message used (no class-level override)
        interaction2.response.send_message.assert_called_once()
        sent_msg = interaction2.response.send_message.call_args[0][0]
        assert "already have one" in sent_msg

    async def test_custom_message_takes_precedence(self):
        """A non-empty instance_limit_message should be sent instead
        of the default.
        """

        class _View(StatefulView):
            instance_limit = 1
            instance_scope = "user_guild"
            instance_policy = "reject"
            instance_limit_message = "Whoa there, partner."

        view1 = _View(interaction=_make_interaction())
        await view1.send()

        interaction2 = _make_interaction()
        view2 = _View(interaction=interaction2)
        await view2.send()

        interaction2.response.send_message.assert_called_once_with(
            "Whoa there, partner.", ephemeral=True
        )

    async def test_falsy_message_falls_back_to_default(self):
        """An empty string for instance_limit_message should fall back
        to error.default_message via the ``or`` chain.
        """

        class _View(StatefulView):
            instance_limit = 1
            instance_scope = "user_guild"
            instance_policy = "reject"
            instance_limit_message = ""

        view1 = _View(interaction=_make_interaction())
        await view1.send()

        interaction2 = _make_interaction()
        view2 = _View(interaction=interaction2)
        await view2.send()

        sent_msg = interaction2.response.send_message.call_args[0][0]
        assert "already have one" in sent_msg

    async def test_override_method_is_called(self):
        """A subclass that overrides on_instance_limit should have its
        override invoked instead of the default behavior.
        """
        captured: list = []

        class _View(StatefulView):
            instance_limit = 1
            instance_scope = "user_guild"
            instance_policy = "reject"

            async def on_instance_limit(self, error):
                captured.append((error.view_type, error.limit))

        view1 = _View(interaction=_make_interaction())
        await view1.send()

        view2 = _View(interaction=_make_interaction())
        result = await view2.send()

        assert result is None
        assert len(captured) == 1
        assert captured[0][1] == 1

    async def test_followup_used_when_response_done(self):
        """If interaction.response.is_done() returns True, the handler
        should fall back to followup.send.
        """

        class _View(StatefulView):
            instance_limit = 1
            instance_scope = "user_guild"
            instance_policy = "reject"

        view1 = _View(interaction=_make_interaction())
        await view1.send()

        interaction2 = _make_interaction()
        interaction2.response.is_done = MagicMock(return_value=True)
        view2 = _View(interaction=interaction2)
        await view2.send()

        interaction2.followup.send.assert_called_once()
        interaction2.response.send_message.assert_not_called()


# // ========================================( Track Child + Refresh Handoff )======================================== // #


class TestAttachChildRefreshHandoff:
    """Verify that auto_refresh_ephemeral migrates the tracked-child slot
    from the old instance to the refreshed one.

    Without this transfer, a parent that called attach_child(old) would
    still hold a reference to the (about-to-exit) old view after refresh,
    and its _cleanup_attached_children pass would silently skip the new view --
    leaving an orphan ephemeral after the parent ends. This was discovered
    during a live Battleship rematch test where MyShipsView panels lingered
    after the game finished, but only when both panels had been refreshed
    past the original 15-minute token window.
    """

    def test_attach_child_sets_back_pointer(self):
        """attach_child should record a back-pointer on the child so it
        can find its parent later.
        """
        parent = StatefulLayoutView(interaction=_make_interaction())
        child = StatefulLayoutView(interaction=_make_interaction())

        parent.attach_child(child)

        assert child in parent._attached_children
        assert child._attached_to is parent

    def test_attach_child_is_idempotent(self):
        """Tracking the same child twice should not duplicate or rebind."""
        parent = StatefulLayoutView(interaction=_make_interaction())
        child = StatefulLayoutView(interaction=_make_interaction())

        parent.attach_child(child)
        parent.attach_child(child)

        assert parent._attached_children.count(child) == 1
        assert child._attached_to is parent

    async def test_cleanup_attached_children_clears_back_pointer(self):
        """After cleanup, surviving children should have no dangling
        back-pointer to the (now-finished) parent.
        """
        parent = StatefulLayoutView(interaction=_make_interaction())
        child = StatefulLayoutView(interaction=_make_interaction())
        child._message = MagicMock()
        child._message.delete = AsyncMock()

        parent.attach_child(child)
        await parent._cleanup_attached_children()

        assert parent._attached_children == []
        assert child._attached_to is None

    async def test_cleanup_attached_children_skips_already_finished(self):
        """Finished children should be silently dropped, not re-exited."""
        parent = StatefulLayoutView(interaction=_make_interaction())
        child = StatefulLayoutView(interaction=_make_interaction())

        parent.attach_child(child)
        child.stop()  # mark finished

        with patch.object(child, "exit", new=AsyncMock()) as mock_exit:
            await parent._cleanup_attached_children()
            mock_exit.assert_not_awaited()

        assert parent._attached_children == []

    async def test_reopen_migrates_tracked_child_slot(self):
        """The headline regression: after _reopen_ephemeral runs, the
        parent's tracked-child slot should hold the new view, not the
        old one. Without the migration, _cleanup_attached_children silently
        skips the new view because it was never tracked.
        """

        class _Refreshable(StatefulLayoutView):
            auto_refresh_ephemeral = True

        parent = StatefulLayoutView(interaction=_make_interaction())
        old_child = _Refreshable(interaction=_make_interaction())
        old_child._message = MagicMock()
        old_child._message.delete = AsyncMock()
        old_child._message.edit = AsyncMock()

        parent.attach_child(old_child)

        # build_reopen_view hands a pre-built replacement to the refresh
        # path without exercising __init__ kwarg snapshotting.
        new_child = _Refreshable(interaction=_make_interaction())
        old_child.build_reopen_view = lambda interaction: new_child

        # Mock send -- no working channel/webhook in test context.
        new_child.send = AsyncMock()
        # Mock self.exit so the old view's exit doesn't try to dispatch.
        old_child.exit = AsyncMock()

        refresh_interaction = _make_interaction()
        await old_child._reopen_ephemeral(refresh_interaction)

        # The slot must now hold the new view, not the old one.
        assert new_child in parent._attached_children
        assert old_child not in parent._attached_children
        assert new_child._attached_to is parent
        assert old_child._attached_to is None

    async def test_reopen_rebuilds_a_child_that_reads_its_parent_while_built(self):
        """The kwargs path strips parent= with the other framework-managed
        kwargs, so a child that read its parent in __init__ raised on every
        Continue and the user got the reopen-failure message.
        """

        class _Parent(RenderableLayoutView):
            ships = "fleet"

        class _Child(StatefulLayoutView):
            auto_refresh_ephemeral = True

            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.label = self.parent.ships
                self.add_item(TextDisplay(self.label))

        parent = _Parent(interaction=_make_interaction())
        await parent.send()
        child = _Child(interaction=_make_interaction(), parent=parent)
        await child.send(ephemeral=True)

        await child._reopen_ephemeral(_make_interaction())

        assert child._torn_down(), "the reopen went through"
        (replacement,) = parent._attached_children
        assert isinstance(replacement, _Child) and replacement is not child
        assert replacement.label == "fleet"
        assert replacement.parent is parent

    async def test_reopen_under_a_closed_parent_still_continues(self):
        """A parent stopped without closing its children leaves the child
        live. Handing that parent to the replacement made its send close the
        replacement straight away, and the old panel stayed on a Continue
        button that could never go anywhere.
        """

        class _Child(StatefulLayoutView):
            auto_refresh_ephemeral = True

            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.add_item(TextDisplay("fleet"))

        parent = RenderableLayoutView(interaction=_make_interaction())
        await parent.send()
        child = _Child(interaction=_make_interaction(), parent=parent)
        await child.send(ephemeral=True)
        parent.stop()

        await child._reopen_ephemeral(_make_interaction())

        assert child._torn_down(), "the reopen went through"
        replacements = [
            v
            for v in child.state_store._active_views.values()
            if type(v) is _Child and v is not child
        ]
        assert len(replacements) == 1 and not replacements[0]._torn_down()

    async def test_reopen_skips_migration_when_parent_finished(self):
        """If the parent already exited while the child was waiting on
        a refresh click, the migration should silently no-op rather than
        re-adding the child to a dead parent's list.
        """

        class _Refreshable(StatefulLayoutView):
            auto_refresh_ephemeral = True

        parent = StatefulLayoutView(interaction=_make_interaction())
        old_child = _Refreshable(interaction=_make_interaction())
        old_child._message = MagicMock()
        old_child._message.delete = AsyncMock()

        parent.attach_child(old_child)
        parent.stop()  # parent ends before child refresh fires

        new_child = _Refreshable(interaction=_make_interaction())
        old_child.build_reopen_view = lambda interaction: new_child
        new_child.send = AsyncMock()
        old_child.exit = AsyncMock()

        refresh_interaction = _make_interaction()
        await old_child._reopen_ephemeral(refresh_interaction)

        # Parent's list is untouched (cleanup will run on its own exit
        # path), but the back-pointer is cleared so the new child
        # doesn't think it's still parented.
        assert new_child._attached_to is None
        assert new_child not in parent._attached_children

    async def test_reopen_reparents_own_children_instead_of_deleting(self):
        """The reopening view's OWN attached children survive the handoff.

        exit() at the end of _reopen_ephemeral cascades into
        _cleanup_attached_children, which exits every still-tracked child
        with delete_message=True. Children must be re-parented onto the
        replacement first so the exit cascade finds nothing to delete.
        """

        class _Refreshable(StatefulLayoutView):
            auto_refresh_ephemeral = True

        old_parent = _Refreshable(interaction=_make_interaction())
        old_parent._message = MagicMock()
        old_parent._message.delete = AsyncMock()
        old_parent._message.edit = AsyncMock()

        child = StatefulLayoutView(interaction=_make_interaction())
        old_parent.attach_child(child)

        new_parent = _Refreshable(interaction=_make_interaction())
        old_parent.build_reopen_view = lambda interaction: new_parent
        new_parent.send = AsyncMock()

        # exit() runs for real so the cascade is exercised; only the
        # child's exit is spied to prove it never fires.
        with patch.object(child, "exit", new=AsyncMock()) as child_exit:
            await old_parent._reopen_ephemeral(_make_interaction())
            child_exit.assert_not_awaited()

        assert child in new_parent._attached_children
        assert child._attached_to is new_parent
        assert old_parent._attached_children == []


# // ========================================( on_reopen_failure Hook )======================================== // #


class TestOnReopenFailure:
    """Verify that the on_reopen_failure hook fires when _reopen_ephemeral
    cannot construct a replacement view, and that the default implementation
    sends the class-level reopen_failure_message as an ephemeral.
    """

    async def test_factory_error_sends_reopen_failure_message(self):
        """When the reopen factory raises, the default on_reopen_failure
        should send reopen_failure_message as an ephemeral.
        """

        class _View(StatefulLayoutView):
            auto_refresh_ephemeral = True

        view = _View(interaction=_make_interaction())
        view._message = MagicMock()
        view._message.delete = AsyncMock()
        view.build_reopen_view = lambda interaction: (_ for _ in ()).throw(RuntimeError("boom"))

        refresh_interaction = _make_interaction()
        await view._reopen_ephemeral(refresh_interaction)

        refresh_interaction.response.send_message.assert_called_once_with(
            view.reopen_failure_message, ephemeral=True
        )

    async def test_factory_none_sends_session_ended(self):
        """When the reopen factory returns None, the default
        on_reopen_failure should send "This session has ended." and
        call exit().
        """

        class _View(StatefulLayoutView):
            auto_refresh_ephemeral = True

        view = _View(interaction=_make_interaction())
        view._message = MagicMock()
        view._message.delete = AsyncMock()
        view.build_reopen_view = lambda interaction: None
        view.exit = AsyncMock()

        refresh_interaction = _make_interaction()
        await view._reopen_ephemeral(refresh_interaction)

        refresh_interaction.response.send_message.assert_called_once_with(
            "This session has ended.", ephemeral=True
        )
        view.exit.assert_awaited_once()

    async def test_factory_none_runs_on_session_ended(self):
        seen = []

        class _View(StatefulLayoutView):
            auto_refresh_ephemeral = True

            async def on_session_ended(self, interaction):
                seen.append(interaction)

        view = _View(interaction=_make_interaction())
        view._message = MagicMock()
        view._message.delete = AsyncMock()
        view.build_reopen_view = lambda interaction: None
        view.exit = AsyncMock()

        refresh_interaction = _make_interaction()
        await view._reopen_ephemeral(refresh_interaction)

        assert seen == [refresh_interaction]
        refresh_interaction.response.send_message.assert_not_called()
        view.exit.assert_awaited_once()

    async def test_factory_none_with_no_session_ended_message_sends_nothing(self):
        class _View(StatefulLayoutView):
            auto_refresh_ephemeral = True
            session_ended_message = None

        view = _View(interaction=_make_interaction())
        view._message = MagicMock()
        view._message.delete = AsyncMock()
        view.build_reopen_view = lambda interaction: None
        view.exit = AsyncMock()

        refresh_interaction = _make_interaction()
        await view._reopen_ephemeral(refresh_interaction)

        refresh_interaction.response.send_message.assert_not_called()
        refresh_interaction.followup.send.assert_not_called()
        view.exit.assert_awaited_once()

    async def test_custom_message_attribute(self):
        """A subclass that overrides reopen_failure_message should see
        the custom text in the ephemeral response.
        """

        class _View(StatefulLayoutView):
            auto_refresh_ephemeral = True
            reopen_failure_message = "Oops, try /settings again."

        view = _View(interaction=_make_interaction())
        view._message = MagicMock()
        view._message.delete = AsyncMock()
        view.build_reopen_view = lambda interaction: (_ for _ in ()).throw(RuntimeError("boom"))

        refresh_interaction = _make_interaction()
        await view._reopen_ephemeral(refresh_interaction)

        refresh_interaction.response.send_message.assert_called_once_with(
            "Oops, try /settings again.", ephemeral=True
        )

    async def test_hook_override_replaces_default(self):
        """A subclass that overrides on_reopen_failure should have its
        custom logic invoked instead of the default ephemeral send.
        """
        captured: list = []

        class _View(StatefulLayoutView):
            auto_refresh_ephemeral = True

            async def on_reopen_failure(self, interaction, error=None):
                captured.append(("factory_error" if error else "session_ended", str(error)))

        view = _View(interaction=_make_interaction())
        view._message = MagicMock()
        view._message.delete = AsyncMock()
        view.build_reopen_view = lambda interaction: (_ for _ in ()).throw(ValueError("test"))

        refresh_interaction = _make_interaction()
        await view._reopen_ephemeral(refresh_interaction)

        assert len(captured) == 1
        assert captured[0][0] == "factory_error"
        # The default send_message should NOT have been called
        refresh_interaction.response.send_message.assert_not_called()

    async def test_a_refused_replacement_leaves_the_current_view_and_its_children(self):
        # The replacement's send returned None; the old view exited anyway,
        # after handing its children to a replacement that never went live.
        refuse = [False]

        class _View(RenderableLayoutView):
            timeout = None

            async def on_pre_send(self, interaction):
                return not refuse[0]

        view = _View(interaction=_make_interaction())
        await view.send(ephemeral=True)
        child = RenderableLayoutView(interaction=_make_interaction(), parent=view)
        await child.send()
        refuse[0] = True

        await view._reopen_ephemeral(_make_interaction())

        assert not view._torn_down()
        assert child._attached_to is view
        assert not view._reopen_in_flight

    async def test_a_second_continue_during_a_reopen_is_logged(self, caplog):
        caplog.set_level(logging.DEBUG, logger="cascadeui")
        gate = asyncio.Event()

        class _View(RenderableLayoutView):
            timeout = None

            async def build_reopen_view(self, interaction):
                await gate.wait()
                return _View(interaction=interaction)

        view = _View(interaction=_make_interaction())
        await view.send(ephemeral=True)
        first = asyncio.create_task(view._reopen_ephemeral(_make_interaction()))
        await asyncio.sleep(0)
        second = _make_interaction()
        second.data = {"custom_id": "refresh"}

        await view._reopen_ephemeral(second)

        second.response.defer.assert_awaited_once()
        assert any(
            "Dropped a click" in r.getMessage() and "'refresh'" in r.getMessage()
            for r in caplog.records
            if r.name.startswith("cascadeui")
        )
        gate.set()
        await asyncio.wait_for(first, 5)


# // ========================================( Reopen Carries Identity )======================================== // #


class TestReopenCarriesIdentity:
    """A reopened ephemeral carries the navigation identity forward.

    Since the arm deadline rides the navigation chain, the view that reopens
    can be a mid-chain sub-view, not just a fresh root. _reopen_ephemeral must
    hand off the post-construction selection, nav stack, root-class accounting,
    reopen factory, undo/redo timeline, and the Back button, or the replacement
    comes back as a stateless root.
    """

    async def test_reopen_applies_nav_state_stack_and_root(self):
        """Post-construction selection, _nav_stack, and _instance_root_class
        transfer to the replacement before send() runs on_load.
        """

        class _NavCarry(StatefulLayoutView):
            auto_refresh_ephemeral = True

            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self._sel = "default"

            def get_nav_state(self):
                return {"sel": self._sel}

            def restore_nav_state(self, state):
                self._sel = state.get("sel", self._sel)

        entry = {"class_name": "Parent", "module": "m", "kwargs": {}, "view_state": {}}
        old = _NavCarry(interaction=_make_interaction())
        old._message = MagicMock()
        old._message.delete = AsyncMock()
        old._message.edit = AsyncMock()
        old._sel = "chosen"
        old._nav_stack = [entry]
        old._instance_root_class = "RootView"
        old._instance_root_scope = "user"

        new = _NavCarry(interaction=_make_interaction())
        old.build_reopen_view = lambda interaction: new
        new.send = AsyncMock()
        old.exit = AsyncMock()

        await old._reopen_ephemeral(_make_interaction())

        assert new._sel == "chosen"
        assert new._nav_stack == [entry]
        assert new._instance_root_class == "RootView"
        assert new._instance_root_scope == "user"

    async def test_reopen_carries_session_for_continuity(self):
        """The replacement rejoins the old view's session so shared_data
        survives the token-cliff swap instead of resetting to a fresh one.
        """

        class _Refreshable(StatefulLayoutView):
            auto_refresh_ephemeral = True

        old = _Refreshable(interaction=_make_interaction())
        old._message = MagicMock()
        old._message.delete = AsyncMock()
        old._message.edit = AsyncMock()
        old.session_id = "session-continuity-1"

        new = _Refreshable(interaction=_make_interaction())
        old.build_reopen_view = lambda interaction: new
        new.send = AsyncMock()
        old.exit = AsyncMock()

        await old._reopen_ephemeral(_make_interaction())

        assert new.session_id == "session-continuity-1"

    async def test_reopen_carries_participants(self):
        """A multi-user ephemeral keeps its participants across the reopen."""

        class _Refreshable(StatefulLayoutView):
            auto_refresh_ephemeral = True

        old = _Refreshable(interaction=_make_interaction())
        old._message = MagicMock()
        old._message.delete = AsyncMock()
        old._message.edit = AsyncMock()
        old._participants.add(42)
        old._participants.add(99)

        new = _Refreshable(interaction=_make_interaction())
        old.build_reopen_view = lambda interaction: new
        new.send = AsyncMock()
        old.exit = AsyncMock()

        await old._reopen_ephemeral(_make_interaction())

        assert 42 in new._participants
        assert 99 in new._participants

    async def test_an_overridden_build_reopen_view_builds_every_continue(self):
        """The override lives on the class, so the replacement it built
        uses it on the next Continue as well."""
        built = []

        class _Panel(RenderableLayoutView):
            timeout = None
            auto_refresh_ephemeral = True

            async def build_reopen_view(self, interaction):
                view = _Panel(interaction=interaction, user_id=self.user_id)
                built.append(view)
                return view

        first = _Panel(interaction=_make_interaction(user_id=1, guild_id=100))
        await first.send(ephemeral=True)

        await first._reopen_ephemeral(_make_interaction(user_id=1, guild_id=100))
        await built[0]._reopen_ephemeral(_make_interaction(user_id=1, guild_id=100))

        assert len(built) == 2
        assert first.current_view is built[1]
        assert not built[1].is_finished()

    async def test_build_reopen_view_returning_none_ends_the_session(self):
        class _Panel(RenderableLayoutView):
            timeout = None
            auto_refresh_ephemeral = True

            def build_reopen_view(self, interaction):
                return None

        view = _Panel(interaction=_make_interaction(user_id=1, guild_id=100))
        await view.send(ephemeral=True)
        click = _make_interaction(user_id=1, guild_id=100)

        await view._reopen_ephemeral(click)

        assert view._torn_down()
        click.response.send_message.assert_awaited_once()
        assert "session has ended" in click.response.send_message.await_args.args[0]

    async def test_reopen_transfers_undo_redo_timeline(self):
        """The old view's undo/redo stacks dispatch onto the replacement's
        state row before the old row is destroyed at exit().
        """

        class _Refreshable(StatefulLayoutView):
            auto_refresh_ephemeral = True

        old = _Refreshable(interaction=_make_interaction())
        old._message = MagicMock()
        old._message.delete = AsyncMock()
        old._message.edit = AsyncMock()

        store = get_store()
        undo = [{"application_slots": {}, "shared_data": None}]
        redo = [{"application_slots": {}, "shared_data": None}]
        store.state.setdefault("views", {})[old.id] = {
            "undo_stack": undo,
            "redo_stack": redo,
        }
        try:
            new = _Refreshable(interaction=_make_interaction())
            old.build_reopen_view = lambda interaction: new
            new.send = AsyncMock()
            new.dispatch = AsyncMock()
            old.exit = AsyncMock()

            await old._reopen_ephemeral(_make_interaction())

            new.dispatch.assert_awaited_once()
            action_type, payload = new.dispatch.await_args.args
            assert action_type == "VIEW_UPDATED"
            assert payload == ActionCreators.view_updated(new.id, undo_stack=undo, redo_stack=redo)
        finally:
            store.state.get("views", {}).pop(old.id, None)

    async def test_reopen_readds_back_button_for_mid_chain_view(self):
        """A reopened view with a non-empty stack gets its Back button back --
        send() builds from build_ui/on_load, which never adds it.
        """

        class _Refreshable(StatefulLayoutView):
            auto_refresh_ephemeral = True
            auto_back_button = True

        old = _Refreshable(interaction=_make_interaction())
        old._message = MagicMock()
        old._message.delete = AsyncMock()
        old._message.edit = AsyncMock()
        old._nav_stack = [{"class_name": "Parent", "module": "m", "kwargs": {}, "view_state": {}}]

        new = _Refreshable(interaction=_make_interaction())
        old.build_reopen_view = lambda interaction: new
        new.send = AsyncMock()
        old.exit = AsyncMock()

        await old._reopen_ephemeral(_make_interaction())

        assert getattr(new, "_auto_back_item", None) is not None

    async def test_reopen_root_view_adds_no_back_button(self):
        """A root-stack reopen adds no Back button -- there is nowhere to go."""

        class _Refreshable(StatefulLayoutView):
            auto_refresh_ephemeral = True
            auto_back_button = True

        old = _Refreshable(interaction=_make_interaction())
        old._message = MagicMock()
        old._message.delete = AsyncMock()
        old._message.edit = AsyncMock()

        new = _Refreshable(interaction=_make_interaction())
        old.build_reopen_view = lambda interaction: new
        new.send = AsyncMock()
        old.exit = AsyncMock()

        await old._reopen_ephemeral(_make_interaction())

        assert getattr(new, "_auto_back_item", None) is None


class TestReopenHandsThePanelOn:
    """Code that kept an ephemeral panel reaches its replacement."""

    async def test_current_view_follows_the_continue_button(self, caplog):
        """The kept reference could not reach the view Continue replaced it
        with, and its exit() did nothing, with nothing said."""

        class _Panel(RenderableLayoutView):
            timeout = None
            auto_refresh_ephemeral = True

        old = _Panel(interaction=_make_interaction(user_id=1, guild_id=100))
        await old.send(ephemeral=True)
        with caplog.at_level(logging.WARNING, logger="cascadeui"):
            await old._reopen_ephemeral(_make_interaction(user_id=1, guild_id=100))
            new = old.current_view
            assert new is not old and isinstance(new, _Panel) and not new.is_finished()
            # The reopen's own close of the old view is not a kept reference.
            assert not [r for r in caplog.records if "handed its panel on" in r.getMessage()]

            await old.exit()
            assert any("handed its panel on" in r.getMessage() for r in caplog.records)
        assert not new.is_finished()

        await old.current_view.exit()
        assert new._torn_down()

    @pytest.mark.parametrize("close", ["exit", "on_timeout", "exit_children"])
    async def test_a_close_during_the_continue_leaves_the_replacement_whole(self, close):
        """A close from another task landing while the replacement was sent
        closed this view and its attached view, and the replacement went live
        without them."""
        loading = None

        class _Panel(RenderableLayoutView):
            timeout = None
            auto_refresh_ephemeral = True

            async def on_load(self):
                if loading is not None:
                    await loading.wait()

        old = _Panel(interaction=_make_interaction(user_id=1, guild_id=100))
        await old.send(ephemeral=True)
        child = RenderableLayoutView(
            interaction=_make_interaction(user_id=1, guild_id=100), parent=old
        )
        await child.send(ephemeral=True)
        loading = asyncio.Event()
        click = _make_interaction(user_id=1, guild_id=100)
        continuing = asyncio.create_task(old._reopen_ephemeral(click))
        for _ in range(20):
            await asyncio.sleep(0)
        closing = asyncio.create_task(getattr(old, close)())
        for _ in range(20):
            await asyncio.sleep(0)
        assert not closing.done(), "the close did not wait for the Continue"
        loading.set()
        await asyncio.wait_for(asyncio.gather(continuing, closing), 5)

        new = old.current_view
        assert new is not old and not new.is_finished()
        assert not child.is_finished()
        assert child in new._attached_children

    async def test_a_send_during_the_continue_waits_and_finds_the_panel_handed_on(self):
        """A send() from code while the replacement was built went ahead: the
        old view posted a message the Continue then deleted, and send()
        returned it as if a live panel had come of it."""
        loading = None

        class _Panel(RenderableLayoutView):
            timeout = None
            auto_refresh_ephemeral = True

            async def on_load(self):
                if loading is not None:
                    await loading.wait()

        old = _Panel(interaction=_make_interaction(user_id=1, guild_id=100))
        await old.send(ephemeral=True)
        loading = asyncio.Event()
        continuing = asyncio.create_task(
            old._reopen_ephemeral(_make_interaction(user_id=1, guild_id=100))
        )
        for _ in range(20):
            await asyncio.sleep(0)
        old.interaction = _make_interaction(user_id=1, guild_id=100)
        sending = asyncio.create_task(old.send(ephemeral=True))
        for _ in range(20):
            await asyncio.sleep(0)
        assert not sending.done(), "the send did not wait for the Continue"
        loading.set()
        await asyncio.wait_for(continuing, 5)

        with pytest.raises(RuntimeError, match="handed its panel on"):
            await asyncio.wait_for(sending, 5)
        old.interaction.response.send_message.assert_not_called()
        assert not old.current_view.is_finished()

    async def test_a_send_from_inside_the_continue_is_refused(self):
        """The old view sent from its own Continue (the replacement's on_load)
        posted a second panel that the hand-over then closed."""
        refusals = []

        class _Panel(RenderableLayoutView):
            timeout = None
            auto_refresh_ephemeral = True
            replacing = None

            async def on_load(self):
                if self.replacing is not None and self is not self.replacing:
                    try:
                        await self.replacing.send(ephemeral=True)
                    except RuntimeError as e:
                        refusals.append(str(e))

        old = _Panel(interaction=_make_interaction(user_id=1, guild_id=100))
        await old.send(ephemeral=True)
        _Panel.replacing = old

        await old._reopen_ephemeral(_make_interaction(user_id=1, guild_id=100))

        assert len(refusals) == 1 and "inside the view's own Continue" in refusals[0]
        assert not old.current_view.is_finished()

    async def test_a_continue_cancelled_before_its_replacement_posts_can_be_clicked_again(self):
        """A cancel before Discord accepted the replacement rolled it back and
        left the old view's Continue answering every later click as a repeat."""
        cancel_in_load = None

        class _Panel(RenderableLayoutView):
            timeout = None
            auto_refresh_ephemeral = True

            async def on_load(self):
                nonlocal cancel_in_load
                if cancel_in_load is not None:
                    task, cancel_in_load = cancel_in_load, None
                    task.cancel()
                    await asyncio.sleep(0)

        old = _Panel(interaction=_make_interaction(user_id=1, guild_id=100))
        await old.send(ephemeral=True)
        continuing = asyncio.create_task(
            old._reopen_ephemeral(_make_interaction(user_id=1, guild_id=100))
        )
        cancel_in_load = continuing
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(continuing, 5)
        assert old.current_view is old and not old._torn_down()

        await old._reopen_ephemeral(_make_interaction(user_id=1, guild_id=100))

        assert old.current_view is not old and old._torn_down()

    async def test_a_continue_cancelled_after_its_replacement_posts_hands_the_panel_on(self):
        """A cancel after Discord accepted the replacement went through before
        the hand-over: two live panels, and the old one's Continue still sent
        another."""
        cancel_after_post = None

        class _Panel(RenderableLayoutView):
            timeout = None
            auto_refresh_ephemeral = True

            async def _update_message_state(self, message):
                await super()._update_message_state(message)
                nonlocal cancel_after_post
                if cancel_after_post is not None:
                    task, cancel_after_post = cancel_after_post, None
                    task.cancel()
                    await asyncio.sleep(0)

        old = _Panel(interaction=_make_interaction(user_id=1, guild_id=100))
        await old.send(ephemeral=True)
        message = old._message
        continuing = asyncio.create_task(
            old._reopen_ephemeral(_make_interaction(user_id=1, guild_id=100))
        )
        cancel_after_post = continuing
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(continuing, 5)

        new = old.current_view
        assert new is not old and not new._torn_down()
        assert old._torn_down()
        message.delete.assert_awaited_once()

    async def test_the_gateway_reporting_the_old_message_deleted_leaves_the_replacement_live(self):
        """Discord can report the deletion of the old message before the
        Continue's delete call returns. The listener ran the old view's
        on_message_delete(), whose close followed the hand-over and froze the
        replacement."""
        deletions = []

        class _Panel(RenderableLayoutView):
            timeout = None
            auto_refresh_ephemeral = True

            async def on_message_delete(self):
                deletions.append(self)
                await super().on_message_delete()

        old = _Panel(interaction=_make_interaction(user_id=1, guild_id=100))
        await old.send(ephemeral=True)
        # Every mocked message shares one id, so the old one gets its own and
        # the deletion can match only the old view.
        old._message.id = 4242
        listeners = []

        async def reported_before_it_returns():
            deleted = get_store()._clean_up_deleted(lambda m: m.id == 4242)
            listeners.append(asyncio.ensure_future(deleted))
            for _ in range(3):
                await asyncio.sleep(0)

        old._message.delete = AsyncMock(side_effect=reported_before_it_returns)

        await old._reopen_ephemeral(_make_interaction(user_id=1, guild_id=100))
        await asyncio.wait_for(asyncio.gather(*listeners), 5)

        new = old.current_view
        assert new is not old and not new._torn_down()
        assert deletions == []

    async def test_a_render_finding_the_old_message_deleted_leaves_the_replacement_live(self):
        """A render of the old view whose edit came back 404 while the Continue
        deleted the message called on_message_gone() and on_message_delete(),
        and that close followed the hand-over to the replacement."""
        hooks = []

        class _Panel(RenderableLayoutView):
            timeout = None
            auto_refresh_ephemeral = True

            async def on_message_gone(self):
                hooks.append("gone")

            async def on_message_delete(self):
                hooks.append("delete")
                await super().on_message_delete()

        old = _Panel(interaction=_make_interaction(user_id=1, guild_id=100))
        await old.send(ephemeral=True)
        message = old._message
        edit_started, edit_lands, delete_lands = (asyncio.Event() for _ in range(3))

        async def edit_finds_it_deleted(**kwargs):
            edit_started.set()
            await edit_lands.wait()
            raise discord.NotFound(MagicMock(status=404), "Unknown Message")

        async def delete_in_flight():
            await delete_lands.wait()

        message.edit = AsyncMock(side_effect=edit_finds_it_deleted)
        message.delete = AsyncMock(side_effect=delete_in_flight)
        old._last_tree_digest = None
        render = asyncio.create_task(old.refresh())
        await asyncio.wait_for(edit_started.wait(), 5)
        continuing = asyncio.create_task(
            old._reopen_ephemeral(_make_interaction(user_id=1, guild_id=100))
        )
        await until(lambda: message.delete.await_count == 1)
        try:
            edit_lands.set()
            await asyncio.wait_for(render, 5)
            for _ in range(20):
                await asyncio.sleep(0)
        finally:
            delete_lands.set()
        await asyncio.wait_for(continuing, 5)
        if old._message_gone_task is not None:
            await asyncio.wait_for(old._message_gone_task, 5)

        new = old.current_view
        assert new is not old and not new._torn_down()
        assert hooks == []

    async def test_a_deletion_reported_after_a_failed_delete_leaves_the_replacement_live(self):
        """A delete Discord carried out but whose reply was lost gave the
        message back to the old view before its exit. The deletion the
        gateway reported while an exit() override awaited then ran the old
        view's on_message_delete(), whose close followed the hand-over and
        froze the replacement."""
        deletions = []
        reported = asyncio.Event()

        class _Panel(RenderableLayoutView):
            timeout = None
            auto_refresh_ephemeral = True

            async def on_message_delete(self):
                deletions.append(self)
                await super().on_message_delete()

            async def exit(self, delete_message=None):
                if self is old:
                    # Work done before closing, such as a record written.
                    await asyncio.wait_for(reported.wait(), 5)
                return await super().exit(delete_message=delete_message)

        old = _Panel(interaction=_make_interaction(user_id=1, guild_id=100))
        await old.send(ephemeral=True)
        old._message.id = 4242
        listeners = []

        async def report_deletion():
            await get_store()._clean_up_deleted(lambda m: m.id == 4242)
            reported.set()

        async def deleted_but_the_reply_lost():
            listeners.append(asyncio.ensure_future(report_deletion()))
            raise aiohttp.ServerDisconnectedError()

        old._message.delete = AsyncMock(side_effect=deleted_but_the_reply_lost)

        await old._reopen_ephemeral(_make_interaction(user_id=1, guild_id=100))
        await asyncio.wait_for(asyncio.gather(*listeners), 5)

        new = old.current_view
        assert new is not old and not new._torn_down()
        assert deletions == []

    async def test_a_render_finding_the_message_gone_after_a_failed_delete_runs_no_hook(self):
        """A render of the old view in flight while a delete was carried out
        but its reply lost: the message went back to the old view, so the
        render's 404 called on_message_gone()."""
        hooks = []

        class _Panel(RenderableLayoutView):
            timeout = None
            auto_refresh_ephemeral = True

            async def on_message_gone(self):
                hooks.append("gone")

            async def on_message_delete(self):
                hooks.append("delete")
                await super().on_message_delete()

        old = _Panel(interaction=_make_interaction(user_id=1, guild_id=100))
        await old.send(ephemeral=True)
        message = old._message
        edit_started, edit_lands = asyncio.Event(), asyncio.Event()

        async def edit_finds_it_deleted(**kwargs):
            edit_started.set()
            await edit_lands.wait()
            raise discord.NotFound(MagicMock(status=404), "Unknown Message")

        async def deleted_but_the_reply_lost():
            edit_lands.set()
            raise aiohttp.ServerDisconnectedError()

        message.edit = AsyncMock(side_effect=edit_finds_it_deleted)
        message.delete = AsyncMock(side_effect=deleted_but_the_reply_lost)
        old._last_tree_digest = None
        render = asyncio.create_task(old.refresh())
        await asyncio.wait_for(edit_started.wait(), 5)

        await old._reopen_ephemeral(_make_interaction(user_id=1, guild_id=100))
        await asyncio.wait_for(render, 5)
        if old._message_gone_task is not None:
            await asyncio.wait_for(old._message_gone_task, 5)

        new = old.current_view
        assert new is not old and not new._torn_down()
        assert hooks == []

    async def test_a_bot_closing_during_the_delete_leaves_the_old_view_released(self, caplog):
        """The bot closed while the delete was in flight and the delete then
        failed: the old view, released by the close, got its message back,
        and its exit edited it through the closed client, logging an error."""
        bot = stub_client()

        class _Panel(RenderableLayoutView):
            timeout = None
            auto_refresh_ephemeral = True

        interaction = _make_interaction(user_id=1, guild_id=100)
        interaction.client = bot
        old = _Panel(interaction=interaction)
        await old.send(ephemeral=True)
        message = old._message
        closed = False

        async def refused_once_closed(**kwargs):
            if closed:
                await refused_by_closed_session()

        async def cut_off_by_the_close():
            nonlocal closed
            bot._closing_task = asyncio.get_running_loop().create_future()
            closed = True
            get_store()._release_views_of(bot)
            raise aiohttp.ServerDisconnectedError()

        message.edit = AsyncMock(side_effect=refused_once_closed)
        message.delete = AsyncMock(side_effect=cut_off_by_the_close)
        click = _make_interaction(user_id=1, guild_id=100)
        click.client = bot

        with caplog.at_level(logging.WARNING, logger="cascadeui"):
            await old._reopen_ephemeral(click)

        assert closed, "the delete never ran"
        assert old._message is None
        ours = [r for r in caplog.records if r.name.startswith("cascadeui")]
        assert [r.getMessage() for r in ours if r.levelno >= logging.WARNING] == []

    async def test_a_step_raising_after_the_replacement_is_live_still_hands_the_panel_on(
        self, caplog
    ):
        """A middleware raising on the undo history's carry stopped the
        Continue after its replacement went live: two live panels, the old
        message never deleted, and the old Continue button dropping every
        later click."""
        from cascadeui.state.middleware import UndoMiddleware

        store = get_store()
        undo = UndoMiddleware()
        await undo.initialize(store)
        store._add_middleware(undo)
        failing = False

        async def audit(action, state, next_fn):
            result = await next_fn(action, state)
            payload = action.get("payload") or {}
            if failing and action["type"] == "VIEW_UPDATED" and "undo_stack" in payload:
                raise RuntimeError("audit write failed")
            return result

        store._add_middleware(audit)

        class _Panel(RenderableLayoutView):
            timeout = None
            auto_refresh_ephemeral = True
            enable_undo = True

        old = _Panel(interaction=_make_interaction(user_id=1, guild_id=100))
        await old.send(ephemeral=True)
        await old.dispatch_scoped({"count": 1}, scope="user")
        message = old._message
        failing = True

        with caplog.at_level(logging.ERROR, logger="cascadeui"):
            await old._reopen_ephemeral(_make_interaction(user_id=1, guild_id=100))

        new = old.current_view
        assert new is not old and not new._torn_down()
        assert old._torn_down()
        message.delete.assert_awaited_once()
        assert any(
            "carrying the undo history over raised RuntimeError" in r.getMessage()
            for r in caplog.records
        )

    async def test_a_view_attached_during_the_delete_moves_to_the_replacement(self):
        """A view sent with parent= while the old message was deleted attached
        to the old view after its children had been carried, and that view's
        exit then closed it."""

        class _Panel(RenderableLayoutView):
            timeout = None
            auto_refresh_ephemeral = True

        old = _Panel(interaction=_make_interaction(user_id=1, guild_id=100))
        await old.send(ephemeral=True)
        children = []

        async def a_child_sent_meanwhile():
            child = RenderableLayoutView(
                interaction=_make_interaction(user_id=1, guild_id=100), parent=old
            )
            await child.send(ephemeral=True)
            children.append(child)

        old._message.delete = AsyncMock(side_effect=a_child_sent_meanwhile)

        await old._reopen_ephemeral(_make_interaction(user_id=1, guild_id=100))

        new = old.current_view
        (child,) = children
        assert not child._torn_down()
        assert child in new._attached_children

    async def test_a_continue_keeps_auto_registered_participants_under_an_instance_limit(self):
        """The replacement registers the view's other users while the old view
        still holds them, and the per-user instance limit counted the old
        view, so the replacement was refused on every Continue."""
        limited = []

        class _Shared(RenderableLayoutView):
            timeout = None
            auto_refresh_ephemeral = True
            instance_limit = 1
            instance_scope = "user_guild"
            auto_register_participants = True

            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.allowed_users = {1, 2}

            async def on_instance_limit(self, error):
                limited.append(error.blocked_user_id)

        old = _Shared(interaction=_make_interaction(user_id=1, guild_id=100))
        await old.send(ephemeral=True)
        assert 2 in old.participants

        await old._reopen_ephemeral(_make_interaction(user_id=1, guild_id=100))

        new = old.current_view
        assert new is not old and not new._torn_down()
        assert limited == []
        assert 2 in new.participants

    async def test_the_replacement_holds_the_panel_while_the_old_message_is_deleted(self):
        """The replacement, live during the delete, took the old view's place,
        attached views, and participants only after it: a child reading
        self.parent got the old view, and a push from the replacement left
        the participants behind."""
        replacements = []
        seen = {}

        class _Panel(RenderableLayoutView):
            timeout = None
            auto_refresh_ephemeral = True

            def build_reopen_view(self, interaction):
                view = super().build_reopen_view(interaction)
                replacements.append(view)
                return view

        old = _Panel(interaction=_make_interaction(user_id=1, guild_id=100))
        await old.send(ephemeral=True)
        await old.register_participant(2)
        child = RenderableLayoutView(
            interaction=_make_interaction(user_id=1, guild_id=100), parent=old
        )
        await child.send(ephemeral=True)

        async def look_meanwhile():
            seen.update(
                current=old.current_view,
                parent=child.parent,
                participants=set(replacements[0].participants),
            )

        old._message.delete = AsyncMock(side_effect=look_meanwhile)

        await old._reopen_ephemeral(_make_interaction(user_id=1, guild_id=100))

        (new,) = replacements
        assert seen["current"] is new
        assert seen["parent"] is new
        assert 2 in seen["participants"]

    async def test_a_replacement_closed_during_the_delete_closes_the_views_it_took(self):
        """An Exit on the replacement while the old message was deleted closed
        it before the attached views moved to it; they then moved onto the
        closed view and stayed live."""
        replacements = []

        class _Panel(RenderableLayoutView):
            timeout = None
            auto_refresh_ephemeral = True

            def build_reopen_view(self, interaction):
                view = super().build_reopen_view(interaction)
                replacements.append(view)
                return view

        old = _Panel(interaction=_make_interaction(user_id=1, guild_id=100))
        await old.send(ephemeral=True)
        child = RenderableLayoutView(
            interaction=_make_interaction(user_id=1, guild_id=100), parent=old
        )
        await child.send(ephemeral=True)

        async def the_replacement_closes_meanwhile():
            await replacements[0].exit()

        old._message.delete = AsyncMock(side_effect=the_replacement_closes_meanwhile)

        await old._reopen_ephemeral(_make_interaction(user_id=1, guild_id=100))

        assert replacements[0]._torn_down()
        assert child._torn_down()

    async def test_an_exit_override_closing_children_from_a_task_does_not_hang(self):
        """exit_children() waits for a Continue running on the view, and the
        Continue ran the old view's exit() inside that wait, so an override
        closing the children from a task of its own waited on itself and the
        click never finished."""

        class _Panel(RenderableLayoutView):
            timeout = None
            auto_refresh_ephemeral = True

            async def exit(self, delete_message=None):
                await asyncio.gather(self.exit_children())
                return await super().exit(delete_message=delete_message)

        old = _Panel(interaction=_make_interaction(user_id=1, guild_id=100))
        await old.send(ephemeral=True)

        await asyncio.wait_for(old._reopen_ephemeral(_make_interaction(user_id=1, guild_id=100)), 5)

        assert old._torn_down()

    async def test_a_bot_closed_before_the_delete_logs_no_error(self, caplog):
        """The delete refused by a client that had closed was logged as an
        error, though closing the bot is not a failure."""
        bot = stub_client()

        class _Panel(RenderableLayoutView):
            timeout = None
            auto_refresh_ephemeral = True

        interaction = _make_interaction(user_id=1, guild_id=100)
        interaction.client = bot
        old = _Panel(interaction=interaction)
        await old.send(ephemeral=True)
        message = old._message

        async def refused_by_the_closed_client(**kwargs):
            await refused_by_closed_session()

        async def the_bot_closed_first():
            bot._closing_task = asyncio.get_running_loop().create_future()
            await refused_by_closed_session()

        message.edit = AsyncMock(side_effect=refused_by_the_closed_client)
        message.delete = AsyncMock(side_effect=the_bot_closed_first)
        click = _make_interaction(user_id=1, guild_id=100)
        click.client = bot

        with caplog.at_level(logging.DEBUG, logger="cascadeui"):
            await old._reopen_ephemeral(click)

        assert message.delete.await_count == 1
        ours = [r for r in caplog.records if r.name.startswith("cascadeui")]
        assert [r.getMessage() for r in ours if r.levelno >= logging.ERROR] == []

    async def test_a_push_from_code_during_the_continue_waits_and_refuses(self):
        """A push() from code landing while the replacement was sent committed
        too: two live panels, current_view on the replacement while the pushed
        screen held the attached view, on an ephemeral message about to lose
        its token."""
        loading = None

        class _Panel(RenderableLayoutView):
            timeout = None
            auto_refresh_ephemeral = True

            async def on_load(self):
                if loading is not None:
                    await loading.wait()

        old = _Panel(interaction=_make_interaction(user_id=1, guild_id=100))
        await old.send(ephemeral=True)
        child = RenderableLayoutView(
            interaction=_make_interaction(user_id=1, guild_id=100), parent=old
        )
        await child.send(ephemeral=True)
        loading = asyncio.Event()
        continuing = asyncio.create_task(old._reopen_ephemeral(_make_interaction(user_id=1)))
        for _ in range(20):
            await asyncio.sleep(0)
        pushing = asyncio.create_task(old.push(RenderableLayoutView))
        for _ in range(20):
            await asyncio.sleep(0)
        assert not pushing.done(), "the push did not wait for the Continue"
        loading.set()
        await asyncio.wait_for(continuing, 5)
        with pytest.raises(RuntimeError, match="handed its panel on"):
            await asyncio.wait_for(pushing, 5)

        new = old.current_view
        assert new is not old and not new.is_finished()
        assert child in new._attached_children
        live = [v for v in old.state_store._active_views.values() if not v.is_finished()]
        assert set(live) == {new, child}

    async def test_navigation_from_inside_the_continue_does_not_wait_on_itself(self):
        """build_reopen_view() runs in the Continue's own task, so a push or pop
        it starts must not wait for the Continue to finish."""
        waited = []

        class _Panel(RenderableLayoutView):
            timeout = None
            auto_refresh_ephemeral = True

            async def build_reopen_view(self, interaction):
                # Awaited directly: before 3.12 a wait_for here would run the
                # call in a task of its own, which is not the Continue's.
                await self._wait_to_navigate("push()")
                waited.append(True)
                return await super().build_reopen_view(interaction)

        old = _Panel(interaction=_make_interaction(user_id=1, guild_id=100))
        await old.send(ephemeral=True)
        # The bound for a regression, which would wait on the Continue for good.
        await asyncio.wait_for(old._reopen_ephemeral(_make_interaction(user_id=1)), 5)

        assert waited == [True], "the navigation waited on the Continue it runs in"

    async def test_a_close_held_by_a_stuck_continue_is_logged(self, monkeypatch, caplog):
        """A close waiting on a Continue whose replacement never finished
        loading waited in silence, with the panel refusing every click."""
        from cascadeui.views import _navigation

        monkeypatch.setattr(_navigation, "_NAVIGATION_WAIT_WARN_SECONDS", 0.01)
        caplog.set_level(logging.WARNING, logger="cascadeui")
        loading = None

        class _Panel(RenderableLayoutView):
            timeout = None
            auto_refresh_ephemeral = True

            async def on_load(self):
                if loading is not None:
                    await loading.wait()

        old = _Panel(interaction=_make_interaction(user_id=1, guild_id=100))
        await old.send(ephemeral=True)
        loading = asyncio.Event()
        continuing = asyncio.create_task(old._reopen_ephemeral(_make_interaction(user_id=1)))
        for _ in range(20):
            await asyncio.sleep(0)
        closing = asyncio.create_task(old.exit())
        try:
            await until(
                lambda: any(
                    "waited" in r.getMessage() and "Continue" in r.getMessage()
                    for r in caplog.records
                    if r.name.startswith("cascadeui")
                ),
                timeout=2,
            )
        except asyncio.TimeoutError:
            pass
        finally:
            loading.set()
        await asyncio.wait_for(asyncio.gather(continuing, closing), 5)

        assert any(
            "waited" in r.getMessage() and "Continue" in r.getMessage()
            for r in caplog.records
            if r.name.startswith("cascadeui")
        ), "the close waited on the Continue without a word"

    async def test_a_navigation_held_by_a_stuck_continue_is_logged_as_itself(
        self, monkeypatch, caplog
    ):
        """A push() waiting on a Continue was logged as a close, naming neither
        the call nor the cycle that hangs it."""
        from cascadeui.views import _navigation

        monkeypatch.setattr(_navigation, "_NAVIGATION_WAIT_WARN_SECONDS", 0.01)
        caplog.set_level(logging.WARNING, logger="cascadeui")
        loading = None

        class _Panel(RenderableLayoutView):
            timeout = None
            auto_refresh_ephemeral = True

            async def on_load(self):
                if loading is not None:
                    await loading.wait()

        old = _Panel(interaction=_make_interaction(user_id=1, guild_id=100))
        await old.send(ephemeral=True)
        loading = asyncio.Event()
        continuing = asyncio.create_task(old._reopen_ephemeral(_make_interaction(user_id=1)))
        for _ in range(20):
            await asyncio.sleep(0)
        pushing = asyncio.create_task(old.push(RenderableLayoutView))

        def waited():
            return [
                r.getMessage()
                for r in caplog.records
                if r.name.startswith("cascadeui") and "for the view's Continue" in r.getMessage()
            ]

        try:
            await until(lambda: waited(), timeout=2)
        except asyncio.TimeoutError:
            pass
        finally:
            loading.set()
        await asyncio.wait_for(continuing, 5)
        with pytest.raises(RuntimeError, match="handed its panel on"):
            await asyncio.wait_for(pushing, 5)

        assert waited() and waited()[0].startswith(
            "push() on _Panel has waited"
        ), f"the push's wait was not logged as the push: {waited()}"

    async def test_a_library_exit_waiting_on_the_continue_is_not_warned_about(self, caplog):
        """The Continue cleared the library-exit mark in its finally, so an
        instance-limit replacement already waiting on the Continue logged the
        handed-over warning for the library's own exit."""
        from discord.ui import TextDisplay

        caplog.set_level(logging.WARNING, logger="cascadeui")
        loading = None

        def _sent_on(message_id):
            interaction = _make_interaction(user_id=1, guild_id=100)
            message = MagicMock(id=message_id, channel=MagicMock(id=888))
            message.edit = AsyncMock(return_value=message)
            message.delete = AsyncMock()
            interaction.original_response = AsyncMock(return_value=message)
            return interaction

        class _Limited(StatefulLayoutView):
            auto_refresh_ephemeral = True
            auto_defer_delay = 0.05
            instance_limit = 1
            instance_policy = "replace"
            instance_scope = "user"

            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.add_item(TextDisplay("panel"))

            async def on_load(self):
                nonlocal loading
                if loading is not None:
                    gate, loading = loading, None
                    await gate.wait()

        old = _Limited(interaction=_sent_on(1000), timeout=3000)
        await old.send(ephemeral=True)
        gate = loading = asyncio.Event()
        continuing = asyncio.create_task(old._reopen_ephemeral(_sent_on(1002)))
        for _ in range(20):
            await asyncio.sleep(0)
        fresh = _Limited(interaction=_sent_on(1003), timeout=3000)
        opening = asyncio.create_task(fresh.send(ephemeral=True))
        for _ in range(20):
            await asyncio.sleep(0)
        gate.set()
        await asyncio.wait_for(asyncio.gather(continuing, opening), 5)

        assert not fresh.is_finished() and old.is_finished()
        handed = [
            r.getMessage()
            for r in caplog.records
            if r.name.startswith("cascadeui") and "handed its panel on" in r.getMessage()
        ]
        assert handed == [], f"the library's own exit was warned about: {handed}"


class TestReopenLinkSurvivesARaisingExit:
    async def test_the_link_holds_when_the_old_views_exit_raises(self, caplog):
        """The link was set after the old view's exit(), so an override that
        raised left code holding the old view no way to reach the live one.
        The raise is logged, since the replacement is already live."""

        class _Panel(RenderableLayoutView):
            timeout = None
            auto_refresh_ephemeral = True
            fail_exit = False

            async def exit(self, delete_message=None):
                if self.fail_exit:
                    raise RuntimeError("exit failed")
                return await super().exit(delete_message=delete_message)

        old = _Panel(interaction=_make_interaction(user_id=1, guild_id=100))
        await old.send(ephemeral=True)
        old.fail_exit = True

        with caplog.at_level(logging.ERROR, logger="cascadeui"):
            await old._reopen_ephemeral(_make_interaction(user_id=1, guild_id=100))

        new = old.current_view
        assert new is not old and not new.is_finished()
        assert any(
            "closing the view it replaced raised RuntimeError: exit failed" in r.getMessage()
            for r in caplog.records
        )


class TestReopenFactoryReturningTheViewItself:
    async def test_the_second_continue_is_refused(self):
        """The factory rides onto the replacement, so one returning a fixed
        instance hands back the view itself on the next Continue: the view
        became its own successor, and every reader of the link spun forever."""

        class _Panel(RenderableLayoutView):
            timeout = None
            auto_refresh_ephemeral = True

        old = _Panel(interaction=_make_interaction(user_id=1, guild_id=100))
        await old.send(ephemeral=True)
        new = _Panel(interaction=_make_interaction(user_id=1, guild_id=100))
        old.build_reopen_view = lambda interaction: new
        await old._reopen_ephemeral(_make_interaction(user_id=1, guild_id=100))
        failures = []

        async def on_reopen_failure(interaction, error=None):
            failures.append(error)

        new.on_reopen_failure = on_reopen_failure
        new.build_reopen_view = lambda interaction: new
        await new._reopen_ephemeral(_make_interaction(user_id=1, guild_id=100))

        # Read before anything follows the link, so a cycle fails here rather
        # than hanging the suite.
        assert new._successor is None
        assert isinstance(failures[0], RuntimeError)
        assert not new.is_finished()
        assert old.current_view is new


class TestReopenRefusesWhatItCannotSend:
    """build_reopen_view() returning something other than a new CascadeUI view
    crashed inside the library naming neither the hook nor the fix, left every
    later Continue click unanswered, or took over another live view."""

    async def _reopen_with(self, make_result):
        class _Panel(RenderableLayoutView):
            timeout = None
            auto_refresh_ephemeral = True

        view = _Panel(interaction=_make_interaction(user_id=1, guild_id=100))
        await view.send(ephemeral=True)
        failures = []

        async def on_reopen_failure(interaction, error=None):
            failures.append(error)

        view.on_reopen_failure = on_reopen_failure
        view.build_reopen_view = lambda interaction: make_result(_Panel)
        await view._reopen_ephemeral(_make_interaction(user_id=1, guild_id=100))
        return view, failures

    @pytest.mark.parametrize(
        "make_result, match",
        [
            (lambda cls: cls, "returned the class _Panel, not a CascadeUI view"),
            (lambda cls: discord.ui.LayoutView(), "returned a LayoutView, not a CascadeUI view"),
        ],
        ids=["the-class", "a-plain-discord-view"],
    )
    async def test_a_result_that_is_not_a_view_instance_is_refused(self, make_result, match):
        view, failures = await self._reopen_with(make_result)

        assert len(failures) == 1 and match in str(failures[0])
        assert view._reopen_in_flight is False
        assert not view.is_finished()

    async def test_a_live_view_is_refused_and_left_as_it_was(self):
        live = {}

        def make_result(cls):
            return live["hub"]

        hub = RenderableLayoutView(interaction=_make_interaction(user_id=1, guild_id=100))
        await hub.send()
        live["hub"] = hub
        session_before = hub.session_id

        view, failures = await self._reopen_with(make_result)

        assert len(failures) == 1 and "already sent, pushed, or closed" in str(failures[0])
        assert hub.session_id == session_before
        assert hub._nav_stack == []
        assert view._reopen_in_flight is False


class TestReopenShowsTheDataOnScreen:
    """The rebuild replays the constructor's kwargs, which held the data the
    view was built with, not the data a refresh swapped in since."""

    @staticmethod
    def _texts(view):
        return [c.content for c in view.walk_children() if isinstance(c, TextDisplay)]

    async def test_continue_after_refresh_data_keeps_the_new_items(self):
        from cascadeui import PaginatedLayoutView

        class _Pages(PaginatedLayoutView):
            timeout = None
            auto_refresh_ephemeral = True

        def fmt(chunk):
            return [TextDisplay(", ".join(chunk))]

        view = await _Pages.from_data(
            ["old-a", "old-b"], 1, fmt, interaction=_make_interaction(user_id=1, guild_id=100)
        )
        await view.send(ephemeral=True)
        await view.refresh_data(["new-a", "new-b", "new-c"])

        await view._reopen_ephemeral(_make_interaction(user_id=1, guild_id=100))

        new = view.current_view
        assert new is not view
        assert len(new.pages) == 3
        assert self._texts(new)[0] == "new-a"

    async def test_continue_after_refresh_pages_keeps_the_new_total(self):
        from cascadeui import PaginatedLayoutView

        class _Pages(PaginatedLayoutView):
            timeout = None
            auto_refresh_ephemeral = True

        async def fetch(offset, limit):
            return [f"row-{i}" for i in range(offset, offset + limit)]

        def fmt(chunk):
            return [TextDisplay(", ".join(chunk))]

        view = _Pages.from_cursor(
            fetch,
            total=2,
            per_page=1,
            formatter=fmt,
            interaction=_make_interaction(user_id=1, guild_id=100),
        )
        await view.send(ephemeral=True)
        await view.refresh_pages(new_total=5)

        await view._reopen_ephemeral(_make_interaction(user_id=1, guild_id=100))

        new = view.current_view
        assert new is not view
        assert len(new.pages) == 5


class TestReopenCleanupOfOldMessage:
    """The old panel is deleted when the token still allows it; otherwise it
    is closed once, as a view sent again closes the message it left."""

    class _Refreshable(StatefulLayoutView):
        auto_refresh_ephemeral = True

        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.add_item(ActionRow(StatefulButton(label="Continue", custom_id="continue")))

    def _old_and_new(self, delete_side_effect=None):
        old = self._Refreshable(interaction=_make_interaction())
        # The armed panel already rendered its Continue button.
        old._has_rendered = True
        old._last_tree_digest = old._compute_tree_digest()
        message = MagicMock()
        message.delete = AsyncMock(side_effect=delete_side_effect)
        message.edit = AsyncMock()
        old._message = message

        new = self._Refreshable(interaction=_make_interaction())
        old.build_reopen_view = lambda interaction: new
        new.send = AsyncMock()
        return old, message

    async def test_deleted_old_message_is_not_edited_afterwards(self):
        old, message = self._old_and_new()

        await old._reopen_ephemeral(_make_interaction())

        message.delete.assert_awaited_once()
        message.edit.assert_not_called()

    async def test_already_missing_old_message_is_not_edited(self):
        old, message = self._old_and_new(
            delete_side_effect=discord.NotFound(MagicMock(status=404), "gone")
        )

        await old._reopen_ephemeral(_make_interaction())

        message.edit.assert_not_called()

    async def test_undeletable_old_message_is_frozen_with_one_edit(self):
        """Past the token window the delete fails. One edit ships, and it is
        the frozen panel, not the live one followed by a second freeze."""
        old, message = self._old_and_new(
            delete_side_effect=discord.HTTPException(MagicMock(status=401), "expired")
        )

        await old._reopen_ephemeral(_make_interaction())

        message.edit.assert_awaited_once()
        shipped = message.edit.await_args.kwargs["view"]
        buttons = [i for i in shipped.walk_children() if isinstance(i, discord.ui.Button)]
        assert buttons and all(b.disabled for b in buttons)

    async def test_a_failed_delete_freezes_under_a_delete_exit_policy(self):
        """Under exit_policy = "delete" a failed delete was sent again, which
        after a server error repeats discord.py's retries and, failing again,
        left the old Continue button looking live."""
        old, message = self._old_and_new(
            delete_side_effect=discord.HTTPException(MagicMock(status=503), "unavailable")
        )
        old.set_class_attribute("exit_policy", "delete")

        await old._reopen_ephemeral(_make_interaction())

        message.delete.assert_awaited_once()
        message.edit.assert_awaited_once()
        shipped = message.edit.await_args.kwargs["view"]
        buttons = [i for i in shipped.walk_children() if isinstance(i, discord.ui.Button)]
        assert buttons and all(b.disabled for b in buttons)

    async def test_a_failed_delete_leaves_the_old_view_without_its_message(self):
        """The old view lets go of its message before the delete and does not
        take it back when the delete fails: the message is frozen once, and a
        later render of the old view reports it closed."""
        old, message = self._old_and_new(
            delete_side_effect=discord.HTTPException(MagicMock(status=500), "unavailable")
        )

        await old._reopen_ephemeral(_make_interaction())
        old.add_item(ActionRow(StatefulButton(label="Late", custom_id="late")))

        assert old._message is None
        assert await old.refresh() is RenderOutcome.CLOSED
        message.edit.assert_awaited_once()

    async def test_a_continue_cut_off_during_the_delete_still_hands_the_panel_on(self):
        """A Continue cancelled while its delete is in flight finishes the
        hand-over before the cancel goes through, since the replacement is
        live: the old view closes, and the old message is frozen."""
        old, message = self._old_and_new()
        deleting = asyncio.Event()

        async def delete_in_flight():
            deleting.set()
            await asyncio.Event().wait()

        message.delete = AsyncMock(side_effect=delete_in_flight)
        continuing = asyncio.create_task(old._reopen_ephemeral(_make_interaction()))
        await asyncio.wait_for(deleting.wait(), 5)
        continuing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await continuing

        assert old.current_view is not old
        assert old.is_finished()
        message.edit.assert_awaited_once()


# // ========================================( Refresh Timer )======================================== // #


class TestTheRefreshTimerWaitsWithoutATask:
    """The timer that arms the refresh button waited in a task asleep until
    its deadline. A send finishing after its cancel while the program ended
    scheduled one during asyncio's shutdown, which then reported the task
    destroyed while still pending."""

    async def test_a_scheduled_timer_leaves_no_task_waiting(self):
        class _View(RenderableLayoutView):
            timeout = None

        view = _View(interaction=_make_interaction())
        await view.send(ephemeral=True)
        try:
            assert len(refresh_timers(view)) == 1
            assert view.task_manager.get_task_count(view.id) == 0
        finally:
            view.task_manager.cancel_tasks(view.id)

    async def test_exit_cancels_the_timer(self):
        class _View(RenderableLayoutView):
            timeout = None

        view = _View(interaction=_make_interaction())
        await view.send(ephemeral=True)
        await until(lambda: refresh_timers(view))
        [timer] = refresh_timers(view)

        await view.exit()

        assert timer.cancelled()
        assert refresh_timers(view) == []


# // ========================================( Ephemeral Flag Reset On Send )======================================== // #


class TestEphemeralFlagResetOnSend:
    """send() sets _ephemeral to match the current send, so a reused instance
    does not carry a stale flag from an earlier ephemeral context.
    """

    async def test_non_ephemeral_send_clears_stale_flag(self):
        """A view carrying a stale _ephemeral=True, sent non-ephemerally,
        ends with the flag cleared -- otherwise its refreshes take the
        ephemeral no-op branch and exit misclassifies real 401s.
        """
        interaction = _make_interaction()
        view = RenderableLayoutView(interaction=interaction)
        view._ephemeral = True

        await view.send(ephemeral=False)

        assert view._ephemeral is False

    async def test_ephemeral_send_sets_flag(self):
        """The ephemeral path still sets the flag."""
        interaction = _make_interaction()
        view = RenderableLayoutView(interaction=interaction)
        assert view._ephemeral is False

        await view.send(ephemeral=True)

        assert view._ephemeral is True

    async def test_public_re_send_stops_a_waiting_timer_from_arming(self):
        """A timer waiting when the same instance is re-sent publicly must not
        arm when it fires. The public re-send cancels no timer, and the stale
        True resolution keeps the effective handoff True, so the policy
        backstop would arm the reopen button over the live public panel. The
        send clears the pending deadline and reassigns the flag, and either
        records that the managed message changed.

        The timer is registered during the first send, while the instance
        still manages the ephemeral send, so at its deadline the flag check
        is the gate that must decline.
        """

        class _View(RenderableLayoutView):
            timeout = None  # first send resolves the handoff True
            refresh_warning_seconds = 899  # arm deadline = send + max(1, 900 - 899) = ~1s

        view = _View(interaction=_make_interaction())
        captured = []
        real_schedule = view._schedule_ephemeral_refresh

        def _capture():
            captured.append(True)
            real_schedule()

        with patch.object(view, "_schedule_ephemeral_refresh", side_effect=_capture):
            await view.send(ephemeral=True)

        try:
            assert view._ephemeral is True
            assert view._refresh_handoff is True
            assert len(captured) == 1

            # The send's schedule passed the entry backstop (the policy is
            # engaged, the flag is True) and the timer waits for its deadline.
            timers = refresh_timers(view)
            assert len(timers) == 1, "the entry backstop declined a timer the send engaged"
            timer = timers[0]

            view.interaction = _make_interaction()
            await view.send(ephemeral=False)

            # The re-send flipped only the flag. The stale resolution keeps
            # the effective policy True -- the exact value the policy
            # backstop reads -- so it cannot be the gate that declines.
            assert view._ephemeral is False
            assert view._refresh_handoff_resolved is True
            assert view._refresh_handoff is True
            assert view._ephemeral_arm_deadline is None

            # The second send neither cancelled nor replaced the first
            # timer; it is still waiting with ~1s left on the original
            # deadline. A fired timer here means the interleaving under test
            # was never reached.
            assert not timer.cancelled()
            assert timer in refresh_timers(
                view
            ), "harness: the timer fired before the second send finished"

            # Every guard other than the flag check is clear going into the
            # deadline, so the flag check is the deciding gate.
            assert not view.is_finished()
            assert view._refresh_armed is False
            assert view._message is not None

            public_message = view._message
            try:
                await wait_for_timer(view, timer)
            except asyncio.TimeoutError:
                pytest.fail("the ephemeral refresh timer never reached its deadline")

            # Still clear after it fired, and no edit reached the public message.
            assert not view.is_finished()
            assert view._message is not None
            assert view._refresh_armed is False
            public_message.edit.assert_not_awaited()
        finally:
            view.task_manager.cancel_tasks(view.id)

    @staticmethod
    def _gated_view():
        """A view that arms ~1s after an ephemeral send, and whose on_load
        waits on ``gate`` once one is set."""

        class _View(RenderableLayoutView):
            timeout = None
            refresh_warning_seconds = 899
            auto_refresh_ephemeral = True
            gate = None

            async def on_load(self):
                if type(self).gate is not None:
                    await type(self).gate.wait()

        return _View

    async def test_an_ephemeral_re_send_stops_a_timer_that_fires_during_it(self):
        """The timer fired while the re-send was loading, and the re-send's new
        deadline is stamped only after its message exists, so the old one
        still matched. The timer armed while the send delivered, and the
        brand-new message shipped as nothing but a refresh button."""
        view_cls = self._gated_view()
        view = view_cls(interaction=_make_interaction())
        await view.send(ephemeral=True)
        view_cls.gate = asyncio.Event()
        view.interaction = _make_interaction()
        resend = asyncio.create_task(view.send(ephemeral=True))
        try:
            await asyncio.sleep(1.3)  # the first send's timer fires mid re-send
            view_cls.gate.set()
            await asyncio.wait_for(resend, timeout=2)
            await asyncio.sleep(0.05)

            assert view._refresh_armed is False
        finally:
            view.task_manager.cancel_tasks(view.id)

    @pytest.mark.parametrize("ephemeral", [False, True], ids=["public", "ephemeral"])
    async def test_a_timer_queued_for_the_turn_stands_down_for_a_re_send(self, ephemeral):
        """The timer passed its checks and then waited for the reload turn, and
        a re-send began meanwhile. Checked only before the wait, it armed
        over the view the re-send was about to ship."""
        view_cls = self._gated_view()
        view = view_cls(interaction=_make_interaction())
        await view.send(ephemeral=True)
        view_cls.gate = asyncio.Event()
        reload = asyncio.create_task(view.reload())  # holds the turn past the deadline
        try:
            await asyncio.sleep(1.3)  # the timer fired and queued for the turn
            view.interaction = _make_interaction()
            resend = asyncio.create_task(view.send(ephemeral=ephemeral))
            await asyncio.sleep(0)
            view_cls.gate.set()
            await asyncio.wait_for(asyncio.gather(reload, resend), timeout=2)
            await asyncio.sleep(0.05)

            assert view._refresh_armed is False
        finally:
            view.task_manager.cancel_tasks(view.id)


class TestArmDeadlineStampedOnEveryEphemeralSend:
    """The arming deadline records the send token's 900s window (a fact
    about the message), so every ephemeral send stamps it. Whether the
    handoff timer runs stays ``auto_refresh_ephemeral``'s call: stamping by
    itself schedules nothing.
    """

    async def test_declined_send_stamps_deadline_without_scheduling(self):
        runs = []

        class _Declined(RenderableLayoutView):
            auto_refresh_ephemeral = False

            def _schedule_ephemeral_refresh(self):
                runs.append(True)

        view = _Declined(interaction=_make_interaction())
        before = time.monotonic()
        await view.send(ephemeral=True)
        after = time.monotonic()
        for _ in range(3):
            await asyncio.sleep(0)

        # 810 = 900 - refresh_warning_seconds (default 90), bracketed
        # between clock readings taken either side of the send.
        assert view._ephemeral_arm_deadline is not None
        assert before + 810 <= view._ephemeral_arm_deadline <= after + 810
        assert runs == []

    async def test_engaged_send_stamps_deadline_and_schedules(self):
        runs = []

        class _Engaged(RenderableLayoutView):
            auto_refresh_ephemeral = True

            def _schedule_ephemeral_refresh(self):
                runs.append(True)

        view = _Engaged(interaction=_make_interaction())
        await view.send(ephemeral=True)
        for _ in range(3):
            await asyncio.sleep(0)

        assert view._ephemeral_arm_deadline is not None
        assert runs == [True]

    async def test_non_ephemeral_send_stamps_nothing(self):
        view = RenderableLayoutView(interaction=_make_interaction())
        await view.send(ephemeral=False)

        assert view._ephemeral_arm_deadline is None

    async def test_ephemeral_re_send_cancels_the_first_sends_waiting_timer(self):
        """A same-instance ephemeral re-send stamps a new token's clock and
        schedules its own timer. The first send's timer was left waiting,
        holding the view until the old deadline, where only the deadline
        comparison kept it from arming; the re-send now cancels it, and its
        own timer arms on the new deadline.
        """

        class _View(RenderableLayoutView):
            timeout = None  # both sends resolve the handoff True
            refresh_warning_seconds = 899  # first arm deadline = send + ~1s

        view = _View(interaction=_make_interaction())
        await view.send(ephemeral=True)
        [timer1] = refresh_timers(view)
        first_deadline = view._ephemeral_arm_deadline
        view.set_class_attribute("refresh_warning_seconds", 898)
        view.interaction = _make_interaction()
        await view.send(ephemeral=True)

        try:
            [timer2] = refresh_timers(view)
            assert timer2 is not timer1
            assert timer1.cancelled()
            assert view._ephemeral_arm_deadline != first_deadline

            second_message = view._message
            try:
                await wait_for_timer(view, timer2)
            except asyncio.TimeoutError:
                pytest.fail("the second send's timer never reached its deadline")
            assert view._refresh_armed is True
            assert second_message.edit.await_count == 1
        finally:
            view.task_manager.cancel_tasks(view.id)

    async def test_a_refused_re_send_restarts_a_timer_that_came_due_during_it(self):
        """The re-send clears the deadline, so a timer due while it ran stood
        down. Refused, the send put the deadline back and started nothing, and
        the live panel reached its token's end with no Continue button."""
        due = []

        class _View(RenderableLayoutView):
            timeout = None
            refresh_warning_seconds = 899  # arm deadline = send + ~1s

            async def on_pre_send(self, interaction):
                if not due:
                    return True
                await wait_for_timer(self, due[0])
                return False

        view = _View(interaction=_make_interaction())
        await view.send(ephemeral=True)
        [timer] = refresh_timers(view)
        due.append(timer)
        view.interaction = _make_interaction()

        try:
            assert await view.send(ephemeral=True) is None
            timers = refresh_timers(view)
            assert len(timers) == 1, "the refused re-send left the panel with no refresh timer"
            await wait_for_timer(view, timers[0])
            assert view._refresh_armed is True
        finally:
            view.task_manager.cancel_tasks(view.id)

    async def test_a_view_kept_after_its_message_went_is_not_armed(self):
        """The timer armed a view whose message had gone and whose
        on_message_delete() kept it to post again: the Continue button took
        its tree for good, and an armed view takes no renders, so the panel
        posted again showed only that button."""
        kept = []

        class _View(RenderableLayoutView):
            timeout = None
            refresh_warning_seconds = 899  # arm deadline = send + ~1s

            async def on_message_delete(self):
                kept.append(self)

        view = _View(interaction=_make_interaction())
        await view.send(ephemeral=True)
        timers = refresh_timers(view)
        assert len(timers) == 1
        children = list(view.children)
        response = MagicMock(status=404, reason="Not Found")
        view._message.edit = AsyncMock(side_effect=discord.NotFound(response, "Unknown Message"))
        view._last_tree_digest = None

        try:
            await view.refresh()
            await until(lambda: kept)
            assert view._message is None and not view.is_finished()
            await wait_for_timer(view, timers[0])
            assert view._refresh_armed is False
            assert view.children == children
        finally:
            view.task_manager.cancel_tasks(view.id)


# // ========================================( Declaration vs Resolution )======================================== // #


class TestDeclarationSeparateFromResolution:
    """``auto_refresh_ephemeral`` is the author's declaration and the library
    never assigns to it. The ``None`` sentinel resolves into the private
    ``_refresh_handoff_resolved`` at each ephemeral send, so introspecting
    the declaration always returns what the class or the caller set, and a
    later send re-derives instead of carrying a stale answer.
    """

    async def test_derived_engagement_leaves_declaration_unset(self):
        runs = []

        class _View(RenderableLayoutView):
            timeout = None

            def _schedule_ephemeral_refresh(self):
                runs.append(True)

        view = _View(interaction=_make_interaction())
        await view.send(ephemeral=True)
        for _ in range(3):
            await asyncio.sleep(0)

        assert view.auto_refresh_ephemeral is None
        assert view._refresh_handoff_resolved is True
        assert runs == [True]

    async def test_derived_decline_leaves_declaration_unset(self):
        runs = []

        class _View(RenderableLayoutView):
            timeout = 300

            def _schedule_ephemeral_refresh(self):
                runs.append(True)

        view = _View(interaction=_make_interaction())
        await view.send(ephemeral=True)
        for _ in range(3):
            await asyncio.sleep(0)

        assert view.auto_refresh_ephemeral is None
        assert view._refresh_handoff_resolved is False
        assert runs == []

    async def test_explicit_engagement_records_no_resolution(self):
        """An explicit pin is its own answer. The resolution field records
        only what the library decided, which for a pin is nothing.
        """

        class _On(RenderableLayoutView):
            auto_refresh_ephemeral = True
            timeout = 300

        view = _On(interaction=_make_interaction())
        await view.send(ephemeral=True)
        try:
            assert view.auto_refresh_ephemeral is True
            assert view._refresh_handoff_resolved is None
            assert view._refresh_handoff is True
        finally:
            view.task_manager.cancel_tasks(view.id)

    async def test_explicit_decline_records_no_resolution(self):
        class _Off(RenderableLayoutView):
            auto_refresh_ephemeral = False
            timeout = None

        view = _Off(interaction=_make_interaction())
        await view.send(ephemeral=True)

        assert view.auto_refresh_ephemeral is False
        assert view._refresh_handoff_resolved is None
        assert view._refresh_handoff is False

    async def test_non_ephemeral_send_resolves_nothing(self):
        view = RenderableLayoutView(interaction=_make_interaction())
        await view.send(ephemeral=False)

        assert view.auto_refresh_ephemeral is None
        assert view._refresh_handoff_resolved is None
        assert view._refresh_handoff is None

    async def test_re_send_re_derives_from_the_current_timeout(self):
        """The resolution is per-send, not per-instance. A second ephemeral
        send derives against the timeout as it stands then, where writing
        the answer onto the declaration froze the first derivation forever.
        """
        runs = []

        class _View(RenderableLayoutView):
            timeout = None

            def _schedule_ephemeral_refresh(self):
                runs.append(True)

        view = _View(interaction=_make_interaction())
        await view.send(ephemeral=True)
        assert view._refresh_handoff_resolved is True

        view.timeout = 300
        view.interaction = _make_interaction()
        await view.send(ephemeral=True)
        for _ in range(3):
            await asyncio.sleep(0)

        assert view._refresh_handoff_resolved is False
        assert view.auto_refresh_ephemeral is None
        assert runs == [True]  # only the first send engaged

    async def test_second_send_decline_stops_a_waiting_timer_from_arming(self):
        """A timer already waiting when a second send re-derives the handoff
        to False must not arm when it fires. The declaration is still None
        at that point (only the resolution flipped), so a check at the deadline
        that reads ``auto_refresh_ephemeral`` finds None and arms a view
        whose effective policy is False; the check must read
        ``_refresh_handoff``.

        The timer is registered during the first send, while the policy is
        still engaged, so the check at the deadline decides rather than the
        schedule's entry backstop. A timer scheduled after the re-derivation
        would be declined at entry, a false green for the branch this names.
        """

        class _View(RenderableLayoutView):
            timeout = None  # first send resolves the handoff True
            refresh_warning_seconds = 899  # arm deadline = send + max(1, 900 - 899) = ~1s

        view = _View(interaction=_make_interaction())
        captured = []
        real_schedule = view._schedule_ephemeral_refresh

        def _capture():
            captured.append(True)
            real_schedule()

        with patch.object(view, "_schedule_ephemeral_refresh", side_effect=_capture):
            await view.send(ephemeral=True)

        try:
            assert view._refresh_handoff_resolved is True
            assert view._refresh_handoff is True
            assert len(captured) == 1

            # The send's schedule passed the entry backstop (the policy is
            # engaged) and the timer waits for its deadline. No timer here
            # would mean the entry backstop declined -- the wrong gate.
            timers = refresh_timers(view)
            assert len(timers) == 1, "the entry backstop declined a timer the send engaged"
            timer = timers[0]

            view.timeout = 300
            view.interaction = _make_interaction()
            await view.send(ephemeral=True)

            # The re-derivation flipped only the resolution. The unset
            # declaration is the exact value the pre-fix check at the deadline
            # read: None is not False, so it armed.
            assert view.auto_refresh_ephemeral is None
            assert view._refresh_handoff_resolved is False
            assert view._refresh_handoff is False

            # The second send neither cancelled nor replaced the first
            # timer; it is still waiting with ~1s left on the original
            # deadline. A fired timer here means the interleaving under test
            # was never reached.
            assert not timer.cancelled()
            assert timer in refresh_timers(
                view
            ), "harness: the timer fired before the second send finished"

            # Every guard ahead of the policy check is clear going into the
            # deadline, so the policy check is the deciding gate.
            assert not view.is_finished()
            assert view._refresh_armed is False
            assert view._message is not None

            try:
                await wait_for_timer(view, timer)
            except asyncio.TimeoutError:
                pytest.fail("the ephemeral refresh timer never reached its deadline")

            # Still clear after it fired: every gate ahead of the policy
            # check passed, so the policy check declined first. The re-send
            # also restamped the deadline, so the identity comparison after
            # it would have declined too -- these assertions establish
            # first, not sole.
            assert not view.is_finished()
            assert view._message is not None
            assert view._refresh_armed is False
        finally:
            view.task_manager.cancel_tasks(view.id)

    async def test_a_pin_after_send_overrides_a_derived_engagement(self):
        """``set_class_attribute`` writes the declaration, and both of the
        timer's ``is False`` backstops read the declaration -- so a pin
        applied after send wins over the derived engagement without
        disturbing the resolution record.
        """

        class _View(RenderableLayoutView):
            timeout = None

        view = _View(interaction=_make_interaction())
        await view.send(ephemeral=True)
        view.task_manager.cancel_tasks(view.id)
        assert view.auto_refresh_ephemeral is None
        assert view._refresh_handoff is True

        view.set_class_attribute("auto_refresh_ephemeral", False)

        assert view._refresh_handoff is False
        assert view._refresh_handoff_resolved is True  # record intact
        view._ephemeral_arm_deadline = time.monotonic() + 500
        view._schedule_ephemeral_refresh()  # entry backstop returns

        assert view._refresh_armed is False

    async def test_reopen_re_derives_on_the_replacement(self):
        """A reopen runs a fresh ``send()`` on a fresh token, so the
        replacement resolves from its own class declaration and timeout.
        The dying instance's resolution (here a chain-inherited decline)
        does not carry.
        """
        created = []

        class _View(RenderableLayoutView):
            timeout = None

            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                created.append(self)

        old = _View(interaction=_make_interaction())
        old._ephemeral = True
        old._message = MagicMock()
        old._message.delete = AsyncMock()
        old._message.edit = AsyncMock()
        old._refresh_handoff_resolved = False  # as if inherited from a declined chain
        old.exit = AsyncMock()

        created.clear()
        await old._reopen_ephemeral(_make_interaction())

        assert len(created) == 1
        replacement = created[0]
        try:
            assert replacement.auto_refresh_ephemeral is None
            assert replacement._refresh_handoff_resolved is True
        finally:
            replacement.task_manager.cancel_tasks(replacement.id)


class TestEphemeralRearmOnNavRollback:
    """A failed-navigation rollback re-arms the source's ephemeral refresh
    handoff when its timer came due while the push was in flight and stood
    down (``_arm_after_rollback``), so a recovered long-lived ephemeral source
    still swaps in its refresh button before the 900s token cliff. The
    re-schedule uses the original deadline, so it sleeps the remaining time
    rather than a fresh window. Every case sets that precondition, so the
    refusals below are refused for their own reason.
    """

    @staticmethod
    def _capture_schedules(view):
        scheduled: list = []
        spy = patch.object(
            view, "_schedule_ephemeral_refresh", side_effect=lambda: scheduled.append(True)
        )
        return scheduled, spy

    async def test_rollback_reschedules_ephemeral_handoff(self):
        import time as _time

        class _Source(RenderableLayoutView):
            auto_refresh_ephemeral = True

        source = _Source(interaction=_make_interaction(user_id=1, guild_id=100))
        await source.send()
        # Engaged handoff: deadline stamped at send, button not yet armed.
        source._ephemeral = True
        source._ephemeral_arm_deadline = _time.monotonic() + 500
        source._refresh_armed = False

        source._arm_after_rollback = True  # its timer came due mid-push
        new_view = _Source(interaction=_make_interaction(user_id=1, guild_id=100))
        scheduled, spy = self._capture_schedules(source)
        with spy:
            source._roll_back_navigation(new_view, reclaim=False)

        assert len(scheduled) == 1  # the ephemeral handoff was re-scheduled

    async def test_a_timer_due_during_a_failed_push_arms_after_the_rollback(self):
        """The timer runs on through a push. Coming due mid-push it stands
        down rather than edit the message the push may hand over, and the
        rollback arms in its place."""

        class _Source(RenderableLayoutView):
            auto_refresh_ephemeral = True

        class _Sub(RenderableLayoutView):
            pass

        source = _Source(interaction=_make_interaction(user_id=1, guild_id=100))
        await source.send(ephemeral=True)
        source.task_manager.cancel_tasks(source.id)
        await asyncio.sleep(0)
        # Due at the timer's one-second floor.
        source._ephemeral_arm_deadline = time.monotonic()
        source._schedule_ephemeral_refresh()

        message = source._message
        gate = asyncio.Event()
        error = discord.HTTPException(MagicMock(status=503), "boom")

        async def deferred_edit(**kwargs):
            await gate.wait()
            raise error

        async def channel_edit(**kwargs):
            if isinstance(kwargs.get("view"), _Sub):
                raise error
            return message

        nav = _make_interaction(user_id=1, guild_id=100, is_done=True)
        nav.edit_original_response = AsyncMock(side_effect=deferred_edit)
        message.edit = AsyncMock(side_effect=channel_edit)
        push = asyncio.create_task(source.push(_Sub, interaction=nav))
        try:
            await asyncio.sleep(1.3)  # the timer comes due while away
            assert not source._refresh_armed
            message.edit.assert_not_called()
            gate.set()
            await push
            await until(lambda: source._refresh_armed, interval=0.01)

            assert source._refresh_armed
        finally:
            gate.set()
            source.task_manager.cancel_tasks(source.id)

    async def test_rollback_skips_reschedule_without_handoff(self):
        # A source that never engaged the handoff (no deadline stamped) gets no
        # re-schedule on rollback.
        class _Source(RenderableLayoutView):
            pass

        source = _Source(interaction=_make_interaction(user_id=1, guild_id=100))
        await source.send()
        source._arm_after_rollback = True  # its timer came due mid-push
        new_view = _Source(interaction=_make_interaction(user_id=1, guild_id=100))

        scheduled, spy = self._capture_schedules(source)
        with spy:
            source._roll_back_navigation(new_view, reclaim=False)

        assert scheduled == []

    async def test_rollback_skips_reschedule_when_already_armed(self):
        import time as _time

        class _Source(RenderableLayoutView):
            auto_refresh_ephemeral = True

        source = _Source(interaction=_make_interaction(user_id=1, guild_id=100))
        await source.send()
        source._ephemeral = True
        source._ephemeral_arm_deadline = _time.monotonic() + 500
        source._refresh_armed = True  # already armed -> nothing to re-arm

        source._arm_after_rollback = True  # its timer came due mid-push
        new_view = _Source(interaction=_make_interaction(user_id=1, guild_id=100))
        scheduled, spy = self._capture_schedules(source)
        with spy:
            source._roll_back_navigation(new_view, reclaim=False)

        assert scheduled == []

    async def test_rollback_does_not_rearm_a_pinned_off_source(self):
        """The deadline is stamped for every ephemeral send, so its presence
        alone no longer implies the handoff engaged. A source that pinned the
        handoff off comes back from a rollback still pinned off.
        """

        class _Source(RenderableLayoutView):
            auto_refresh_ephemeral = False

        source = _Source(interaction=_make_interaction(user_id=1, guild_id=100))
        await source.send(ephemeral=True)
        assert source._ephemeral_arm_deadline is not None
        assert source._refresh_armed is False

        source._arm_after_rollback = True  # its timer came due mid-push
        new_view = _Source(interaction=_make_interaction(user_id=1, guild_id=100))
        scheduled, spy = self._capture_schedules(source)
        with spy:
            source._roll_back_navigation(new_view, reclaim=False)

        assert scheduled == []

    async def test_rollback_rearms_a_derived_engaged_source(self):
        """The rollback gate reads the effective policy, so a send whose
        ``None`` declaration derived "engaged" re-arms -- with the
        declaration itself still reading ``None``.
        """

        class _Source(RenderableLayoutView):
            timeout = None

        source = _Source(interaction=_make_interaction(user_id=1, guild_id=100))
        await source.send(ephemeral=True)
        # The send-scheduled timer came due mid-push and stood down.
        source.task_manager.cancel_tasks(source.id)
        assert source.auto_refresh_ephemeral is None

        source._arm_after_rollback = True  # its timer came due mid-push
        new_view = _Source(interaction=_make_interaction(user_id=1, guild_id=100))
        scheduled, spy = self._capture_schedules(source)
        with spy:
            source._roll_back_navigation(new_view, reclaim=False)

        assert len(scheduled) == 1

    async def test_rollback_does_not_rearm_a_derived_declined_source(self):
        """The other polarity: a send whose ``None`` declaration derived
        "declined" comes back declined. The deadline is present (stamped at
        every ephemeral send), so only the resolved policy can say no here.
        """

        class _Source(RenderableLayoutView):
            timeout = 300

        source = _Source(interaction=_make_interaction(user_id=1, guild_id=100))
        await source.send(ephemeral=True)
        assert source.auto_refresh_ephemeral is None
        assert source._ephemeral_arm_deadline is not None
        assert source._refresh_armed is False

        source._arm_after_rollback = True  # its timer came due mid-push
        new_view = _Source(interaction=_make_interaction(user_id=1, guild_id=100))
        scheduled, spy = self._capture_schedules(source)
        with spy:
            source._roll_back_navigation(new_view, reclaim=False)

        assert scheduled == []


class TestEphemeralHandoffOnNavSuccess:
    """A successful push carries the ephemeral arming deadline onto the
    destination and schedules its handoff timer at the post-commit seam in
    _commit_navigation -- the same guard shape as the rollback re-arm, but on
    the new view. The carried deadline (stamped once at the original send)
    means a mid-chain hop sleeps only the remaining time to the 900s token
    cliff, and the next hop's commit reaps the prior destination's
    timer so the chain holds one live timer.
    """

    @staticmethod
    def _http_error(status=503):
        return discord.HTTPException(MagicMock(status=status), "boom")

    async def test_push_carries_deadline_and_schedules_handoff(self):
        class _Source(RenderableLayoutView):
            auto_refresh_ephemeral = True

        class _Dest(RenderableLayoutView):
            async def on_state_changed(self, state):
                pass

        source = _Source(interaction=_make_interaction(user_id=1, guild_id=100))
        await source.send()
        source._ephemeral = True
        deadline = time.monotonic() + 500
        source._ephemeral_arm_deadline = deadline

        dest = await source.push(_Dest)
        try:
            # Carried, not recomputed: the token belongs to the original send.
            assert dest._ephemeral_arm_deadline == deadline
            await until(lambda: len(refresh_timers(dest)) == 1)
        finally:
            dest.task_manager.cancel_tasks(dest.id)

    async def test_second_hop_cancels_prior_destination_timer(self):
        class _Source(RenderableLayoutView):
            auto_refresh_ephemeral = True

        class _Mid(RenderableLayoutView):
            async def on_state_changed(self, state):
                pass

        class _Deep(RenderableLayoutView):
            async def on_state_changed(self, state):
                pass

        source = _Source(interaction=_make_interaction(user_id=1, guild_id=100))
        await source.send()
        source._ephemeral = True
        source._ephemeral_arm_deadline = time.monotonic() + 500

        mid = await source.push(_Mid)
        await until(lambda: refresh_timers(mid))
        [mid_timer] = refresh_timers(mid)

        deep = await mid.push(_Deep)
        try:
            # The second hop's cancel_tasks(mid.id) reaps the first
            # destination's timer; only the new destination holds one.
            for _ in range(3):
                await asyncio.sleep(0)
            assert mid_timer.cancelled()
            assert refresh_timers(mid) == []
            assert len(refresh_timers(deep)) == 1
        finally:
            deep.task_manager.cancel_tasks(deep.id)

    async def test_rollback_leaves_no_destination_timer(self):
        class _Source(RenderableLayoutView):
            auto_refresh_ephemeral = True

        class _Dest(RenderableLayoutView):
            async def on_state_changed(self, state):
                pass

        source = _Source(interaction=_make_interaction(user_id=1, guild_id=100))
        await source.send(ephemeral=True)

        await until(lambda: refresh_timers(source))
        [timer] = refresh_timers(source)

        # Every edit endpoint fails -> _roll_back_navigation.
        nav = _make_interaction(user_id=1, guild_id=100, is_done=False)
        nav.response.edit_message = AsyncMock(side_effect=self._http_error())
        nav.response.defer = AsyncMock(side_effect=self._http_error())
        nav.edit_original_response = AsyncMock(side_effect=self._http_error())
        source._message.edit = AsyncMock(side_effect=self._http_error())

        dest = await source.push(_Dest, interaction=nav)
        try:
            # The deadline carried in the navigation batch, but the timer
            # never armed: scheduling is post-commit and the edit never
            # confirmed. The source's own timer ran on through the push, so
            # it is still the only one, neither cancelled nor duplicated.
            assert dest._ephemeral_arm_deadline is not None
            assert dest.task_manager.get_task_count(dest.id) == 0
            assert refresh_timers(dest) == []
            assert refresh_timers(source) == [timer]
            assert not timer.cancelled()
        finally:
            source.task_manager.cancel_tasks(source.id)


# // ========================================( Expired Token )======================================== // #


def _http_error(status: int) -> discord.HTTPException:
    """An HTTPException carrying a specific status, as Discord would raise."""
    return discord.HTTPException(MagicMock(status=status, reason="Error"), f"{status} Error")


class TestRefreshAbsorbsExpiredEphemeralToken:
    """An ephemeral past the 15-minute webhook cliff cannot be edited.

    ``exit()`` and ``on_timeout()`` both treat that 401 as expected
    lifecycle and log it at debug. ``refresh()`` re-raised it instead, so
    every state dispatch reaching such a view surfaced an ERROR and a
    traceback from the store's subscriber wrapper.
    """

    async def test_ephemeral_401_is_absorbed(self):
        view = RenderableLayoutView()
        view._ephemeral = True
        view._message = AsyncMock()
        view._message.edit.side_effect = _http_error(401)
        view._last_tree_digest = None

        await view.refresh()

        view._message.edit.assert_awaited_once()

    async def test_non_ephemeral_401_still_raises(self):
        """The guard is scoped to ephemerals. A 401 elsewhere is a real error."""
        view = RenderableLayoutView()
        view._ephemeral = False
        view._message = AsyncMock()
        view._message.edit.side_effect = _http_error(401)
        view._last_tree_digest = None

        with pytest.raises(discord.HTTPException):
            await view.refresh()

    async def test_ephemeral_non_401_still_raises(self):
        """Only token expiry is absorbed, not every failure on an ephemeral."""
        view = RenderableLayoutView()
        view._ephemeral = True
        view._message = AsyncMock()
        view._message.edit.side_effect = _http_error(500)
        view._last_tree_digest = None

        with pytest.raises(discord.HTTPException):
            await view.refresh()


class TestArmingRetriesWhileTheTokenLives:
    """A dropped arming edit keeps retrying, the way a rate-limited one does.

    ``_handle_rate_limit`` queues a successor, so a 429 during arming heals
    itself. A transport failure carries no retry-after and took a short fixed
    window instead, and the deferred render that fired on that window did not
    look at whether its own edit landed. Two consecutive drops inside the
    90-second arming budget therefore ended the chain and left the panel
    frozen with no refresh button on it.
    """

    async def test_a_second_dropped_arming_edit_queues_another_attempt(self):
        class _View(RenderableLayoutView):
            auto_refresh_ephemeral = True

        view = _View(interaction=_make_interaction())
        view._message = MagicMock()
        view._message.edit = AsyncMock(side_effect=aiohttp.ClientOSError(104, "reset"))
        await arm(view)
        view._last_tree_digest = None  # as an arming edit that did not land leaves it

        queued = []
        view._queue_deferred_refresh = lambda wait: queued.append(wait)

        await view._deferred_refresh(0)

        assert queued == [5.0], "the armed branch must re-queue while the token can still edit"

    async def test_a_landed_arming_edit_ends_the_chain(self):
        class _View(RenderableLayoutView):
            auto_refresh_ephemeral = True

        view = _View(interaction=_make_interaction())
        view._message = MagicMock()
        view._message.edit = AsyncMock()
        await arm(view)
        view._last_tree_digest = None  # as an arming edit that did not land leaves it

        queued = []
        view._queue_deferred_refresh = lambda wait: queued.append(wait)

        await view._deferred_refresh(0)

        assert queued == []

    async def test_an_expired_token_ends_the_chain(self):
        """Past the cliff the edit answers 401, which does not set the flag."""

        class _View(RenderableLayoutView):
            auto_refresh_ephemeral = True

        view = _View(interaction=_make_interaction())
        view._ephemeral = True
        view._message = MagicMock()
        view._message.edit = AsyncMock(
            side_effect=discord.HTTPException(MagicMock(status=401), "expired")
        )
        await arm(view)
        view._last_tree_digest = None  # as an arming edit that did not land leaves it

        queued = []
        view._queue_deferred_refresh = lambda wait: queued.append(wait)

        await view._deferred_refresh(0)

        assert queued == [], "a dead token must not spin a retry loop"

    @pytest.mark.parametrize("seam", ["arming", "retry"])
    async def test_a_server_error_queues_another_attempt(self, seam):
        """A 5xx carries no retry-after either, and ended the chain at the
        first one: logged at DEBUG, with the panel left frozen."""

        class _View(RenderableLayoutView):
            auto_refresh_ephemeral = True

        view = _View(interaction=_make_interaction())
        view._message = MagicMock()
        view._message.edit = AsyncMock(side_effect=_http_error(503))
        queued = []
        view._queue_deferred_refresh = lambda wait: queued.append(wait)

        if seam == "arming":
            await view._arm_refresh_button()
        else:
            await arm(view)
            view._last_tree_digest = None  # as an arming edit that did not land leaves it
            await view._deferred_refresh(0)

        assert queued == [5.0]

    @pytest.mark.parametrize("seam", ["arming", "retry"])
    async def test_a_client_error_on_the_arming_edit_ends_the_chain(self, seam):
        class _View(RenderableLayoutView):
            auto_refresh_ephemeral = True

        view = _View(interaction=_make_interaction())
        view._message = MagicMock()
        view._message.edit = AsyncMock(side_effect=_http_error(403))
        queued = []
        view._queue_deferred_refresh = lambda wait: queued.append(wait)

        if seam == "arming":
            await view._arm_refresh_button()
        else:
            await arm(view)
            view._last_tree_digest = None  # as an arming edit that did not land leaves it
            with pytest.raises(discord.HTTPException):
                await view._deferred_refresh(0)

        assert queued == []


class _ArmedSource(RenderableLayoutView):
    """Ephemeral sender with the handoff engaged and the arming point ~1s out
    (``900 - 899``), so a destination timer fires within a short test sleep.
    """

    auto_refresh_ephemeral = True
    refresh_warning_seconds = 899


class _DeclinedSource(RenderableLayoutView):
    """Ephemeral sender with the handoff pinned off. The send still stamps
    the arming deadline -- the token clock is a fact about the message.
    """

    auto_refresh_ephemeral = False
    refresh_warning_seconds = 899


class TestHandoffPolicyAcrossNavigation:
    """``auto_refresh_ephemeral`` holds on navigation destinations in both
    polarities. The arming deadline is stamped at every ephemeral send (it
    records the message's token clock, not the policy), so the post-commit
    gate can honor the destination's own flag against it: an explicit
    ``False`` stays off even when the source armed, an explicit ``True``
    engages even when the source declined, and a ``None`` push destination
    inherits the immediate source's effective policy, carried on the
    private ``_refresh_handoff_resolved`` -- the declaration itself never
    changes. The inheritance is push-only; ``TestHandoffPolicyOnPop``
    covers the pop direction, where the restored view resumes its own
    resolution instead.
    """

    @staticmethod
    async def _push_to(source_cls, dest_cls):
        source = source_cls(interaction=_make_interaction(user_id=1), user_id=1, guild_id=2)
        await source.send(ephemeral=True)
        destination = await source.push(
            dest_cls, interaction=_make_interaction(user_id=1, guild_id=2)
        )
        destination._message = MagicMock()
        destination._message.edit = AsyncMock()
        return destination

    async def test_a_pinned_off_destination_is_not_armed_by_an_armed_source(self):
        armed = []

        class _PinnedOff(RenderableLayoutView):
            auto_refresh_ephemeral = False

            async def _arm_refresh_button(self):
                armed.append(True)

        dest = await self._push_to(_ArmedSource, _PinnedOff)
        assert dest._ephemeral_arm_deadline is not None
        await asyncio.sleep(1.3)

        assert armed == []

    async def test_a_pinned_on_destination_is_armed_by_an_armed_source(self):
        armed = []

        class _PinnedOn(RenderableLayoutView):
            auto_refresh_ephemeral = True

            async def _arm_refresh_button(self):
                armed.append(True)

        await self._push_to(_ArmedSource, _PinnedOn)
        await until(lambda: armed, interval=0.01)

        assert armed == [True]

    async def test_an_unset_destination_inherits_an_armed_source(self):
        armed = []

        class _Derived(RenderableLayoutView):
            async def _arm_refresh_button(self):
                armed.append(True)

        assert _Derived.auto_refresh_ephemeral is None
        dest = await self._push_to(_ArmedSource, _Derived)
        # The inherited decision rides the private resolution field so the
        # next hop and a rollback re-arm read policy, never deadline
        # presence. The declaration is the author's and stays untouched.
        assert dest.auto_refresh_ephemeral is None
        assert dest._refresh_handoff_resolved is True
        await until(lambda: armed, interval=0.01)

        assert armed == [True]

    async def test_a_pinned_on_destination_is_armed_by_a_declined_source(self):
        """The other polarity of the pinned-off guarantee. Fails on any tree
        where a declined send skips the deadline stamp: there is then no
        clock for the destination's ``True`` to arm against.
        """
        armed = []

        class _PinnedOn(RenderableLayoutView):
            auto_refresh_ephemeral = True

            async def _arm_refresh_button(self):
                armed.append(True)

        dest = await self._push_to(_DeclinedSource, _PinnedOn)
        assert dest._ephemeral_arm_deadline is not None
        await until(lambda: armed, interval=0.01)

        assert armed == [True]

    async def test_a_pinned_off_destination_stays_off_after_a_declined_source(self):
        armed = []

        class _PinnedOff(RenderableLayoutView):
            auto_refresh_ephemeral = False

            async def _arm_refresh_button(self):
                armed.append(True)

        dest = await self._push_to(_DeclinedSource, _PinnedOff)
        # The clock is carried even here: presence records the token window,
        # not the policy.
        assert dest._ephemeral_arm_deadline is not None
        await asyncio.sleep(1.3)

        assert armed == []

    async def test_an_unset_destination_inherits_a_declined_source(self):
        armed = []

        class _Derived(RenderableLayoutView):
            async def _arm_refresh_button(self):
                armed.append(True)

        assert _Derived.auto_refresh_ephemeral is None
        dest = await self._push_to(_DeclinedSource, _Derived)
        assert dest._ephemeral_arm_deadline is not None
        assert dest.auto_refresh_ephemeral is None
        assert dest._refresh_handoff_resolved is False
        await asyncio.sleep(1.3)

        assert armed == []

    async def test_a_declined_decision_survives_two_unset_hops(self):
        """The carried resolution is what takes the decision past the first
        hop: a second ``None`` destination inherits from the first's
        ``_refresh_handoff_resolved``, while both declarations stay ``None``.
        Without the carry, the second hop would find no policy anywhere and
        fall back to the carried clock, arming a chain whose send declined.
        """
        armed = []

        class _Mid(RenderableLayoutView):
            pass

        class _Deep(RenderableLayoutView):
            async def _arm_refresh_button(self):
                armed.append(True)

        mid = await self._push_to(_DeclinedSource, _Mid)
        deep = await mid.push(_Deep, interaction=_make_interaction(user_id=1, guild_id=2))
        deep._message = MagicMock()
        deep._message.edit = AsyncMock()
        assert mid.auto_refresh_ephemeral is None
        assert deep.auto_refresh_ephemeral is None
        assert deep._refresh_handoff_resolved is False
        await asyncio.sleep(1.3)

        assert armed == []

    async def test_an_engaged_decision_survives_two_unset_hops(self):
        """The engaged polarity of the two-hop carry. The mid view's own
        in-window ``timeout`` shows navigation inherits the source's
        resolution rather than re-deriving at the destination.
        """

        class _Src(RenderableLayoutView):
            timeout = None

        class _Mid(RenderableLayoutView):
            timeout = 300

        class _Deep(RenderableLayoutView):
            pass

        src = _Src(interaction=_make_interaction(user_id=1), user_id=1, guild_id=2)
        await src.send(ephemeral=True)
        mid = await src.push(_Mid, interaction=_make_interaction(user_id=1, guild_id=2))
        deep = await mid.push(_Deep, interaction=_make_interaction(user_id=1, guild_id=2))
        try:
            assert mid.auto_refresh_ephemeral is None
            assert deep.auto_refresh_ephemeral is None
            assert mid._refresh_handoff_resolved is True
            assert deep._refresh_handoff_resolved is True
            # The carried decision holds a live timer on the deepest hop.
            await until(lambda: len(refresh_timers(deep)) == 1)
        finally:
            deep.task_manager.cancel_tasks(deep.id)

    async def test_an_unresolved_chain_keeps_the_carried_deadline_behavior(self):
        """A deadline stamped outside the send pipeline (no resolved flag
        anywhere on the chain) keeps the presence behavior: with no policy
        to consult, the carried clock arms.
        """
        armed = []

        class _Derived(RenderableLayoutView):
            async def _arm_refresh_button(self):
                armed.append(True)

        source = RenderableLayoutView(
            interaction=_make_interaction(user_id=1), user_id=1, guild_id=2
        )
        await source.send()
        source._message = MagicMock()
        source._message.edit = AsyncMock()
        source._ephemeral = True
        source._ephemeral_arm_deadline = time.monotonic() - 5
        dest = await source.push(_Derived, interaction=_make_interaction(user_id=1, guild_id=2))
        dest._message = MagicMock()
        dest._message.edit = AsyncMock()
        await until(lambda: armed, interval=0.01)

        assert armed == [True]


# // ========================================( Handoff Policy on Pop )======================================== // #


class TestHandoffPolicyOnPop:
    """Policy inheritance flows down a navigation chain, never back up it.

    A push destination with no answer of its own adopts the source's
    effective policy; a popped-to view resumes the resolution it held when
    it was pushed away from, handed back through the nav-stack entry. The
    departing child's declaration therefore never reaches the restored
    view: a derived engagement survives a round trip through a pinned-off
    child, a pinned parent's declaration stands untouched in both
    polarities, and a view that held no policy comes back holding none.
    """

    @staticmethod
    async def _round_trip(root_cls, child_cls):
        root = root_cls(interaction=_make_interaction(user_id=1), user_id=1, guild_id=2)
        await root.send(ephemeral=True)
        child = await root.push(child_cls, interaction=_make_interaction(user_id=1, guild_id=2))
        child._message = MagicMock()
        child._message.edit = AsyncMock()
        popped = await child.pop(interaction=_make_interaction(user_id=1, guild_id=2))
        assert popped is not None
        popped._message = MagicMock()
        popped._message.edit = AsyncMock()
        return child, popped

    async def test_pop_from_a_pinned_off_child_resumes_a_derived_engagement(self):
        """The freezing shape: a root whose ``None`` declaration derived
        "engaged" at its ephemeral send pushes into a child pinning the
        handoff off, then pops back. The restored root resumes its own
        resolution (the child's ``False`` does not flow up), so the
        post-commit gate schedules its timer against the carried deadline
        rather than leaving the panel frozen at the token cliff.
        """
        armed = []

        class _Root(RenderableLayoutView):
            timeout = None  # the send derives the handoff engaged
            refresh_warning_seconds = 899  # arm deadline = send + ~1s

            async def _arm_refresh_button(self):
                armed.append(True)

        class _PinnedOffChild(RenderableLayoutView):
            auto_refresh_ephemeral = False

        _, popped = await self._round_trip(_Root, _PinnedOffChild)
        try:
            # The post-commit gate is the seam under test: it reads the
            # restored resolution, so its decision is visible before any
            # sleep -- declaration untouched, resumed resolution engaged,
            # timer scheduled.
            assert popped.auto_refresh_ephemeral is None
            assert popped._refresh_handoff_resolved is True
            assert popped._refresh_handoff is True
            await until(lambda: len(refresh_timers(popped)) == 1)

            await until(lambda: armed, interval=0.01)
            assert armed == [True]
        finally:
            popped.task_manager.cancel_tasks(popped.id)

    async def test_pop_from_an_unset_child_keeps_the_engagement(self):
        """The control cell: an unset child inherits the engagement on the
        way down, and the pop hands the root back its own resolution. Both
        directions agree, so the round trip changes nothing.
        """
        armed = []

        class _Root(RenderableLayoutView):
            timeout = None
            refresh_warning_seconds = 899

            async def _arm_refresh_button(self):
                armed.append(True)

        class _UnsetChild(RenderableLayoutView):
            pass

        child, popped = await self._round_trip(_Root, _UnsetChild)
        try:
            assert child._refresh_handoff_resolved is True  # inherited on push
            assert popped._refresh_handoff_resolved is True  # resumed on pop
            await until(lambda: len(refresh_timers(popped)) == 1)

            await until(lambda: armed, interval=0.01)
            assert armed == [True]
        finally:
            popped.task_manager.cancel_tasks(popped.id)

    async def test_a_pinned_on_parent_acquires_no_resolution_from_the_child(self):
        """A declared parent needs no hand-back (the declaration rides the
        class through reconstruction) and must not pick up a resolution
        record it never made. The restored view arms on its own ``True``
        with the resolution still unset.
        """
        armed = []

        class _Root(RenderableLayoutView):
            auto_refresh_ephemeral = True
            refresh_warning_seconds = 899

            async def _arm_refresh_button(self):
                armed.append(True)

        class _PinnedOffChild(RenderableLayoutView):
            auto_refresh_ephemeral = False

        _, popped = await self._round_trip(_Root, _PinnedOffChild)
        try:
            assert popped._refresh_handoff is True  # the declaration decides
            assert popped._refresh_handoff_resolved is None
            await until(lambda: len(refresh_timers(popped)) == 1)

            await until(lambda: armed, interval=0.01)
            assert armed == [True]
        finally:
            popped.task_manager.cancel_tasks(popped.id)

    async def test_a_pinned_off_parent_stays_off_after_pop(self):
        """The other polarity: the parent's own ``False`` declaration
        decides at the post-commit gate, and the engaged child's ``True``
        does not flow up any more than a ``False`` does. No timer.
        """

        class _Root(RenderableLayoutView):
            auto_refresh_ephemeral = False
            refresh_warning_seconds = 899

        class _PinnedOnChild(RenderableLayoutView):
            auto_refresh_ephemeral = True

        _, popped = await self._round_trip(_Root, _PinnedOnChild)
        try:
            assert popped._refresh_handoff is False
            assert popped._refresh_handoff_resolved is None
            for _ in range(3):
                await asyncio.sleep(0)
            assert popped.task_manager.get_task_count(popped.id) == 0
            assert refresh_timers(popped) == []
        finally:
            popped.task_manager.cancel_tasks(popped.id)

    async def test_a_parent_with_no_policy_does_not_acquire_the_childs(self):
        """A chain whose deadline was stamped outside the send pipeline has
        no resolution anywhere, so the presence fallback governs it.
        Popping back from a pinned-off child leaves the restored view
        unresolved rather than adopting the child's ``False``, and the
        carried clock still arms.
        """
        armed = []

        class _Root(RenderableLayoutView):
            async def _arm_refresh_button(self):
                armed.append(True)

        class _PinnedOffChild(RenderableLayoutView):
            auto_refresh_ephemeral = False

        root = _Root(interaction=_make_interaction(user_id=1), user_id=1, guild_id=2)
        await root.send()
        root._message = MagicMock()
        root._message.edit = AsyncMock()
        root._ephemeral = True
        root._ephemeral_arm_deadline = time.monotonic() + 0.5
        child = await root.push(
            _PinnedOffChild, interaction=_make_interaction(user_id=1, guild_id=2)
        )
        child._message = MagicMock()
        child._message.edit = AsyncMock()
        popped = await child.pop(interaction=_make_interaction(user_id=1, guild_id=2))
        assert popped is not None
        popped._message = MagicMock()
        popped._message.edit = AsyncMock()
        try:
            assert popped.auto_refresh_ephemeral is None
            assert popped._refresh_handoff_resolved is None
            await until(lambda: len(refresh_timers(popped)) == 1)

            await until(lambda: armed, interval=0.01)
            assert armed == [True]
        finally:
            popped.task_manager.cancel_tasks(popped.id)
