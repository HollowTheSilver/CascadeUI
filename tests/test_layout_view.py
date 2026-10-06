"""Tests for StatefulLayoutView (V2 base class)."""

import asyncio
import contextlib
import inspect
import io
import logging
import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import discord
import pytest
from discord.ui import ActionRow, Button, Container
from discord.ui import File as UIFile
from discord.ui import LayoutView, MediaGallery, Section, Separator, TextDisplay, Thumbnail
from helpers import RenderableLayoutView
from helpers import make_interaction as _make_interaction
from helpers import schedule_bounded_waits_as_before_312, until

from cascadeui.components.base import StatefulButton, StatefulSelect
from cascadeui.components.buttons import LinkButton
from cascadeui.components.patterns.v2 import card, divider, gallery
from cascadeui.components.types import MAX_MESSAGE_CHARACTERS
from cascadeui.state.singleton import get_store
from cascadeui.state.store import _CURRENT_INTERACTION
from cascadeui.views._placement import validate_placement
from cascadeui.views.base import RenderOutcome, _StatefulMixin, _view_class_registry
from cascadeui.views.layout import (
    DisplayLayoutView,
    StatefulLayoutView,
    count_characters,
    count_components,
)


class TestStatefulLayoutViewInit:
    """Basic init and inheritance tests."""

    def test_is_subclass_of_layout_view(self):
        assert issubclass(StatefulLayoutView, LayoutView)

    def test_is_subclass_of_mixin(self):
        assert issubclass(StatefulLayoutView, _StatefulMixin)

    def test_init_with_required_kwargs(self):
        interaction = _make_interaction()
        view = StatefulLayoutView(interaction=interaction)

        assert view.state_store is not None
        assert view.user_id == 100
        assert view.guild_id == 200
        # Default polarity: session_continuity is False, so the derived
        # session_id carries a per-instance UUID suffix after the user id.
        prefix = f"{StatefulLayoutView._class_session_key()}:user_100:"
        assert view.session_id.startswith(prefix)
        assert len(view.session_id) == len(prefix) + 8

    def test_subscribes_to_state_on_init(self):
        interaction = _make_interaction()
        view = StatefulLayoutView(interaction=interaction)

        store = get_store()
        assert view.id in store.subscribers

    def test_default_subscribed_actions(self):
        interaction = _make_interaction()
        view = StatefulLayoutView(interaction=interaction)

        assert view.subscribed_actions == set()

    def test_auto_defer_defaults(self):
        interaction = _make_interaction()
        view = StatefulLayoutView(interaction=interaction)

        assert view.auto_defer is True
        assert view.auto_defer_delay == 2.5

    def test_owner_only_defaults(self):
        interaction = _make_interaction()
        view = StatefulLayoutView(interaction=interaction)

        assert view.owner_only is True


class TestMakeExitButton:
    """``make_exit_button`` returns an unattached button; ``add_exit_button`` wraps it."""

    def test_returns_unattached_button(self):
        from cascadeui.components.base import StatefulButton

        interaction = _make_interaction()
        view = StatefulLayoutView(interaction=interaction)
        before = len(list(view.children))
        btn = view.make_exit_button(label="Close")
        after = len(list(view.children))

        assert isinstance(btn, StatefulButton)
        assert btn.label == "Close"
        assert after == before  # not attached

    def test_add_exit_button_still_attaches(self):
        interaction = _make_interaction()
        view = StatefulLayoutView(interaction=interaction)
        before = len(list(view.children))
        view.add_exit_button()
        after = len(list(view.children))
        assert after == before + 1  # ActionRow wrapper added


class TestMakeBackButton:
    """``make_back_button`` returns an unattached Back button (V1 + V2)."""

    def test_returns_unattached_button(self):
        interaction = _make_interaction()
        view = StatefulLayoutView(interaction=interaction)
        before = len(list(view.children))
        btn = view.make_back_button()
        after = len(list(view.children))

        assert isinstance(btn, StatefulButton)
        assert btn.label == "Back"
        assert after == before  # not attached

    def test_custom_label_and_custom_id(self):
        interaction = _make_interaction()
        view = StatefulLayoutView(interaction=interaction)
        btn = view.make_back_button(label="Return", custom_id="nav_back")

        assert btn.label == "Return"
        assert btn.custom_id == "nav_back"

    def test_add_back_button_uses_helper(self):
        # The auto back button is built via make_back_button, so it carries
        # the same Back label and is stashed for rebuild restoration.
        interaction = _make_interaction()
        view = StatefulLayoutView(interaction=interaction)
        view._add_back_button()

        assert view._auto_back_item in list(view.children)


class TestMakeNavRow:
    """``make_nav_row`` combines Back + Exit into one ActionRow (V2)."""

    def test_returns_actionrow_with_both(self):
        interaction = _make_interaction()
        view = StatefulLayoutView(interaction=interaction)
        row = view.make_nav_row()

        assert isinstance(row, ActionRow)
        assert [c.label for c in row.children] == ["Back", "Exit"]

    def test_back_only(self):
        interaction = _make_interaction()
        view = StatefulLayoutView(interaction=interaction)
        row = view.make_nav_row(exit=False)

        assert [c.label for c in row.children] == ["Back"]

    def test_exit_only(self):
        interaction = _make_interaction()
        view = StatefulLayoutView(interaction=interaction)
        row = view.make_nav_row(back=False)

        assert [c.label for c in row.children] == ["Exit"]

    def test_custom_labels(self):
        interaction = _make_interaction()
        view = StatefulLayoutView(interaction=interaction)
        row = view.make_nav_row(back_label="Prev", exit_label="Close")

        assert [c.label for c in row.children] == ["Prev", "Close"]

    def test_custom_emoji_and_style(self):
        import discord

        interaction = _make_interaction()
        view = StatefulLayoutView(interaction=interaction)
        row = view.make_nav_row(
            back_label="Leagues",
            back_emoji="\U0001f3e0",
            back_style=discord.ButtonStyle.primary,
        )
        back = list(row.children)[0]
        assert back.label == "Leagues"
        assert str(back.emoji) == "\U0001f3e0"
        assert back.style == discord.ButtonStyle.primary

    def test_default_emoji_matches_button_helpers(self):
        # The defaults mirror make_back_button / make_exit_button so the
        # shorthand and the manual composition render identically.
        interaction = _make_interaction()
        view = StatefulLayoutView(interaction=interaction)
        row = view.make_nav_row()
        back, exit_ = list(row.children)
        assert str(back.emoji) == str(view.make_back_button().emoji)
        assert str(exit_.emoji) == str(view.make_exit_button().emoji)

    def test_both_false_raises(self):
        interaction = _make_interaction()
        view = StatefulLayoutView(interaction=interaction)

        with pytest.raises(ValueError, match="at least one"):
            view.make_nav_row(back=False, exit=False)

    def test_not_attached(self):
        interaction = _make_interaction()
        view = StatefulLayoutView(interaction=interaction)
        before = len(list(view.children))
        view.make_nav_row()
        after = len(list(view.children))

        assert after == before  # returns the row, does not attach


class TestStatefulLayoutViewSubclass:
    """Subclass registration and kwargs auto-capture."""

    def test_subclass_registered_in_view_class_registry(self):
        class _TestLayoutPanel(StatefulLayoutView):
            pass

        key = _TestLayoutPanel._class_session_key()
        assert key in _view_class_registry
        assert _view_class_registry[key] is _TestLayoutPanel

    def test_init_kwargs_auto_captured(self):
        class _CustomLayout(StatefulLayoutView):
            def __init__(self, *args, title="default", **kwargs):
                self.title = title
                super().__init__(*args, **kwargs)

        interaction = _make_interaction()
        view = _CustomLayout(interaction=interaction, title="Dashboard")

        assert view.title == "Dashboard"
        assert view._init_kwargs == {"title": "Dashboard"}

    def test_non_reconstructible_kwargs_excluded(self):
        class _AnotherLayout(StatefulLayoutView):
            def __init__(self, *args, label="x", **kwargs):
                self.label = label
                super().__init__(*args, **kwargs)

        interaction = _make_interaction()
        view = _AnotherLayout(interaction=interaction, label="test")

        # interaction is non-reconstructible, should be excluded
        assert "interaction" not in view._init_kwargs
        assert view._init_kwargs == {"label": "test"}


class TestCountCharacters:
    """The character budget's per-item counter, mirroring count_components.

    Without it, measuring a subtree's characters means adding it to a
    throwaway view, which borrows that view's component limit -- so a
    subtree over the COMPONENT budget failed a CHARACTER measurement with
    an error naming neither.
    """

    def test_it_counts_text_and_nothing_else(self):
        assert count_characters(TextDisplay("x" * 100)) == 100
        assert count_characters(Button(label="L" * 80, custom_id="b")) == 0

    def test_it_reaches_every_depth(self):
        tree = Container(
            TextDisplay("a" * 30),
            Section(TextDisplay("b" * 20), accessory=Button(label="Z", custom_id="z")),
        )

        assert count_characters(tree) == 50

    @pytest.mark.parametrize(
        "items",
        [
            [TextDisplay("a" * 40), TextDisplay("b" * 60)],
            [Container(TextDisplay("c" * 30), TextDisplay("d" * 20))],
            [Container(TextDisplay("e" * 15), ActionRow(Button(label="Q" * 60, custom_id="q")))],
        ],
    )
    def test_it_agrees_with_discord_pys_own_counter(self, items):
        """The parity that makes the two numbers comparable at all."""
        view = RenderableLayoutView()
        base = view.content_length()
        for item in items:
            view.add_item(item)

        assert sum(count_characters(i) for i in items) == view.content_length() - base
        view.stop()

    def test_measuring_items_needs_no_view_and_so_no_component_limit(self):
        """The asymmetry this closes.

        Forty-five text nodes are over the component budget and trivially
        under the character one. Measuring their characters through a
        throwaway view raises about components instead.
        """
        items = [TextDisplay("x" * 10) for _ in range(45)]

        assert sum(count_characters(i) for i in items) == 450

    def test_a_view_is_refused_by_name(self):
        view = RenderableLayoutView()

        with pytest.raises(TypeError, match="which is a view"):
            count_characters(view)

        view.stop()


class TestCountComponentsRejectsAView:
    """The pairing exists to kill an off-by-one; it must not reproduce it.

    ``count_components`` counts a node and its descendants, so handing it
    a view counts the view itself and reports one more component than
    Discord does. That is the transcription error the budget surface was
    added to remove, so it is refused rather than answered wrongly.
    """

    def test_a_layout_view_is_refused_by_name(self):
        view = RenderableLayoutView()
        view.add_item(TextDisplay("a"))

        with pytest.raises(TypeError, match="which is a view"):
            count_components(view)

        view.stop()

    def test_a_v1_view_is_refused_too(self):
        """V1 and V2 views are siblings, not parent and child."""
        from cascadeui.views.view import StatefulView

        view = StatefulView(user_id=1, guild_id=2)

        with pytest.raises(TypeError, match="which is a view"):
            count_components(view)

        view.stop()

    def test_a_bare_string_is_refused_by_both_counters(self):
        """The realistic mistake, because the builders wrap one.

        A caller measuring the text they are about to hand ``card()``
        would read zero characters and one component for it, while the
        composed card counts every character and its own node.
        """
        for bad in ("some text", None, 42):
            with pytest.raises(TypeError, match="expects a component"):
                count_characters(bad)
            with pytest.raises(TypeError, match="expects a component"):
                count_components(bad)

    def test_a_component_still_counts(self):
        """The refusal must not cost the shape the function is for."""
        assert count_components(TextDisplay("a")) == 1
        assert count_components(Container(TextDisplay("a"), TextDisplay("b"))) == 3


class TestLinkButtonUrl:
    """An empty link url is refused where it is written, for V1 too.

    The pre-flight validator catches one in a V2 tree and a V1 view
    reaches no pre-flight at all, so the same button shipped to a form
    error depending only on which view held it.
    """

    def test_an_empty_url_is_refused(self):
        with pytest.raises(ValueError, match="needs a non-empty url"):
            LinkButton(label="Docs", url="")

    def test_a_whitespace_url_is_refused(self):
        with pytest.raises(ValueError, match="needs a non-empty url"):
            LinkButton(label="Docs", url="   ")

    def test_a_real_url_is_accepted(self):
        button = LinkButton(label="Docs", url="https://example.com")

        assert button.url == "https://example.com"

    def test_a_non_string_url_is_refused_by_type(self):
        """The type is established before a property of it is read.

        A truthy non-str answers ``strip`` by raising from inside the
        guard, naming neither the parameter nor the owner. A yarl.URL is
        the realistic one: aiohttp is a hard dependency, so every
        consumer holds the type.
        """
        import yarl

        for bad in (123, True, yarl.URL("https://example.com")):
            with pytest.raises(TypeError, match="needs a str url"):
                LinkButton(label="Docs", url=bad)

    def test_link_section_is_held_to_the_same_rule(self):
        """The documented way to build one must not be the lenient way.

        ``link_section`` composes a raw ``discord.ui.Button`` rather than a
        ``LinkButton``, so a guard on the class alone left the same mistake
        refused or accepted by which constructor a caller reached for.
        """
        from cascadeui.components.patterns.v2 import link_section

        with pytest.raises(ValueError, match="needs a non-empty url"):
            link_section("Docs", label="Open", url="")

        assert link_section("Docs", label="Open", url="https://e.dev") is not None

    def test_the_refusal_names_the_call_the_caller_wrote(self):
        """Each owner reports in its own vocabulary."""
        from cascadeui.components.patterns.v2 import link_section

        with pytest.raises(ValueError, match="LinkButton"):
            LinkButton(label="A", url="")
        with pytest.raises(ValueError, match="link_section"):
            link_section("B", label="C", url="")


class TestMessageTextBudget:
    """The summed display text is warned about, not enforced.

    The placement validator's per-node cap cannot see this one: ten short
    text nodes each pass it and can still cross the message total. Nothing
    enforces the total either, so the tree ships and Discord refuses it at
    send, naming no component.
    """

    @staticmethod
    def _capture(caplog):
        return [r.getMessage() for r in caplog.records if "display characters" in r.getMessage()]

    def test_an_over_budget_tree_warns_with_the_measured_total(self, caplog):
        view = RenderableLayoutView()
        view.add_item(TextDisplay("a" * 1500))
        view.add_item(Container(TextDisplay("b" * 1500), TextDisplay("c" * 1500)))
        total = view.content_length()
        assert total > MAX_MESSAGE_CHARACTERS

        with caplog.at_level(logging.WARNING, logger="cascadeui"):
            view._check_placement()

        warnings = self._capture(caplog)
        assert len(warnings) == 1
        # The measured number, not the cap restated: a caller trimming text
        # needs to know how far over it is.
        assert str(total) in warnings[0]
        assert str(MAX_MESSAGE_CHARACTERS) in warnings[0]
        view.stop()

    def test_it_warns_once_per_view(self, caplog):
        """The seams that call this run on every edit."""
        view = RenderableLayoutView()
        for _ in range(10):
            view.add_item(TextDisplay("a" * 450))

        with caplog.at_level(logging.WARNING, logger="cascadeui"):
            for _ in range(3):
                view._check_placement()

        assert len(self._capture(caplog)) == 1
        view.stop()

    def test_an_over_budget_tree_is_not_rejected(self, caplog):
        """A warning, not a raise.

        Discord documents the cap and discord.py counts it without
        raising, so the enforcing side is unobserved from here. Refusing a
        tree Discord would have accepted is the worse error.
        """
        view = RenderableLayoutView()
        for _ in range(10):
            view.add_item(TextDisplay("a" * 450))

        with caplog.at_level(logging.WARNING, logger="cascadeui"):
            view._check_placement()  # must not raise

        view.stop()

    def test_the_counter_reaches_text_nodes_only(self, caplog):
        """Silence is not proof a tree is under the cap.

        ``content_length`` sums ``TextDisplay`` content and nothing else,
        so a screen carrying its text in button labels, select
        placeholders, and option labels reads as zero against it. The
        documented reach is pinned here because a consumer trusting the
        warning's silence on a control-heavy tree would be trusting a
        fact about the counter rather than about the message.
        """
        view = RenderableLayoutView()
        before = view.content_length()
        view.add_item(ActionRow(StatefulButton(label="L" * 80, custom_id="b")))
        view.add_item(
            ActionRow(
                StatefulSelect(
                    custom_id="s",
                    placeholder="P" * 150,
                    options=[
                        discord.SelectOption(label="O" * 100, value="v", description="D" * 100)
                    ],
                )
            )
        )

        # 430 characters of text a reader can see, none of it in a
        # TextDisplay, so the counter does not move at all.
        assert view.content_length() == before

        with caplog.at_level(logging.WARNING, logger="cascadeui"):
            view._check_placement()

        assert self._capture(caplog) == []
        view.stop()

    def test_a_second_excursion_warns_again(self, caplog):
        """The latch is per excursion, not per view lifetime.

        Reading the latch before measuring would make the reset
        unreachable: a view that warned once would never reach the
        under-cap branch that re-arms it, and a panel regressing over
        budget later would be silent about it.
        """
        view = RenderableLayoutView()
        for _ in range(10):
            view.add_item(TextDisplay("a" * 450))

        with caplog.at_level(logging.WARNING, logger="cascadeui"):
            view._check_placement()
            assert len(self._capture(caplog)) == 1

            # Back under the cap: nothing to report, and the latch re-arms.
            view.clear_items()
            view.add_item(TextDisplay("small"))
            view._check_placement()
            assert len(self._capture(caplog)) == 1

            # Over again, and it is reported rather than swallowed.
            view.clear_items()
            for _ in range(10):
                view.add_item(TextDisplay("b" * 450))
            view._check_placement()
            assert len(self._capture(caplog)) == 2

        view.stop()

    def test_a_tree_under_the_cap_is_silent(self, caplog):
        view = RenderableLayoutView()
        view.add_item(TextDisplay("short"))

        with caplog.at_level(logging.WARNING, logger="cascadeui"):
            view._check_placement()

        assert self._capture(caplog) == []
        view.stop()

    def test_many_small_nodes_cross_a_cap_no_per_node_check_can_see(self, caplog):
        """The gap this closes, stated as its own case.

        Every node here is far under the per-node 4000 limit, so the
        placement validator passes the tree and only the sum is wrong.
        """
        view = RenderableLayoutView()
        for _ in range(10):
            view.add_item(TextDisplay("x" * 450))

        validate_placement(view)  # per-node checks all pass

        with caplog.at_level(logging.WARNING, logger="cascadeui"):
            view._check_placement()

        assert len(self._capture(caplog)) == 1
        view.stop()


class TestStatefulLayoutViewComponentBudget:
    """add_item re-messages discord.py's 40-component cap in the library's style."""

    def test_under_40_adds_normally(self):
        view = StatefulLayoutView()
        view.add_item(Container(*[TextDisplay(f"t{i}") for i in range(10)]))
        assert view._total_children == 11

    def test_over_40_raises_friendly_message(self):
        view = StatefulLayoutView()
        view.add_item(Container(*[TextDisplay(f"a{i}") for i in range(10)]))  # 11 nodes
        with pytest.raises(ValueError) as exc_info:
            view.add_item(Container(*[TextDisplay(f"b{i}") for i in range(40)]))  # 41 nodes
        msg = str(exc_info.value)
        assert "40-component" in msg
        assert "control_buttons" in msg
        # Both sides of the arithmetic, so the reader can see why it failed.
        assert "holds 11 component(s)" in msg
        assert "adds 41 more" in msg
        assert "(52 > 40)" in msg

    def test_message_reports_the_incoming_subtree_not_just_the_view(self):
        """An oversized subtree onto an empty view reports the incoming size.

        The view genuinely holds zero components, so its own count alone
        cannot explain the rejection. The size of what is being added is
        the number that does.
        """
        view = StatefulLayoutView()
        with pytest.raises(ValueError) as exc_info:
            view.add_item(Container(*[TextDisplay(f"t{i}") for i in range(50)]))
        msg = str(exc_info.value)
        assert "holds 0 component(s)" in msg
        assert "Container adds 51 more" in msg
        assert "(51 > 40)" in msg

    def test_message_counts_a_section_accessory(self):
        """Discord counts the accessory; a message that did not would mislead."""
        view = StatefulLayoutView()
        section = Section(TextDisplay("x"), accessory=Thumbnail("https://e.com/i.png"))
        view.add_item(section)
        assert view._total_children == 3  # Section + TextDisplay + Thumbnail

    def test_chains_discord_py_error(self):
        # The friendly error chains discord.py's terse original via `from`.
        view = StatefulLayoutView()
        with pytest.raises(ValueError) as exc_info:
            view.add_item(Container(*[TextDisplay(f"x{i}") for i in range(41)]))
        cause = exc_info.value.__cause__
        assert isinstance(cause, ValueError)
        assert "maximum number of children exceeded" in str(cause)

    def test_unrelated_value_error_passes_through(self, monkeypatch):
        # A ValueError that is NOT the component-budget cap is re-raised
        # untouched -- the override only re-messages the 40-component error.
        def _boom(self, item):
            raise ValueError("some unrelated problem")

        monkeypatch.setattr(LayoutView, "add_item", _boom)
        view = StatefulLayoutView()
        with pytest.raises(ValueError, match="some unrelated problem"):
            view.add_item(TextDisplay("hi"))

    @pytest.mark.parametrize(
        "label,factory",
        [
            ("leaf", lambda: TextDisplay("t")),
            (
                "section_with_accessory",
                lambda: Section(
                    TextDisplay("a"), TextDisplay("b"), accessory=Thumbnail("https://x/a.png")
                ),
            ),
            (
                "action_row",
                lambda: ActionRow(discord.ui.Button(label="a"), discord.ui.Button(label="b")),
            ),
            (
                "container_mixed",
                lambda: Container(
                    TextDisplay("t"),
                    Separator(),
                    ActionRow(discord.ui.Button(label="x")),
                    Section(TextDisplay("s"), accessory=Thumbnail("https://x/b.png")),
                ),
            ),
        ],
    )
    def test_count_components_matches_the_enforcement_count(self, label, factory):
        """The public counter and discord.py's accounting read the same number.

        ``count_components`` hand-walks ``walk_children`` while
        ``total_components`` reads discord.py's ``_total_children``; the
        budget idiom the docs teach adds the two, so a subtree shape either
        counter miscounts (a Section's accessory is the historical one)
        silently breaks every caller's pre-composition check.
        """
        from cascadeui import count_components

        item = factory()
        predicted = count_components(item)

        view = StatefulLayoutView()
        held = view.total_components
        view.add_item(item)

        assert view.total_components - held == predicted


class TestStatefulLayoutViewDispatch:
    """State dispatch and batch tests."""

    async def test_dispatch_forwards_to_store(self):
        interaction = _make_interaction()
        view = StatefulLayoutView(interaction=interaction)

        result = await view.dispatch("VIEW_UPDATED", {"view_id": view.id})
        assert result is not None

    async def test_batch_returns_store_batch(self):
        interaction = _make_interaction()
        view = StatefulLayoutView(interaction=interaction)

        batch = view.batch()
        assert batch is not None

    async def test_scoped_state_empty_without_scope(self):
        interaction = _make_interaction()
        view = StatefulLayoutView(interaction=interaction)

        assert view.scoped_state == {}


class TestStatefulLayoutViewSend:
    """V2 ``StatefulLayoutView.send()`` basic routing, file forwarding,
    and rollback close. Sibling of ``TestStatefulViewSend`` in
    ``test_view_init.py``; same shape, V2 base path.
    """

    async def test_send_via_interaction(self):
        interaction = _make_interaction()
        view = RenderableLayoutView(interaction=interaction)

        message = await view.send()

        interaction.response.send_message.assert_called_once()
        call_kwargs = interaction.response.send_message.call_args
        assert call_kwargs.kwargs["view"] is view

    async def test_send_registers_view(self):
        interaction = _make_interaction()
        view = RenderableLayoutView(interaction=interaction)
        store = get_store()

        await view.send()

        assert view.id in store._active_views

    async def test_send_no_content_embed_params(self):
        """V2 send() has no content/embed/embeds params."""
        interaction = _make_interaction()
        view = RenderableLayoutView(interaction=interaction)

        # Only ephemeral is accepted
        message = await view.send(ephemeral=False)
        assert message is not None

    async def test_send_rollback_on_failure(self):
        interaction = _make_interaction()
        interaction.response.send_message = AsyncMock(side_effect=Exception("fail"))
        view = RenderableLayoutView(interaction=interaction)
        store = get_store()

        with pytest.raises(Exception, match="fail"):
            await view.send()

        assert view.id not in store._active_views

    async def test_send_requires_context_or_interaction(self):
        view = RenderableLayoutView()

        with pytest.raises(RuntimeError, match="requires either"):
            await view.send()

    async def test_send_forwards_files_to_discord(self):
        """``files=`` reaches the underlying send call alongside ``view=``."""
        interaction = _make_interaction()
        view = RenderableLayoutView(interaction=interaction)
        photo = MagicMock(spec=discord.File)

        await view.send(files=[photo])

        call_kwargs = interaction.response.send_message.call_args.kwargs
        assert call_kwargs["files"] == [photo]
        assert call_kwargs["view"] is view

    async def test_send_forwards_single_file_to_discord(self):
        """``file=`` (singular) reaches the underlying send call."""
        interaction = _make_interaction()
        view = RenderableLayoutView(interaction=interaction)
        photo = MagicMock(spec=discord.File)

        await view.send(file=photo)

        call_kwargs = interaction.response.send_message.call_args.kwargs
        assert call_kwargs["file"] is photo

    async def test_send_omits_files_when_unset(self):
        """Send-kwargs carry no ``file``/``files`` keys unless callers supplied them."""
        interaction = _make_interaction()
        view = RenderableLayoutView(interaction=interaction)

        await view.send()

        call_kwargs = interaction.response.send_message.call_args.kwargs
        assert "files" not in call_kwargs
        assert "file" not in call_kwargs

    async def test_send_failure_closes_file_handles(self):
        """Caller-supplied file handles get closed when the send raises before HTTP."""
        interaction = _make_interaction()
        interaction.response.send_message = AsyncMock(side_effect=Exception("fail"))
        view = RenderableLayoutView(interaction=interaction)
        photo1 = MagicMock(spec=discord.File)
        photo2 = MagicMock(spec=discord.File)

        with pytest.raises(Exception, match="fail"):
            await view.send(files=[photo1, photo2])

        photo1.close.assert_called_once()
        photo2.close.assert_called_once()

    async def test_send_failure_closes_singular_file(self):
        """``file=`` (singular) also gets closed on rollback."""
        interaction = _make_interaction()
        interaction.response.send_message = AsyncMock(side_effect=Exception("fail"))
        view = RenderableLayoutView(interaction=interaction)
        photo = MagicMock(spec=discord.File)

        with pytest.raises(Exception, match="fail"):
            await view.send(file=photo)

        photo.close.assert_called_once()

    async def test_a_bare_file_passed_to_files_does_not_strand_the_view(self):
        """``files=`` expects an iterable; a caller who passes one bare
        ``discord.File`` there reaches an iteration of a non-iterable while
        collecting handles to close. The population moved inside the
        pipeline's own try/except so that failure rolls the view all the
        way back instead of raising before registration cleanup ever runs.
        """
        store = get_store()
        photo = discord.File(io.BytesIO(b"x"), filename="pic.png")
        view = RenderableLayoutView(interaction=_make_interaction())

        with pytest.raises(TypeError):
            await view.send(files=photo)  # bare File, not a list -- misuse

        assert view.id not in store._active_views
        assert view.id not in store.state["views"]

    async def test_unmatched_attachment_reference_warns(self, caplog):
        """A reference with no matching file renders a placeholder, silently.

        Discord resolves ``attachment://`` against the files travelling with
        the same message. With no match the message ships and renders an
        unresolved placeholder, returning no error, so the send seam is the
        only place the mismatch is visible.
        """
        view = RenderableLayoutView(interaction=_make_interaction())
        view.add_item(card(gallery("attachment://missing.png")))

        with caplog.at_level(logging.WARNING, logger="cascadeui.views.base"):
            await view.send()

        assert any("attachment://missing.png" in r.getMessage() for r in caplog.records)

    async def test_a_matched_attachment_reference_is_quiet(self, caplog):
        """The file the builder was handed carries the name it emitted.

        A spoiler file is the case worth holding: discord.py prefixes the
        filename, so comparing against anything other than ``File.uri``
        reports a false mismatch on every spoilered attachment.
        """
        photo = discord.File(io.BytesIO(b"x"), filename="pic.png", spoiler=True)
        view = RenderableLayoutView(interaction=_make_interaction())
        view.add_item(card(gallery(photo)))

        with caplog.at_level(logging.WARNING, logger="cascadeui.views.base"):
            await view.send(files=[photo])

        assert not [r for r in caplog.records if "attachment reference" in r.getMessage()]

    async def test_a_remote_url_needs_no_file(self, caplog):
        """Only ``attachment://`` references are half of an upload."""
        view = RenderableLayoutView(interaction=_make_interaction())
        view.add_item(card(gallery("https://cdn.example/a.png")))

        with caplog.at_level(logging.WARNING, logger="cascadeui.views.base"):
            await view.send()

        assert not [r for r in caplog.records if "attachment reference" in r.getMessage()]


class TestSeedInitialState:
    """The seed_initial_state hook fires after registration, before notification."""

    async def test_default_hook_is_no_op(self):
        # The default hook does nothing -- existing views ship unchanged.
        interaction = _make_interaction()
        view = RenderableLayoutView(interaction=interaction)

        message = await view.send()
        assert message is not None

    async def test_hook_receives_state_dict(self):
        interaction = _make_interaction()
        captured = {}

        class SeedingView(RenderableLayoutView):
            async def seed_initial_state(self, state):
                captured["state"] = state

        view = SeedingView(interaction=interaction)
        await view.send()

        assert "state" in captured
        assert isinstance(captured["state"], dict)
        assert "views" in captured["state"]

    async def test_hook_runs_after_register_view(self):
        # When the hook fires, the view is already in the active registry
        # so the slot it seeds can reference its own view_id safely.
        interaction = _make_interaction()
        store = get_store()
        observed = {}

        class SeedingView(RenderableLayoutView):
            async def seed_initial_state(self, state):
                observed["registered"] = self.id in store._active_views

        view = SeedingView(interaction=interaction)
        await view.send()

        assert observed["registered"] is True

    async def test_hook_can_dispatch_inside_send_batch(self):
        # Dispatches from inside the seed hook collapse into the batch's
        # BATCH_COMPLETE notification rather than firing as a separate
        # subscriber pass. Verified by checking the action fires without
        # error inside the send pipeline.
        interaction = _make_interaction()
        store = get_store()

        async def _seed_reducer(action, state):
            new = {**state}
            app = {**state.get("application", {})}
            app["seeded_value"] = action["payload"].get("value")
            new["application"] = app
            return new

        store._register_reducer("SEED_TEST_ACTION", _seed_reducer)

        class SeedingView(RenderableLayoutView):
            async def seed_initial_state(self, state):
                await self.dispatch("SEED_TEST_ACTION", {"value": "seeded"})

        try:
            view = SeedingView(interaction=interaction)
            await view.send()

            assert store.state["application"].get("seeded_value") == "seeded"
        finally:
            store._unregister_reducer("SEED_TEST_ACTION")

    async def test_hook_failure_propagates(self):
        # If the override raises, the send pipeline surfaces the error. No
        # silent swallowing -- a broken seed should break the send so the
        # subclass author sees the bug immediately.
        interaction = _make_interaction()

        class BrokenSeedView(RenderableLayoutView):
            async def seed_initial_state(self, state):
                raise RuntimeError("seed broke")

        view = BrokenSeedView(interaction=interaction)
        with pytest.raises(RuntimeError, match="seed broke"):
            await view.send()

    async def test_seed_can_build_the_component_tree(self):
        # A view whose tree is built ONLY in seed_initial_state must still
        # pass placement validation and send. Placement runs on the final
        # tree at the Discord-send stage, after seed has populated it -- not
        # before registration, where the tree is still empty and the check
        # would reject it as "no top-level components".
        interaction = _make_interaction()

        class SeedBuildsUIView(StatefulLayoutView):
            async def seed_initial_state(self, state):
                self.add_item(Container(TextDisplay("built in seed")))

        view = SeedBuildsUIView(interaction=interaction)
        message = await view.send()

        assert message is not None
        assert any(isinstance(child, Container) for child in view.children)


class TestOnLoadHook:
    """The on_load hook runs an async preload before the view is displayed.

    Sibling of seed_initial_state, but earlier in the pipeline: on_load
    fires before placement validation and the Discord send so the first
    render reflects loaded data, where seed_initial_state seeds state
    slots after registration.
    """

    async def test_default_hook_is_no_op(self):
        # Views without async preload ship unchanged -- the default does nothing.
        interaction = _make_interaction()
        view = RenderableLayoutView(interaction=interaction)

        message = await view.send()
        assert message is not None

    async def test_hook_runs_during_send(self):
        interaction = _make_interaction()
        observed = {}

        class LoadingView(RenderableLayoutView):
            async def on_load(self):
                observed["loaded"] = True

        view = LoadingView(interaction=interaction)
        await view.send()

        assert observed.get("loaded") is True

    async def test_hook_runs_before_message_ships(self):
        # The preload completes before the Discord send, so a view can
        # build its tree against loaded data and have that be what ships.
        interaction = _make_interaction()
        order = []

        original_send = interaction.response.send_message

        async def _tracking_send(*args, **kwargs):
            order.append("send")
            return await original_send(*args, **kwargs)

        interaction.response.send_message = AsyncMock(side_effect=_tracking_send)

        class LoadingView(RenderableLayoutView):
            async def on_load(self):
                order.append("on_load")

        view = LoadingView(interaction=interaction)
        await view.send()

        assert order == ["on_load", "send"]

    async def test_hook_runs_before_placement_check(self):
        # on_load builds the tree, so it must run before _check_placement
        # validates it. Adding a child in on_load and asserting the
        # placement check saw it proves the ordering.
        interaction = _make_interaction()
        seen_children = {}

        class LoadingView(StatefulLayoutView):
            async def on_load(self):
                self.add_item(ActionRow(StatefulButton(label="Loaded")))

            def _check_placement(self):
                seen_children["count"] = len(list(self.children))
                return super()._check_placement()

        view = LoadingView(interaction=interaction)
        await view.send()

        assert seen_children.get("count", 0) >= 1

    async def test_hook_failure_propagates(self):
        # A broken preload breaks the send so the subclass author sees it.
        interaction = _make_interaction()

        class BrokenLoadView(StatefulLayoutView):
            async def on_load(self):
                raise RuntimeError("load broke")

        view = BrokenLoadView(interaction=interaction)
        with pytest.raises(RuntimeError, match="load broke"):
            await view.send()

    async def test_reload_runs_on_load_then_refresh(self):
        # reload() is the out-of-band convenience: on_load, then refresh.
        interaction = _make_interaction()
        order = []

        class LoadingView(RenderableLayoutView):
            async def on_load(self):
                order.append("on_load")

            async def refresh(self, **kwargs):
                order.append("refresh")

        view = LoadingView(interaction=interaction)
        await view.send()
        order.clear()

        await view.reload()

        assert order == ["on_load", "refresh"]


class TestReloadThrottleCoalescing:
    """reload() respects the refresh throttle at the reload layer, so a burst of
    out-of-band reloads inside a cooldown collapses to one on_load fetch (B2)."""

    async def test_reload_without_cooldown_fetches_immediately(self):
        interaction = _make_interaction()
        loads = []

        class LoadingView(RenderableLayoutView):
            async def on_load(self):
                loads.append(1)

            async def refresh(self, **kwargs):
                pass

        view = LoadingView(interaction=interaction)
        await view.reload()
        assert loads == [1]

    async def test_reload_during_cooldown_defers_the_fetch(self):
        import time

        interaction = _make_interaction()
        loads = []

        class LoadingView(RenderableLayoutView):
            async def on_load(self):
                loads.append(1)

            async def refresh(self, **kwargs):
                pass

        view = LoadingView(interaction=interaction)
        # Simulate an active cooldown window.
        view._cooldown_not_before = time.monotonic() + 30

        await view.reload()
        # The fetch was deferred, not run.
        assert loads == []
        assert view._reload_pending is True
        assert view._deferred_refresh_task is not None

        # A second reload in the window neither fetches nor spawns a 2nd task.
        first_task = view._deferred_refresh_task
        await view.reload()
        assert loads == []
        assert view._deferred_refresh_task is first_task

        # Let the event loop pick up the task so its coroutine enters the sleep
        # before cancellation -- avoids a "never awaited" warning.
        await asyncio.sleep(0)
        view._deferred_refresh_task.cancel()
        try:
            await view._deferred_refresh_task
        except asyncio.CancelledError:
            pass

    async def test_deferred_reload_replays_captured_kwargs(self):
        """A coalesced reload's keyword args are replayed at the boundary, so a
        forwarded keyword (e.g. force) survives the defer."""
        interaction = _make_interaction()
        replayed = []

        class KwargView(RenderableLayoutView):
            async def reload(self, **kwargs):
                replayed.append(kwargs)  # capture the replay; don't re-defer

            async def on_load(self):
                pass

        view = KwargView(interaction=interaction)
        view._message = MagicMock()
        view._reload_pending = True
        view._pending_reload_kwargs = {"force": True}

        await view._deferred_refresh(0)

        assert replayed == [{"force": True}]
        assert view._pending_reload_kwargs == {}


class TestReloadSerialization:
    """Overlapping ``reload()`` calls run one at a time on the reload lock.

    Interactions serialize on ``_interaction_lock`` and notifications coalesce
    on ``_update_lock``, but ``reload()`` is reachable from paths neither
    covers (a background task, callbacks on two different interactions). Two
    ``on_load`` bodies interleaving on one view read and stamp each other's
    half-built state; the lock makes each run atomic, and each queued reload
    re-fetches at run time so the last one to run renders the freshest data.
    """

    async def test_concurrent_reloads_do_not_interleave_on_load(self):
        interaction = _make_interaction()
        order = []
        gate = asyncio.Event()

        class ParkingLoader(RenderableLayoutView):
            _parked = False

            async def on_load(self):
                order.append("start")
                if not type(self)._parked:
                    type(self)._parked = True
                    await gate.wait()
                order.append("end")

            async def refresh(self, **kwargs):
                pass

        view = ParkingLoader(interaction=interaction)
        first = asyncio.create_task(view.reload())
        await asyncio.sleep(0)  # first enters on_load and parks on the gate
        second = asyncio.create_task(view.reload())
        # Drain the ready queue so the second reload runs as far as it can get.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        # The second reload has not entered on_load while the first is parked
        # mid-fetch, and the lock is what held it out.
        assert order == ["start"]
        assert view._reload_lock.locked()

        gate.set()
        await first
        await second
        # Serialized: each run completes before the next begins.
        assert order == ["start", "end", "start", "end"]

    async def test_older_reload_cannot_overwrite_a_newer_ones_fetch(self):
        """A build that started first and stalls mid-fetch must not finish
        after a later build and overwrite its rows: serialized reloads run in
        start order, so the last reload to run fetched last."""
        interaction = _make_interaction()
        gate = asyncio.Event()

        class SlowThenFast(RenderableLayoutView):
            rows = None
            _parked = False

            async def on_load(self):
                if not type(self)._parked:
                    type(self)._parked = True
                    await gate.wait()
                    self.rows = "first-fetch"
                else:
                    self.rows = "second-fetch"

            async def refresh(self, **kwargs):
                pass

        view = SlowThenFast(interaction=interaction)
        first = asyncio.create_task(view.reload())
        await asyncio.sleep(0)  # first parks mid-fetch
        second = asyncio.create_task(view.reload())
        await asyncio.sleep(0)
        gate.set()
        await first
        await second

        assert view.rows == "second-fetch"

    async def test_reload_from_inside_on_load_raises_a_directed_error(self):
        """reload() inside its own on_load would deadlock on the lock it
        already holds (and recursed unboundedly before the lock existed), so
        the reentrancy is rejected with an error naming the mistake."""
        interaction = _make_interaction()

        class Reentrant(RenderableLayoutView):
            async def on_load(self):
                await self.reload()

            async def refresh(self, **kwargs):
                pass

        view = Reentrant(interaction=interaction)
        with pytest.raises(
            RuntimeError, match=r"reload\(\) called while reload\(\) holds Reentrant's reload turn"
        ):
            await view.reload()
        # The failed run released the lock; the view is not wedged.
        assert not view._reload_lock.locked()

    @staticmethod
    def _reaching_pair(gate):
        """Two views whose first on_load run parks on ``gate`` and then reaches
        the partner (``reach`` names the method); later runs only load."""

        class Reaching(RenderableLayoutView):
            partner = None
            reach = "reload"

            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.reached = False

            async def on_load(self):
                if not self.reached:
                    self.reached = True
                    await gate.wait()
                    await getattr(self.partner, self.reach)()

            async def refresh(self, **kwargs):
                pass

        class Rankings(Reaching):
            pass

        class Controls(Reaching):
            reach = "load"

        rankings = Rankings(interaction=_make_interaction())
        controls = Controls(interaction=_make_interaction())
        rankings.partner, controls.partner = controls, rankings
        return rankings, controls

    async def test_two_reloads_awaiting_each_other_raise_instead_of_hanging(self):
        """Each view holds its own lock and then waits on the other's. With
        nothing to break it the pair hung for the life of the process; the
        reload that closes the cycle now raises, and the other completes."""
        from cascadeui.views.base import _RELOAD_WAITING

        gate = asyncio.Event()
        rankings, controls = self._reaching_pair(gate)
        first = asyncio.create_task(rankings.reload())
        second = asyncio.create_task(controls.reload())
        await asyncio.sleep(0)  # both hold their own lock, parked on the gate
        gate.set()
        results = await asyncio.wait_for(
            asyncio.gather(first, second, return_exceptions=True), timeout=2
        )

        errors = [r for r in results if isinstance(r, RuntimeError)]
        assert len(errors) == 1
        assert "would wait forever" in str(errors[0])
        assert not rankings._reload_lock.locked()
        assert not controls._reload_lock.locked()
        assert _RELOAD_WAITING == {}

    async def test_a_holder_queued_on_an_unrelated_view_is_waited_for(self):
        """The cycle walk must only refuse a chain that returns to the caller.
        A holder queued behind a third task's reload finishes, so the caller
        queues normally."""
        from cascadeui.views.base import _RELOAD_WAITING

        third_gate = asyncio.Event()
        order = []

        class Parked(RenderableLayoutView):
            async def on_load(self):
                await third_gate.wait()
                order.append("third")

            async def refresh(self, **kwargs):
                pass

        class Reaching(RenderableLayoutView):
            other = None
            reached = False

            async def on_load(self):
                if not type(self).reached:
                    type(self).reached = True
                    await self.other.load()
                order.append("reaching")

            async def refresh(self, **kwargs):
                pass

        third = Parked(interaction=_make_interaction())
        reaching = Reaching(interaction=_make_interaction())
        reaching.other = third
        held = asyncio.create_task(third.reload())
        await asyncio.sleep(0)  # the third view holds its lock, parked
        queued = asyncio.create_task(reaching.reload())
        await asyncio.sleep(0)  # reaching holds its lock, queued on the third view
        caller = asyncio.create_task(reaching.reload())
        await asyncio.sleep(0)  # queues behind reaching, which is not waiting on it
        assert _RELOAD_WAITING.get(queued) is third

        third_gate.set()
        await asyncio.wait_for(asyncio.gather(held, queued, caller), timeout=2)
        assert order == ["third", "third", "reaching", "reaching"]

    async def test_a_cross_view_reentry_in_one_task_names_the_view(self):
        """One task reloading a view whose on_load reaches a partner whose
        on_load reaches back: the lock this task already holds would never
        free, so the reentry raises naming the view it came back to."""
        gate = asyncio.Event()
        gate.set()
        rankings, controls = self._reaching_pair(gate)
        with pytest.raises(
            RuntimeError, match=r"load\(\) called while reload\(\) holds Rankings's reload turn"
        ):
            await rankings.reload()
        assert not rankings._reload_lock.locked()
        assert not controls._reload_lock.locked()

    @staticmethod
    def _gated_panel(gate, first_label, later_label):
        """A panel whose first on_load builds at once and whose later ones wait
        on ``gate``, each building a fresh live button."""

        class Panel(RenderableLayoutView):
            loads = 0

            def _build(self, label):
                self.clear_items()
                self.add_item(TextDisplay(label))

                async def noop(interaction):
                    pass

                self.add_item(ActionRow(StatefulButton(label="Go", custom_id="go", callback=noop)))

            async def on_load(self):
                self.loads += 1
                if self.loads == 1:
                    self._build(first_label)
                    return
                await gate.wait()
                self._build(later_label)

        return Panel

    async def test_a_reload_that_outlives_an_exit_leaves_the_frozen_panel_alone(self):
        """A reload from a task the view does not own (a consumer's refresh
        loop) finished after exit() froze the panel and shipped live buttons
        onto it."""
        gate = asyncio.Event()
        view = self._gated_panel(gate, "initial", "reloaded")(interaction=_make_interaction())
        await view.send()
        in_flight = asyncio.create_task(view.reload())
        await asyncio.sleep(0)
        queued = asyncio.create_task(view.reload())
        await asyncio.sleep(0)
        await view.exit()
        edits = view._message.edit.await_count

        gate.set()
        outcomes = await asyncio.wait_for(asyncio.gather(in_flight, queued), timeout=2)

        assert outcomes == [RenderOutcome.NO_MESSAGE, RenderOutcome.NO_MESSAGE]
        assert view._message.edit.await_count == edits
        assert view.loads == 2  # the queued reload never ran on_load on the dead view

    async def test_a_reload_that_outlives_a_push_leaves_the_destination_alone(self):
        gate = asyncio.Event()
        source = self._gated_panel(gate, "source", "source reloaded")(
            interaction=_make_interaction()
        )
        await source.send()
        message = source._message
        reload = asyncio.create_task(source.reload())
        await asyncio.sleep(0)
        click = _make_interaction(message=MagicMock(id=message.id))
        await source.push(RenderableLayoutView, click)
        edits = message.edit.await_count

        gate.set()

        assert await asyncio.wait_for(reload, timeout=2) is RenderOutcome.NO_MESSAGE
        assert message.edit.await_count == edits

    async def test_a_reload_declined_during_a_failed_push_runs_after_the_rollback(self):
        """The source was unsubscribed while its push was in flight, so a
        reload then read it as torn down. When the push failed and the source
        came back, that reload was simply gone."""
        gate = asyncio.Event()

        class Source(RenderableLayoutView):
            loads = 0

            async def on_load(self):
                self.loads += 1

        class Dest(RenderableLayoutView):
            async def on_load(self):
                await gate.wait()

        source = Source(interaction=_make_interaction(user_id=1, guild_id=100))
        await source.send()
        error = discord.HTTPException(MagicMock(status=503), "boom")
        nav = _make_interaction(user_id=1, guild_id=100)
        nav.response.edit_message = AsyncMock(side_effect=error)
        nav.response.defer = AsyncMock(side_effect=error)
        nav.edit_original_response = AsyncMock(side_effect=error)
        source._message.edit = AsyncMock(side_effect=error)
        push = asyncio.create_task(source.push(Dest, interaction=nav))
        await asyncio.sleep(0.05)  # parked in Dest.on_load: the source is away

        assert await source.reload() is RenderOutcome.DEFERRED
        gate.set()
        await asyncio.wait_for(push, timeout=2)
        await asyncio.wait_for(source.task_manager.wait_tasks(source.id), timeout=2)

        assert not source._torn_down()
        assert source.loads == 2


class TestLoad:
    """``load()`` runs the serialized ``on_load`` without the render.

    ``reload()`` was the only thing preventing two ``on_load`` bodies from
    interleaving, and it always rendered or deferred, so a caller that needed
    the load serialized with no edit (a view whose data another view reads)
    had to hand-roll a second lock.
    """

    @staticmethod
    def _sent(cls):
        view = cls(interaction=_make_interaction())
        message = MagicMock()
        message.id = 999
        message.edit = AsyncMock()
        view._message = message
        view._webhook_message = None
        return view

    async def test_load_runs_on_load_and_edits_nothing(self):
        calls = []

        class Loader(RenderableLayoutView):
            async def on_load(self):
                calls.append("on_load")

        view = self._sent(Loader)
        assert await view.load() is True
        assert calls == ["on_load"]
        view._message.edit.assert_not_awaited()

    async def test_load_runs_inside_a_throttle_window(self):
        """The reload gate paces fetches that each end in an edit. A load ships
        nothing, so it neither waits for the window nor schedules a retry."""
        calls = []

        class Loader(RenderableLayoutView):
            async def on_load(self):
                calls.append("on_load")

        view = self._sent(Loader)
        view._cooldown_not_before = time.monotonic() + 60

        assert await view.load() is True
        assert calls == ["on_load"]
        assert view._deferred_refresh_task is None
        assert view._reload_pending is False

    async def test_load_queues_behind_a_reload_in_progress(self):
        order = []
        gate = asyncio.Event()

        class Loader(RenderableLayoutView):
            _parked = False

            async def on_load(self):
                order.append("start")
                if not type(self)._parked:
                    type(self)._parked = True
                    await gate.wait()
                order.append("end")

            async def refresh(self, **kwargs):
                pass

        view = self._sent(Loader)
        reload_task = asyncio.create_task(view.reload())
        await asyncio.sleep(0)  # the reload parks mid-fetch
        load_task = asyncio.create_task(view.load())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert order == ["start"]

        gate.set()
        await reload_task
        assert await load_task is True
        assert order == ["start", "end", "start", "end"]

    async def test_load_skips_on_load_while_armed(self):
        """Re-running on_load would rebuild the tree over the refresh button."""
        calls = []

        class Loader(RenderableLayoutView):
            async def on_load(self):
                calls.append("on_load")

        view = self._sent(Loader)
        view._refresh_armed = True

        assert await view.load() is False
        assert calls == []
        view._message.edit.assert_not_awaited()

    async def test_load_from_inside_its_own_on_load_raises(self):
        class Reentrant(RenderableLayoutView):
            async def on_load(self):
                await self.load()

        view = self._sent(Reentrant)
        with pytest.raises(
            RuntimeError, match=r"load\(\) called while load\(\) holds Reentrant's reload turn"
        ):
            await view.load()
        assert not view._reload_lock.locked()

    async def test_a_cancelled_waiter_leaves_the_lock_usable(self):
        """A load cancelled while queued never held the lock, so it must not
        release it, and it must not stay listed as waiting."""
        from cascadeui.views.base import _RELOAD_WAITING

        gate = asyncio.Event()

        class Loader(RenderableLayoutView):
            async def on_load(self):
                await gate.wait()

            async def refresh(self, **kwargs):
                pass

        view = self._sent(Loader)
        holder = asyncio.create_task(view.reload())
        await asyncio.sleep(0)
        waiter = asyncio.create_task(view.load())
        await asyncio.sleep(0)
        assert _RELOAD_WAITING.get(waiter) is view

        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert waiter not in _RELOAD_WAITING
        assert view._reload_lock.locked()  # still the holder's

        gate.set()
        await holder
        assert not view._reload_lock.locked()
        assert await view.load() is True

    async def test_a_load_whose_turn_comes_after_an_exit_does_not_run(self):
        """An on_load run on a torn-down view reads state the teardown cleared."""
        gate = asyncio.Event()
        loads = []

        class Loader(RenderableLayoutView):
            async def on_load(self):
                loads.append("load")
                if len(loads) == 2:
                    await gate.wait()

        view = Loader(interaction=_make_interaction())
        await view.send()
        reloading = asyncio.create_task(view.reload())
        await asyncio.sleep(0)
        loading = asyncio.create_task(view.load())
        await asyncio.sleep(0)
        await view.exit()
        gate.set()
        await asyncio.wait_for(reloading, timeout=2)

        assert await asyncio.wait_for(loading, timeout=2) is False
        assert loads == ["load", "load"]


class TestRenderSeamsHoldTheReloadTurn:
    """The send and push/pop run ``on_load`` and ship the tree under the
    view's reload turn, and record as the render baseline the tree they
    actually serialized.

    Both seams ran ``on_load`` outside the lock ``reload()`` takes, so a
    reload another task started meanwhile (the leaderboard's avatar backfill,
    scheduled from inside the send's own ``on_load``) ran a second
    ``on_load`` beside the first. Separately, every baseline was digested
    after the request's await, so a rebuild while the request was in flight
    was certified as shipped and every later unchanged refresh skipped.
    """

    @staticmethod
    def _park_send(interaction):
        """Hold the send's request in flight until ``release`` is set, recording
        the tree it serialized when the request was built."""
        in_flight = asyncio.Event()
        release = asyncio.Event()
        shipped = []

        async def send_message(**kwargs):
            shipped.append(kwargs["view"].to_components())
            interaction.response.is_done.return_value = True
            in_flight.set()
            await release.wait()

        interaction.response.send_message = AsyncMock(side_effect=send_message)
        return in_flight, release, shipped

    @staticmethod
    def _texts(components):
        return [c.get("content") for c in components if c.get("type") == 10]

    async def test_a_reload_started_inside_the_sends_on_load_waits_for_it(self):
        gate = asyncio.Event()
        running = {"now": 0, "most": 0}
        spawned = []

        class Loader(RenderableLayoutView):
            async def on_load(self):
                running["now"] += 1
                running["most"] = max(running["most"], running["now"])
                try:
                    if not spawned:
                        spawned.append(asyncio.create_task(self.reload()))
                        await asyncio.sleep(0)  # the spawned reload tries to start
                        await gate.wait()
                finally:
                    running["now"] -= 1

        view = Loader(interaction=_make_interaction())
        send = asyncio.create_task(view.send())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        gate.set()
        await asyncio.wait_for(asyncio.gather(send, *spawned), timeout=2)

        assert running["most"] == 1

    async def test_a_reload_during_delivery_renders_after_the_message_exists(self):
        interaction = _make_interaction()
        in_flight, release, _ = self._park_send(interaction)
        loads = []

        class Loader(RenderableLayoutView):
            async def on_load(self):
                loads.append(self._message is not None)

        view = Loader(interaction=interaction)
        send = asyncio.create_task(view.send())
        await asyncio.wait_for(in_flight.wait(), timeout=2)
        reload = asyncio.create_task(view.reload())
        await asyncio.sleep(0)
        assert loads == [False]  # queued behind delivery

        release.set()
        await asyncio.wait_for(send, timeout=2)
        outcome = await asyncio.wait_for(reload, timeout=2)
        assert loads == [False, True]
        assert outcome is not RenderOutcome.NO_MESSAGE

    async def test_a_state_change_during_delivery_reaches_the_message(self):
        """The notification arrived while the send held the reload turn, so its
        render was deferred and replayed once the turn released, after the
        message existed. A later unchanged refresh skips against the tree
        Discord actually has."""
        store = get_store()

        async def bump(action, state):
            return {**state, "application": {**state["application"], "count": 1}}

        store._register_reducer("COUNT_BUMPED", bump)

        class Counter(StatefulLayoutView):
            subscribed_actions = {"COUNT_BUMPED"}

            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.build_ui()

            def build_ui(self):
                self.clear_items()
                count = self.state_store.state["application"].get("count", 0)
                self.add_item(TextDisplay(f"count={count}"))

            def state_selector(self, state):
                return state["application"].get("count")

        interaction = _make_interaction()
        in_flight, release, shipped = self._park_send(interaction)
        view = Counter(interaction=interaction)
        send = asyncio.create_task(view.send())
        await asyncio.wait_for(in_flight.wait(), timeout=2)

        await store.dispatch("COUNT_BUMPED", {})
        await store._flush_notifications()
        release.set()
        await asyncio.wait_for(send, timeout=2)
        await asyncio.wait_for(view.task_manager.wait_tasks(view.id), timeout=2)

        message = view._message
        assert self._texts(shipped[0]) == ["count=0"]
        message.edit.assert_awaited_once()
        assert self._texts(message.edit.await_args.kwargs["view"].to_components()) == ["count=1"]
        assert await view.refresh() is RenderOutcome.SKIPPED

    async def test_a_render_dropped_before_delivery_is_not_replayed(self):
        """A render that finds no message before the send begins is covered by
        the send itself, which serializes the tree as it then stands; only a
        drop inside the delivery window owes a follow-up edit."""
        view = RenderableLayoutView(interaction=_make_interaction())
        assert await view.refresh() is RenderOutcome.NO_MESSAGE
        # The replay's own edit would skip on an unchanged tree, so the replay
        # is observed at the render it runs, which for a V1 view passing
        # embed= would ship a redundant edit.
        view.on_state_changed = AsyncMock()

        await view.send()

        view.on_state_changed.assert_not_awaited()
        view._message.edit.assert_not_awaited()

    async def test_a_refresh_baseline_certifies_the_tree_it_sent(self):
        view = RenderableLayoutView(interaction=_make_interaction())
        view.clear_items()
        view.add_item(TextDisplay("first"))
        message = MagicMock()
        message.id = 999

        async def edit(**kwargs):
            # Rebuilt while this edit is in flight.
            view.clear_items()
            view.add_item(TextDisplay("second"))

        message.edit = AsyncMock(side_effect=edit)
        view._message = message
        view._webhook_message = None

        assert await view.refresh() is RenderOutcome.RENDERED
        # Discord holds "first"; the live tree says "second", so this ships.
        assert await view.refresh() is RenderOutcome.RENDERED
        assert message.edit.await_count == 2

    async def test_a_reload_started_during_navigation_waits_for_the_edit(self):
        order = []
        gate = asyncio.Event()
        spawned = []

        class Destination(RenderableLayoutView):
            async def on_load(self):
                order.append("load")
                if not spawned:
                    spawned.append(asyncio.create_task(self.reload()))
                    await asyncio.sleep(0)
                    await gate.wait()

        source = RenderableLayoutView(interaction=_make_interaction())
        await source.send()
        click = _make_interaction()
        click.response.edit_message = AsyncMock(side_effect=lambda **kw: order.append("edit"))

        push = asyncio.create_task(source.push(Destination, click))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        gate.set()
        await asyncio.wait_for(asyncio.gather(push, *spawned), timeout=2)

        assert order == ["load", "edit", "load"]

    async def test_a_notification_while_another_task_holds_the_turn_renders_after_it(self):
        """A dispatch made from a task on_load spawned notifies inline in that
        task, not the one holding the turn. Rendering there ran beside the
        holder, and waiting for the turn deadlocked whenever the holder was
        awaiting the notifying task, so the render is replayed at release."""
        store = get_store()

        async def ping(action, state):
            return {**state, "application": {**state["application"], "pinged": True}}

        store._register_reducer("TURN_PINGED", ping)
        renders = []

        class Loader(RenderableLayoutView):
            subscribed_actions = {"TURN_PINGED"}

            async def on_load(self):
                if not renders:
                    await asyncio.gather(self.dispatch("TURN_PINGED", {}))

            async def on_state_changed(self, state):
                renders.append(self._reload_task)

            def state_selector(self, state):
                return state["application"].get("pinged")

        view = Loader(interaction=_make_interaction())
        assert await asyncio.wait_for(view.load(), timeout=2) is True
        await asyncio.wait_for(view.task_manager.wait_tasks(view.id), timeout=2)

        assert renders == [None]  # once, after the turn was released

    async def test_the_send_backstop_covers_a_wait_for_the_reload_turn(self):
        """A slash-command send has no click timer behind it, so a send queued
        behind another task's load is acked by the send-scoped backstop."""

        class Loader(RenderableLayoutView):
            auto_defer_delay = 0.1
            calls = 0

            async def on_load(self):
                type(self).calls += 1
                if type(self).calls == 1:  # the load() already holding the turn
                    await asyncio.sleep(0.4)

        interaction = _make_interaction()
        interaction.type = discord.InteractionType.application_command
        interaction.followup.send = AsyncMock(
            return_value=interaction.original_response.return_value
        )
        view = Loader(interaction=interaction)
        background = asyncio.create_task(view.load())
        await asyncio.sleep(0)

        await asyncio.wait_for(view.send(), timeout=3)
        await asyncio.wait_for(background, timeout=3)

        interaction.response.defer.assert_awaited_once()
        interaction.response.send_message.assert_not_awaited()
        interaction.followup.send.assert_awaited_once()

    async def test_a_send_cancelled_while_waiting_for_the_turn_rolls_back(self):
        class Loader(RenderableLayoutView):
            calls = 0

            async def on_load(self):
                type(self).calls += 1
                if type(self).calls == 1:
                    await asyncio.sleep(5)

        view = Loader(interaction=_make_interaction())
        store = view.state_store
        background = asyncio.create_task(view.load())
        await asyncio.sleep(0)
        try:
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(view.send(), timeout=0.3)
        finally:
            background.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await background

        assert view.id not in store._active_views
        assert view.id not in store.subscribers
        assert view.is_finished()

    async def test_a_send_that_bails_releases_the_turn(self):
        class Vetoed(RenderableLayoutView):
            async def on_load(self):
                pass

            async def seed_initial_state(self, state):
                raise RuntimeError("seed failed")

        view = Vetoed(interaction=_make_interaction())
        with pytest.raises(RuntimeError, match="seed failed"):
            await view.send()

        assert not view._reload_lock.locked()
        assert view._sending_task is None

    async def test_a_reload_during_registration_ships_with_the_send(self):
        """Registration runs without the turn, so a reload another task starts
        there runs at once. Its fetch lands in the tree the send then ships,
        and it reports DEFERRED rather than that there is no message."""
        spawned = []

        class Loader(RenderableLayoutView):
            loads = 0

            async def on_load(self):
                self.loads += 1
                self.clear_items()
                self.add_item(TextDisplay(f"load {self.loads}"))

            async def seed_initial_state(self, state):
                spawned.append(asyncio.create_task(self.reload()))
                await asyncio.sleep(0)

        interaction = _make_interaction()
        _, release, shipped = self._park_send(interaction)
        release.set()
        view = Loader(interaction=interaction)
        await asyncio.wait_for(view.send(), timeout=2)
        outcome = await asyncio.wait_for(spawned[0], timeout=2)

        assert outcome is RenderOutcome.DEFERRED
        assert self._texts(shipped[0]) == ["load 2"]

    async def test_a_hook_that_awaits_a_reload_of_the_sending_view_does_not_hang(self):
        """Code the send runs while registering (a store.on() hook here) may
        await a task that reloads the view being sent. A send holding the
        reload turn through registration left that task waiting on it
        forever."""
        store = get_store()

        class Panel(RenderableLayoutView):
            loads = 0

            async def on_load(self):
                self.loads += 1

        async def hook(action, state):
            panels = [v for v in store.get_active_views().values() if isinstance(v, Panel)]
            await asyncio.gather(*(panel.reload() for panel in panels))

        store.on("VIEW_CREATED", hook)
        try:
            view = Panel(interaction=_make_interaction())
            assert await asyncio.wait_for(view.send(), timeout=2) is not None
            assert view.loads == 2
        finally:
            store.off("VIEW_CREATED", hook)

    async def test_a_failed_send_rolls_back_without_waiting_on_its_own_turn(self):
        """The rollback dispatches VIEW_DESTROYED, whose hooks run inline, and
        one that reloaded the view being rolled back waited on the turn the
        failed send still held."""
        store = get_store()

        class Empty(StatefulLayoutView):
            async def on_load(self):
                self.clear_items()  # an empty V2 tree fails placement at delivery

        async def hook(action, state):
            views = [v for v in store.get_active_views().values() if isinstance(v, Empty)]
            await asyncio.gather(*(v.reload() for v in views))

        store.on("VIEW_DESTROYED", hook)
        try:
            view = Empty(interaction=_make_interaction())
            with pytest.raises(ValueError, match="no top-level components"):
                await asyncio.wait_for(view.send(), timeout=2)
        finally:
            store.off("VIEW_DESTROYED", hook)

    async def test_the_sends_own_reload_during_registration_runs(self):
        """An inline notification from the registration dispatch runs in the
        send's task, and registration holds no turn, so its reload runs."""
        loads = []

        class Loader(RenderableLayoutView):
            async def on_load(self):
                loads.append("load")

            async def seed_initial_state(self, state):
                await self.reload()

        view = Loader(interaction=_make_interaction())
        await asyncio.wait_for(view.send(), timeout=2)

        assert loads == ["load", "load"]

    async def test_a_reload_inside_the_sends_own_reload_still_raises(self):
        class Loader(RenderableLayoutView):
            joined = False

            async def on_load(self):
                if self.joined:
                    await self.reload()

            async def seed_initial_state(self, state):
                self.joined = True
                await self.reload()

        view = Loader(interaction=_make_interaction())
        with pytest.raises(RuntimeError, match=r"reload\(\) called while reload\(\) holds"):
            await asyncio.wait_for(view.send(), timeout=2)
        assert view.id not in view.state_store.subscribers

    @staticmethod
    def _slow_rebuild_panel(action_type):
        """A panel whose rebuild awaits between clearing its tree and refilling it."""
        store = get_store()

        async def bump(action, state):
            return {**state, "application": {**state["application"], "count": 1}}

        store._register_reducer(action_type, bump)

        class Panel(StatefulLayoutView):
            subscribed_actions = {action_type}

            def state_selector(self, state):
                return state["application"].get("count")

            async def build_ui(self):
                self.clear_items()
                await asyncio.sleep(0.05)
                count = self.state_store.state["application"].get("count", 0)
                self.add_item(TextDisplay(f"count={count}"))

        return store, Panel

    async def test_a_state_change_during_the_sends_on_load_waits_for_the_message(self):
        """The deferred render used to replay when the on_load stage released
        the turn, while the send was still registering. A rebuild suspended
        between clear and refill then left Stage 5 validating an empty tree,
        and the send failed on a tree the view never meant to ship."""
        store, Panel = self._slow_rebuild_panel("LOAD_WINDOW_BUMPED")
        in_load = asyncio.Event()
        gate = asyncio.Event()

        class Loader(Panel):
            async def on_load(self):
                in_load.set()
                await gate.wait()
                await self.build_ui()

            async def seed_initial_state(self, state):
                await asyncio.sleep(0.01)

        interaction = _make_interaction()
        _, release, shipped = self._park_send(interaction)
        release.set()
        view = Loader(interaction=interaction)
        send = asyncio.create_task(view.send())
        await asyncio.wait_for(in_load.wait(), timeout=2)
        await store.dispatch("LOAD_WINDOW_BUMPED", {})
        await store._flush_notifications()
        gate.set()

        await asyncio.wait_for(send, timeout=2)
        await asyncio.wait_for(view.task_manager.wait_tasks(view.id), timeout=2)
        assert self._texts(shipped[0]) == ["count=1"]

    async def test_a_state_change_during_registration_waits_for_the_message(self):
        store, Panel = self._slow_rebuild_panel("REGISTRATION_BUMPED")
        in_seed = asyncio.Event()
        seed_gate = asyncio.Event()

        class Loader(Panel):
            async def on_load(self):
                await self.build_ui()

            async def seed_initial_state(self, state):
                in_seed.set()
                await seed_gate.wait()

        interaction = _make_interaction()
        _, release, shipped = self._park_send(interaction)
        release.set()
        view = Loader(interaction=interaction)
        send = asyncio.create_task(view.send())
        await asyncio.wait_for(in_seed.wait(), timeout=2)
        await store.dispatch("REGISTRATION_BUMPED", {})
        await asyncio.sleep(0)  # a render that ran now would be mid-rebuild at Stage 5
        seed_gate.set()

        await asyncio.wait_for(send, timeout=2)
        await store._flush_notifications()
        await asyncio.wait_for(view.task_manager.wait_tasks(view.id), timeout=2)
        assert self._texts(shipped[0]) == ["count=0"]
        edits = view._message.edit.await_args_list
        assert self._texts(edits[-1].kwargs["view"].to_components()) == ["count=1"]

    @staticmethod
    def _replayed_view(action_type, on_state_changed):
        """A view whose on_load dispatches from a gathered task, so the
        notification is deferred and replayed when the load releases the turn."""
        store = get_store()

        async def ping(action, state):
            return {**state, "application": {**state["application"], "replayed": True}}

        store._register_reducer(action_type, ping)

        class Loader(RenderableLayoutView):
            subscribed_actions = {action_type}
            dispatched = False

            async def on_load(self):
                if not self.dispatched:
                    self.dispatched = True
                    await asyncio.gather(self.dispatch(action_type, {}))

            def state_selector(self, state):
                return state["application"].get("replayed")

        Loader.on_state_changed = on_state_changed
        return Loader(interaction=_make_interaction())

    async def test_a_replayed_render_is_not_answered_as_the_click(self):
        """The replay task copied the releasing task's context. When that was a
        click callback, the background render waived the cooldown and could
        answer through the click's response slot."""
        seen = []

        async def on_state_changed(self, state):
            seen.append(_CURRENT_INTERACTION.get())

        view = self._replayed_view("REPLAY_CONTEXT", on_state_changed)
        token = _CURRENT_INTERACTION.set(_make_interaction())
        try:
            assert await asyncio.wait_for(view.load(), timeout=2) is True
        finally:
            _CURRENT_INTERACTION.reset(token)
        await asyncio.wait_for(view.task_manager.wait_tasks(view.id), timeout=2)

        assert seen == [None]

    async def test_a_replayed_render_that_raises_is_logged_with_its_traceback(self, caplog):
        async def on_state_changed(self, state):
            raise KeyError("player_1")

        view = self._replayed_view("REPLAY_RAISES", on_state_changed)
        with caplog.at_level(logging.ERROR, logger="cascadeui"):
            await asyncio.wait_for(view.load(), timeout=2)
            await asyncio.wait_for(view.task_manager.wait_tasks(view.id), timeout=2)

        records = [r for r in caplog.records if "Error notifying subscriber" in r.getMessage()]
        assert len(records) == 1
        assert records[0].exc_info is not None

    async def test_a_deferred_refresh_that_fires_during_a_load_waits_for_it(self):
        """The cooldown's deferred render called on_state_changed directly, so
        it ran while another task's on_load had the tree half-built."""
        rendered_while = []

        class Loader(RenderableLayoutView):
            refresh_cooldown_ms = 50
            loading = False
            slow = False
            k = 0

            def build_ui(self):
                self.clear_items()
                self.add_item(TextDisplay(f"k={self.k}"))

            async def on_load(self):
                if not self.slow:
                    return
                self.loading = True
                self.clear_items()
                await asyncio.sleep(0.2)
                self.add_item(TextDisplay("loaded"))
                self.loading = False

            async def on_state_changed(self, state):
                rendered_while.append("loading" if self.loading else "idle")
                await super().on_state_changed(state)

        view = Loader(interaction=_make_interaction())
        await view.send()
        view.k = 1
        view.build_ui()
        await view.refresh()
        view.k = 2
        view.build_ui()
        assert await view.refresh() is RenderOutcome.DEFERRED

        view.slow = True
        await asyncio.wait_for(view.load(), timeout=2)
        await asyncio.wait_for(view.task_manager.wait_tasks(view.id), timeout=2)

        assert rendered_while == ["idle"]

    @staticmethod
    def _coalescing_view(action_type, on_load=None):
        """A view whose first render parks, so a second notification coalesces
        into it and is re-run when it finishes."""
        store = get_store()

        async def bump(action, state):
            n = state["application"].get("n", 0) + 1
            return {**state, "application": {**state["application"], "n": n}}

        store._register_reducer(action_type, bump)
        first_render = asyncio.Event()
        release_render = asyncio.Event()
        seen = []

        class Coalescing(RenderableLayoutView):
            subscribed_actions = {action_type}
            loading = False

            def state_selector(self, state):
                return state["application"].get("n")

            async def on_state_changed(self, state):
                seen.append("loading" if self.loading else "idle")
                if len(seen) == 1:
                    first_render.set()
                    await release_render.wait()

        if on_load is not None:
            Coalescing.on_load = on_load
        return store, Coalescing, first_render, release_render, seen

    async def test_a_coalesced_render_waits_for_a_reload_that_started_meanwhile(self):
        release_load = asyncio.Event()

        async def on_load(self):
            if self._message is None:
                return
            self.loading = True
            await release_load.wait()
            self.loading = False

        store, Coalescing, first_render, release_render, seen = self._coalescing_view(
            "COALESCED_TURN", on_load
        )
        view = Coalescing(interaction=_make_interaction())
        await view.send()
        await store.dispatch("COALESCED_TURN", {})
        await asyncio.wait_for(first_render.wait(), timeout=2)
        await store.dispatch("COALESCED_TURN", {})
        await asyncio.sleep(0)
        reload = asyncio.create_task(view.reload())
        await asyncio.sleep(0.01)

        release_render.set()
        await asyncio.sleep(0.05)
        release_load.set()
        await asyncio.wait_for(reload, timeout=2)
        await store._flush_notifications()
        await asyncio.wait_for(view.task_manager.wait_tasks(view.id), timeout=2)

        assert seen == ["idle", "idle"]

    async def test_a_coalesced_render_is_dropped_when_the_view_exited_meanwhile(self):
        store, Coalescing, first_render, release_render, seen = self._coalescing_view(
            "COALESCED_EXIT"
        )
        view = Coalescing(interaction=_make_interaction())
        await view.send()
        await store.dispatch("COALESCED_EXIT", {})
        await asyncio.wait_for(first_render.wait(), timeout=2)
        await store.dispatch("COALESCED_EXIT", {})
        await asyncio.sleep(0)
        await view.exit()

        release_render.set()
        await store._flush_notifications()

        assert seen == ["idle"]

    async def test_a_preload_that_raises_rolls_the_send_back(self):
        class Broken(RenderableLayoutView):
            async def on_load(self):
                raise RuntimeError("preload failed")

        view = Broken(interaction=_make_interaction())
        with pytest.raises(RuntimeError, match="preload failed"):
            await view.send()

        assert view.id not in view.state_store.subscribers
        assert view.is_finished()

    async def test_a_v1_send_preload_that_raises_rolls_the_send_back(self):
        from cascadeui import TabView

        async def broken():
            raise RuntimeError("tab builder failed")

        view = TabView(interaction=_make_interaction(), tabs={"A": broken})
        with pytest.raises(RuntimeError, match="tab builder failed"):
            await view.send()

        assert view.id not in view.state_store.subscribers

    async def test_a_rolled_back_destination_cannot_paint_over_the_source(self):
        """The destination's turn is released when its edit fails, and a reload
        queued on it runs at the next suspension. The rollback awaited before
        unbinding the destination's message, so that reload edited the
        recovered source's message with the destination's tree."""
        from cascadeui.setup import setup_middleware

        class AuditLog:
            async def __call__(self, action, state, next_fn):
                await asyncio.sleep(0)
                return await next_fn(action, state)

        await setup_middleware(AuditLog())

        class Destination(RenderableLayoutView):
            loads = 0

            async def on_load(self):
                type(self).loads += 1
                self.clear_items()
                self.add_item(TextDisplay(f"DESTINATION {type(self).loads}"))
                if type(self).loads == 1:
                    # Not owned by the task manager, so only the unbind stops it.
                    self._queued = asyncio.create_task(self.reload())
                    await asyncio.sleep(0)
                    raise RuntimeError("destination preload failed")

        source = RenderableLayoutView(interaction=_make_interaction())
        await source.send()
        message = source._message
        message.edit.reset_mock()

        with pytest.raises(RuntimeError, match="destination preload failed"):
            await asyncio.wait_for(source.push(Destination, _make_interaction()), timeout=2)
        for _ in range(20):
            await asyncio.sleep(0)

        shipped = [str(c.kwargs["view"].to_components()) for c in message.edit.await_args_list]
        assert not any("DESTINATION" in s for s in shipped)
        assert not source.is_finished()

    async def test_a_rolled_back_destination_stops_its_own_background_work(self):
        """A task the destination's on_load started under its own id (an avatar
        backfill) would otherwise run on_load again on a torn-down view."""

        class Destination(RenderableLayoutView):
            loads = 0

            async def on_load(self):
                type(self).loads += 1
                if type(self).loads == 1:
                    self.task_manager.create_task(self.id, self.reload())
                    await asyncio.sleep(0)
                    raise RuntimeError("destination preload failed")

        source = RenderableLayoutView(interaction=_make_interaction())
        await source.send()
        with pytest.raises(RuntimeError, match="destination preload failed"):
            await asyncio.wait_for(source.push(Destination, _make_interaction()), timeout=2)
        for _ in range(20):
            await asyncio.sleep(0)

        assert Destination.loads == 1

    async def test_a_refresh_during_delivery_is_replayed_once_the_message_exists(self):
        """A refresh another task makes while the request is in flight finds no
        message yet; it reports DEFERRED, and the turn's release sends it as
        the refresh it was: the tree as it then stands, with no state render
        nobody asked for."""
        interaction = _make_interaction()
        in_flight, release, _ = self._park_send(interaction)
        view = RenderableLayoutView(interaction=interaction)
        view.on_state_changed = AsyncMock()

        send = asyncio.create_task(view.send())
        await asyncio.wait_for(in_flight.wait(), timeout=2)
        # Changed after the post serialized its tree.
        view.children[0].content = "changed in flight"
        assert await view.refresh() is RenderOutcome.DEFERRED

        release.set()
        await asyncio.wait_for(send, timeout=2)
        await asyncio.wait_for(view.task_manager.wait_tasks(view.id), timeout=2)
        view.on_state_changed.assert_not_awaited()
        view.message.edit.assert_awaited_once()
        shipped = view.message.edit.await_args.kwargs["view"].to_components()
        assert self._texts(shipped) == ["changed in flight"]

    async def test_a_long_wait_for_the_turn_is_logged(self, monkeypatch, caplog):
        """The cycle walk cannot see a holder that awaits a task instead of a
        lock, so a wait that never ends has to at least say so."""
        from cascadeui.views import base as base_module

        monkeypatch.setattr(base_module, "_RELOAD_WAIT_WARN_SECONDS", 0.05)
        gate = asyncio.Event()

        class Loader(RenderableLayoutView):
            async def on_load(self):
                await gate.wait()

        view = Loader(interaction=_make_interaction())
        with caplog.at_level(logging.WARNING):
            holder = asyncio.create_task(view.load())
            await asyncio.sleep(0)
            waiter = asyncio.create_task(view.load())
            await asyncio.sleep(0.15)
            gate.set()
            await asyncio.wait_for(asyncio.gather(holder, waiter), timeout=2)

        assert "load() on Loader has waited" in caplog.text
        assert "for load() to release" in caplog.text

    async def test_a_refresh_baseline_digests_the_tree_its_edit_serializes(self, monkeypatch):
        """Before 3.12 the bound runs its coroutine as a task whose first step
        runs on a later loop iteration, so work queued ahead of it could
        rebuild the tree between a digest taken outside the wrap and the
        edit's serialization."""
        schedule_bounded_waits_as_before_312(monkeypatch)
        view = RenderableLayoutView(interaction=_make_interaction())
        view.clear_items()
        view.add_item(TextDisplay("first"))
        serialized = []
        message = MagicMock()
        message.id = 999

        async def edit(**kwargs):
            serialized.append(str(kwargs["view"].to_components()))

        message.edit = AsyncMock(side_effect=edit)
        view._message = message
        view._webhook_message = None

        def rebuild():
            view.clear_items()
            view.add_item(TextDisplay("second"))

        asyncio.get_running_loop().call_soon(rebuild)
        await view.refresh()

        assert "second" in serialized[0]
        assert view._last_tree_digest == view._compute_tree_digest()

    async def test_a_refresh_bound_before_a_concurrent_404_does_not_raise(self, monkeypatch):
        """Reading the message inside the wrapped coroutine, a loop iteration
        later before 3.12, found it already cleared by a refresh that had
        seen the message deleted, and raised AttributeError."""
        schedule_bounded_waits_as_before_312(monkeypatch)
        view = RenderableLayoutView(interaction=_make_interaction())
        reached = asyncio.Event()

        async def edit(**kwargs):
            reached.set()  # the request reached Discord; the message is gone
            raise discord.NotFound(MagicMock(status=404), "gone")

        message = MagicMock()
        message.id = 999
        message.edit = AsyncMock(side_effect=edit)
        view._message = message
        view._webhook_message = None

        first = asyncio.create_task(view.refresh(content="a"))
        await reached.wait()
        second = asyncio.create_task(view.refresh(content="b"))
        outcomes = await asyncio.gather(first, second)

        assert outcomes == [RenderOutcome.NO_MESSAGE, RenderOutcome.NO_MESSAGE]

    async def test_a_webhook_edit_bound_before_a_concurrent_expiry_does_not_raise(
        self, monkeypatch
    ):
        """The webhook path has the same shape: an expired token clears the
        handle, and a concurrent embed edit read it a loop iteration later."""
        schedule_bounded_waits_as_before_312(monkeypatch)
        view = RenderableLayoutView(interaction=_make_interaction())
        message = MagicMock()
        message.id = 999
        message.edit = AsyncMock()
        view._message = message
        reached = asyncio.Event()

        async def webhook_edit(**kwargs):
            reached.set()  # the request reached Discord; the token has expired
            raise discord.HTTPException(MagicMock(status=401), "token expired")

        view._webhook_message = MagicMock()
        view._webhook_message.edit = AsyncMock(side_effect=webhook_edit)

        first = asyncio.create_task(view.refresh(embed=discord.Embed(title="a")))
        await reached.wait()
        second = asyncio.create_task(view.refresh(embed=discord.Embed(title="b")))
        outcomes = await asyncio.gather(first, second)

        assert outcomes == [RenderOutcome.RENDERED, RenderOutcome.RENDERED]

    async def test_a_rebuild_hook_that_reloads_the_destination_raises(self):
        """The navigation already runs the destination's on_load and render
        under its turn, so a rebuild= that reloads it re-enters that turn."""
        source = RenderableLayoutView(interaction=_make_interaction())
        await source.send()

        with pytest.raises(
            RuntimeError,
            match=r"reload\(\) called while push\(\) or pop\(\) holds.*Fix: drop the reload\(\)",
        ):
            await asyncio.wait_for(
                source.push(
                    RenderableLayoutView, _make_interaction(), rebuild=lambda v: v.reload()
                ),
                timeout=2,
            )
        assert not source.is_finished()

    async def test_a_rebuild_hook_that_loads_the_destination_raises(self):
        source = RenderableLayoutView(interaction=_make_interaction())
        await source.send()

        with pytest.raises(RuntimeError, match=r"Fix: drop the load\(\)"):
            await asyncio.wait_for(
                source.push(RenderableLayoutView, _make_interaction(), rebuild=lambda v: v.load()),
                timeout=2,
            )
        assert not source.is_finished()


class TestReloadDisposition:
    """``reload()`` and ``refresh()`` name what they did with the edit.

    A caller that stamps something on the strength of a reload (a one-shot
    notice panel marking itself delivered) could not previously tell a shipped
    edit from a reload the throttle handed to a scheduled task: both returned
    ``None``, ``refresh_degraded`` read ``False`` in both cases, and the
    distinguishing state (``_reload_pending``) was private.
    """

    def _sent_view(self, cls=RenderableLayoutView, **kwargs):
        view = cls(interaction=_make_interaction(), **kwargs)
        message = MagicMock()
        message.id = 999
        message.edit = AsyncMock()
        view._message = message
        view._webhook_message = None
        return view

    async def _cancel_deferred(self, view):
        task = view._deferred_refresh_task
        if task is not None:
            await asyncio.sleep(0)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def test_deferred_at_the_reload_gate(self):
        loads = []

        class Loader(RenderableLayoutView):
            async def on_load(self):
                loads.append(1)

        view = self._sent_view(Loader)
        view._cooldown_not_before = time.monotonic() + 30

        outcome = await view.reload()

        # The gate decided, not the render: no fetch ran, the reload is
        # pending on the single deferred task, and the caller is told so.
        assert outcome is RenderOutcome.DEFERRED
        assert loads == []
        assert view._reload_pending is True
        await self._cancel_deferred(view)

    async def test_rendered_then_skipped_as_unchanged(self):
        view = self._sent_view()

        first = await view.reload()
        second = await view.reload()

        assert first is RenderOutcome.RENDERED
        assert second is RenderOutcome.SKIPPED
        # The digest short-circuit decided the second call: exactly one edit
        # shipped, and nothing was queued for later.
        assert view._message.edit.await_count == 1
        assert view._reload_pending is False

    async def test_dropped_on_transport_failure(self):
        view = self._sent_view()
        view._message.edit = AsyncMock(side_effect=aiohttp.ClientOSError(104, "reset"))

        outcome = await view.reload()

        assert outcome is RenderOutcome.DROPPED
        assert view.refresh_degraded is True

    async def test_no_message_before_send(self):
        loads = []

        class Loader(RenderableLayoutView):
            async def on_load(self):
                loads.append(1)

        view = Loader(interaction=_make_interaction())

        outcome = await view.reload()

        # The fetch still runs on an unsent view (unchanged behavior); the
        # outcome says no editable message exists rather than claiming a render.
        assert loads == [1]
        assert outcome is RenderOutcome.NO_MESSAGE

    async def test_armed_reload_relays_the_frozen_refresh_outcome(self):
        loads = []

        class Loader(RenderableLayoutView):
            async def on_load(self):
                loads.append(1)

        view = self._sent_view(Loader)
        view._refresh_armed = True

        outcome = await view.reload()

        # on_load is skipped while armed (the tree is the refresh button);
        # the disposition describes the frozen-tree edit that shipped.
        assert loads == []
        assert outcome is RenderOutcome.RENDERED
        assert view._message.edit.await_count == 1

    async def test_refresh_reports_deferred_on_a_rate_limit(self):
        view = self._sent_view()
        view._message.edit = AsyncMock(side_effect=_FakeRateLimit(30.0))

        outcome = await view.refresh()

        # The 429 armed the backoff window and queued the retry; the caller
        # is told the edit is deferred, not that it landed.
        assert outcome is RenderOutcome.DEFERRED
        assert view._ratelimit_not_before > time.monotonic()
        await self._cancel_deferred(view)

    async def test_coalesced_boolean_kwargs_or_across_calls(self):
        """A ``force=True`` gated into the window survives an unforced reload
        landing in the same window, in either order; wholesale replacement of
        the pending kwargs silently dropped it. Non-boolean keywords take the
        newest call's value."""

        async def gated_pair(first_kwargs, second_kwargs):
            view = self._sent_view()
            view._cooldown_not_before = time.monotonic() + 30
            assert await view.reload(**first_kwargs) is RenderOutcome.DEFERRED
            assert await view.reload(**second_kwargs) is RenderOutcome.DEFERRED
            pending = dict(view._pending_reload_kwargs)
            await self._cancel_deferred(view)
            return pending

        assert await gated_pair({"force": True}, {}) == {"force": True}
        assert await gated_pair({"force": False}, {"force": True}) == {"force": True}
        assert await gated_pair({"cursor": 3}, {"cursor": 0}) == {"cursor": 0}

    async def test_a_reload_during_a_deferred_render_is_not_latched(self):
        """A reload arriving while the deferred task's render is under way
        waits for that render, then meets the cooldown and must still leave a
        task to serve it once the deferred task has gone, rather than a
        pending reload latched with nothing left to run it."""
        park = asyncio.Event()

        class ParkingRender(RenderableLayoutView):
            async def on_state_changed(self, state):
                await park.wait()

        view = self._sent_view(ParkingRender)
        deferred = asyncio.create_task(view._deferred_refresh(0))
        view._deferred_refresh_task = deferred
        await asyncio.sleep(0)  # wakes, window inactive, parks in the render
        await asyncio.sleep(0)

        view._cooldown_not_before = time.monotonic() + 30
        reload = asyncio.create_task(view.reload(force=True))
        await asyncio.sleep(0)
        assert not reload.done()  # waits for the render in progress

        park.set()
        await deferred
        assert await asyncio.wait_for(reload, timeout=2) is RenderOutcome.DEFERRED

        assert view._reload_pending is True
        successor = view._deferred_refresh_task
        assert successor is not None
        assert successor is not deferred
        await self._cancel_deferred(view)

    async def test_stale_pending_reload_does_not_respawn_while_armed(self):
        """The successor requeue skips armed views. An armed reload is a plain
        refresh of the frozen tree and never clears ``_reload_pending``, so
        requeueing on the flag would respawn a successor after every dispatch
        until the token cliff."""
        view = self._sent_view()
        view._refresh_armed = True
        view._reload_pending = True  # latched before the view armed

        await view._deferred_refresh(0)

        # The armed dispatch ran (frozen-tree edit shipped) and the task
        # exited without spawning a successor for the moot reload.
        assert view._message.edit.await_count == 1
        assert view._deferred_refresh_task is None


class TestOnLoadDurationWarning:
    """A slow on_load (over auto_defer_delay) logs a one-per-class warning so a
    render-path preload that competes with interaction acks becomes visible. The
    no-op default is skipped entirely (zero overhead).
    """

    async def test_slow_on_load_warns_once(self, caplog):
        from cascadeui.views import base as base_mod

        base_mod._slow_on_load_warned.clear()

        class SlowView(StatefulLayoutView):
            auto_defer_delay = 0.01  # tight budget so a small real delay overruns

            async def on_load(self):
                await asyncio.sleep(0.05)  # reliably over 0.01s

        view = SlowView(interaction=_make_interaction())
        with caplog.at_level(logging.WARNING, logger="cascadeui.views.base"):
            await view._run_on_load()

        warns = [r for r in caplog.records if "on_load() took" in r.message]
        assert len(warns) == 1
        assert "SlowView" in warns[0].message

    async def test_fast_on_load_no_warning(self, caplog):
        from cascadeui.views import base as base_mod

        base_mod._slow_on_load_warned.clear()

        class QuickView(StatefulLayoutView):
            async def on_load(self):  # near-instant, under the 2.5s default budget
                pass

        view = QuickView(interaction=_make_interaction())
        with caplog.at_level(logging.WARNING, logger="cascadeui.views.base"):
            await view._run_on_load()

        assert not [r for r in caplog.records if "on_load() took" in r.message]

    async def test_dedup_per_class(self, caplog):
        from cascadeui.views import base as base_mod

        base_mod._slow_on_load_warned.clear()

        class SlowView(StatefulLayoutView):
            auto_defer_delay = 0.01

            async def on_load(self):
                await asyncio.sleep(0.05)

        with caplog.at_level(logging.WARNING, logger="cascadeui.views.base"):
            await SlowView(interaction=_make_interaction())._run_on_load()
            await SlowView(interaction=_make_interaction())._run_on_load()

        warns = [r for r in caplog.records if "on_load() took" in r.message]
        assert len(warns) == 1  # second slow run, same class -> no second warning

    async def test_default_noop_no_warning(self, caplog):
        # The default no-op on_load is skipped entirely -- no timing, no warning,
        # even with a tight budget that a timed no-op might trip.
        from cascadeui.views import base as base_mod

        base_mod._slow_on_load_warned.clear()

        class DefaultView(StatefulLayoutView):
            auto_defer_delay = 0.0001  # default on_load -- never timed, so never warns

        view = DefaultView(interaction=_make_interaction())
        with caplog.at_level(logging.WARNING, logger="cascadeui.views.base"):
            await view._run_on_load()

        assert not [r for r in caplog.records if "on_load() took" in r.message]


class TestOnPreSendHook:
    """on_pre_send is the pre-send veto gate: it runs first in the send
    pipeline and aborts the send cleanly when it returns False.
    """

    async def test_default_hook_allows_send(self):
        # The default returns True, so a view without an override sends normally.
        interaction = _make_interaction()
        view = RenderableLayoutView(interaction=interaction)

        message = await view.send()
        assert message is not None

    async def test_veto_aborts_send(self):
        # Returning False aborts: no message ships, send() returns None.
        interaction = _make_interaction()

        class GatedView(StatefulLayoutView):
            async def on_pre_send(self, interaction):
                return False

        view = GatedView(interaction=interaction)
        message = await view.send()

        assert message is None
        interaction.response.send_message.assert_not_called()

    async def test_falsy_non_false_return_also_vetoes(self):
        # The gate is `if not await on_pre_send(...)`, so any falsy value
        # (None, 0) vetoes, not only an explicit False.
        interaction = _make_interaction()

        class GatedView(StatefulLayoutView):
            async def on_pre_send(self, interaction):
                return None

        view = GatedView(interaction=interaction)
        message = await view.send()

        assert message is None
        interaction.response.send_message.assert_not_called()

    async def test_veto_runs_before_on_load(self):
        # The gate runs first, so a veto skips the on_load preload entirely.
        interaction = _make_interaction()
        order = []

        class GatedView(StatefulLayoutView):
            async def on_pre_send(self, interaction):
                order.append("pre_send")
                return False

            async def on_load(self):
                order.append("on_load")

        view = GatedView(interaction=interaction)
        await view.send()

        assert order == ["pre_send"]  # on_load never ran

    async def test_hook_receives_triggering_interaction(self):
        interaction = _make_interaction()
        seen = {}

        class GatedView(RenderableLayoutView):
            async def on_pre_send(self, interaction):
                seen["interaction"] = interaction
                return True

        view = GatedView(interaction=interaction)
        await view.send()

        assert seen["interaction"] is interaction

    async def test_veto_leaves_no_state(self):
        # A vetoed send leaves zero side effects: the view stops, its
        # subscriber is removed, and it never registers in either registry.
        interaction = _make_interaction()
        store = get_store()

        class GatedView(StatefulLayoutView):
            async def on_pre_send(self, interaction):
                return False

        view = GatedView(interaction=interaction)
        await view.send()

        assert view.is_finished()
        assert view.id not in store.subscribers
        assert view.id not in store.state["views"]
        assert view.id not in store._active_views

    async def test_a_veto_hook_that_raises_leaves_no_state(self):
        # A raise went past the cleanup a False gets, so the view stayed
        # subscribed and unstopped with no message: a subscriber nothing shows.
        store = get_store()

        class GatedView(RenderableLayoutView):
            async def on_pre_send(self, interaction):
                raise RuntimeError("permission lookup failed")

        view = GatedView(interaction=_make_interaction())
        with pytest.raises(RuntimeError, match="permission lookup failed"):
            await view.send()

        assert view.is_finished()
        assert view.id not in store.subscribers

    async def test_override_can_respond_through_open_slot(self):
        # The response slot is still open inside on_pre_send, so an override
        # can tell the user why the send was vetoed.
        interaction = _make_interaction()

        class GatedView(StatefulLayoutView):
            async def on_pre_send(self, interaction):
                await self.respond(interaction, "Not allowed", ephemeral=True)
                return False

        view = GatedView(interaction=interaction)
        message = await view.send()

        assert message is None
        interaction.response.send_message.assert_called_once()


class TestOnTimeoutLogLevel:
    """on_timeout downgrades the expected ephemeral token-expiry edit
    failure to DEBUG; a non-ephemeral edit failure stays WARNING. The
    branch is gated on ``_ephemeral``, so any edit exception exercises it.

    The view carries a freezable button so on_timeout ships the disable edit;
    a component-less display view skips the edit entirely (nothing to freeze).
    """

    async def test_ephemeral_edit_failure_logs_debug(self, caplog):
        interaction = _make_interaction()
        view = RenderableLayoutView(interaction=interaction)
        view.add_item(ActionRow(StatefulButton(label="Fire")))
        await view.send()
        view._ephemeral = True
        view._message = MagicMock()
        view._message.edit = AsyncMock(side_effect=RuntimeError("Invalid Webhook Token"))

        with caplog.at_level(logging.DEBUG, logger="cascadeui.views.base"):
            await view.on_timeout()

        debug_records = [
            r for r in caplog.records if "Skipped disabling components" in r.getMessage()
        ]
        assert debug_records
        assert all(r.levelno == logging.DEBUG for r in debug_records)
        # The old WARNING line must not fire for the ephemeral case.
        assert not [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING and "disable components" in r.getMessage()
        ]

    async def test_non_ephemeral_edit_failure_logs_warning(self, caplog):
        interaction = _make_interaction()
        view = RenderableLayoutView(interaction=interaction)
        view.add_item(ActionRow(StatefulButton(label="Fire")))
        await view.send()
        view._ephemeral = False
        view._message = MagicMock()
        view._message.edit = AsyncMock(side_effect=RuntimeError("boom"))

        with caplog.at_level(logging.WARNING, logger="cascadeui.views.base"):
            await view.on_timeout()

        warning_records = [
            r for r in caplog.records if "Could not disable components on timeout" in r.getMessage()
        ]
        assert warning_records
        assert all(r.levelno == logging.WARNING for r in warning_records)


class TestFreezeSkipsNoOpEdit:
    """A component-less display view tears down without a cosmetic PATCH.

    _freeze_components returns the count of newly-disabled items; on_timeout
    and exit(delete_message=False) skip the message edit when it returns 0,
    so a static card (text and images, no interactive components) posts and
    self-cleans without ever shipping a no-op edit that re-sends an identical
    tree.
    """

    def test_freeze_returns_count_of_newly_disabled(self):
        view = RenderableLayoutView(interaction=_make_interaction())
        view.add_item(ActionRow(StatefulButton(label="Fire")))
        assert view._freeze_components() == 1  # the one button
        assert view._freeze_components() == 0  # already disabled, nothing new

    def test_display_only_freeze_returns_zero(self):
        # RenderableLayoutView holds a single TextDisplay -- no disabled attr.
        view = RenderableLayoutView(interaction=_make_interaction())
        assert view._freeze_components() == 0

    async def test_on_timeout_skips_edit_for_display_view(self):
        view = RenderableLayoutView(interaction=_make_interaction())
        await view.send()
        view._message = MagicMock()
        view._message.edit = AsyncMock()

        await view.on_timeout()

        view._message.edit.assert_not_called()  # nothing to freeze -> no PATCH

    async def test_on_timeout_edits_when_a_component_freezes(self):
        view = RenderableLayoutView(interaction=_make_interaction())
        view.add_item(ActionRow(StatefulButton(label="Fire")))
        await view.send()
        view._message = MagicMock()
        view._message.edit = AsyncMock()

        await view.on_timeout()

        view._message.edit.assert_awaited_once()  # a button froze -> one PATCH

    async def test_exit_keep_message_skips_edit_for_display_view(self):
        view = RenderableLayoutView(interaction=_make_interaction())
        await view.send()
        view._message = MagicMock()
        view._message.edit = AsyncMock()
        view._message.delete = AsyncMock()

        await view.exit(delete_message=False)

        view._message.edit.assert_not_called()
        view._message.delete.assert_not_called()  # message left intact


class TestStatefulLayoutViewInteraction:
    """Interaction check and owner_only tests."""

    async def test_owner_only_rejects_other_user(self):
        interaction = _make_interaction(user_id=100)
        view = StatefulLayoutView(interaction=interaction)

        other_interaction = _make_interaction(user_id=999)
        result = await view.interaction_check(other_interaction)

        assert result is False

    async def test_owner_only_allows_owner(self):
        interaction = _make_interaction(user_id=100)
        view = StatefulLayoutView(interaction=interaction)

        same_interaction = _make_interaction(user_id=100)
        result = await view.interaction_check(same_interaction)

        assert result is True

    async def test_owner_only_disabled(self):
        interaction = _make_interaction(user_id=100)
        view = StatefulLayoutView(interaction=interaction)
        view.owner_only = False

        other_interaction = _make_interaction(user_id=999)
        result = await view.interaction_check(other_interaction)

        assert result is True


class TestStatefulLayoutViewCleanup:
    """Exit and cleanup tests."""

    async def test_exit_unregisters_view(self):
        interaction = _make_interaction()
        view = StatefulLayoutView(interaction=interaction)
        store = get_store()
        store._register_view(view)

        assert view.id in store._active_views

        await view.exit()

        assert view.id not in store._active_views

    async def test_exit_unsubscribes(self):
        interaction = _make_interaction()
        view = StatefulLayoutView(interaction=interaction)
        store = get_store()

        assert view.id in store.subscribers

        await view.exit()

        assert view.id not in store.subscribers


class TestStableCustomIds:
    """``_stabilize_custom_ids`` rewrites auto-generated ids post-build.

    discord.py assigns a random hex custom_id to any Button/Select without
    an explicit ``custom_id=``. Every ``build_ui()`` rebuild produces
    fresh UUIDs, which causes the ViewStore dispatch table to churn and
    creates a race window where pending user clicks reference evicted
    entries. Stabilization rewrites auto-generated ids to deterministic
    anchors so repeat rebuilds produce identical dispatch keys.
    """

    def _make_view_with_build(self, build_fn):
        class _V(StatefulLayoutView):
            def build_ui(self):
                build_fn(self)

        return _V(interaction=_make_interaction())

    def test_callback_without_a_qualname_does_not_crash_the_build(self):
        """The anchor reads the callback's name, and not every callable has one.

        A ``functools.partial`` carries no ``__qualname__``, and stabilization
        runs inside every ``build_ui``, so reading it unguarded took down the
        whole render rather than the one id it could not name.
        """
        import functools

        async def handler(item_id, interaction):
            pass

        def build(view):
            view.clear_items()
            view.add_item(
                ActionRow(StatefulButton(label="Go", callback=functools.partial(handler, 7)))
            )

        view = self._make_view_with_build(build)
        view.build_ui()

        button = view.children[0].children[0]
        assert button.custom_id  # anchored, not raised

    def test_explicit_custom_id_preserved(self):
        def build(view):
            view.clear_items()
            view.add_item(
                ActionRow(
                    StatefulButton(label="Fire", custom_id="user_chosen_id"),
                )
            )

        view = self._make_view_with_build(build)
        view.build_ui()

        btn = next(view.walk_children())
        # ActionRow first, button second
        button = [c for c in view.walk_children() if isinstance(c, StatefulButton)][0]
        assert button.custom_id == "user_chosen_id"

    def test_link_and_premium_buttons_not_stabilized(self):
        # Link (url) and premium (sku_id) buttons forbid a custom_id; the
        # stabilizer must skip both, or Discord 400s (code 50035, "custom id
        # and url cannot both be specified").
        import discord

        def build(view):
            view.clear_items()
            view.add_item(ActionRow(discord.ui.Button(label="Docs", url="https://example.com")))
            view.add_item(
                ActionRow(discord.ui.Button(style=discord.ButtonStyle.premium, sku_id=123456789))
            )
            view.add_item(ActionRow(StatefulButton(label="Fire")))

        view = self._make_view_with_build(build)
        view.build_ui()

        buttons = [c for c in view.walk_children() if isinstance(c, discord.ui.Button)]
        link = next(b for b in buttons if b.url)
        premium = next(b for b in buttons if getattr(b, "sku_id", None))
        interactive = next(b for b in buttons if not b.url and not getattr(b, "sku_id", None))
        assert link.custom_id is None
        assert premium.custom_id is None
        assert interactive.custom_id is not None

    def test_auto_generated_id_rewritten_with_content_anchor(self):
        def build(view):
            view.clear_items()
            view.add_item(
                ActionRow(
                    StatefulButton(label="Fire"),
                    StatefulButton(label="Close"),
                )
            )

        view = self._make_view_with_build(build)
        view.build_ui()

        buttons = [c for c in view.walk_children() if isinstance(c, StatefulButton)]
        # Content-unique -> content-only id, anchored to view prefix.
        prefix = view.id[:8]
        assert buttons[0].custom_id.startswith(f"{prefix}:")
        assert "Fire" in buttons[0].custom_id
        assert "Close" in buttons[1].custom_id
        # Different buttons get different ids.
        assert buttons[0].custom_id != buttons[1].custom_id

    def test_repeat_build_produces_identical_ids(self):
        """The core promise: rebuild must not churn the dispatch table."""

        def build(view):
            view.clear_items()
            view.add_item(
                ActionRow(
                    StatefulButton(label="Fire"),
                    StatefulButton(label="Close"),
                )
            )

        view = self._make_view_with_build(build)
        view.build_ui()
        first = [c.custom_id for c in view.walk_children() if isinstance(c, StatefulButton)]
        view.build_ui()
        second = [c.custom_id for c in view.walk_children() if isinstance(c, StatefulButton)]
        assert first == second

    def test_colliding_content_uses_position_anchor(self):
        """A 3x3 grid of identical-callback cells must disambiguate by position.

        Mirrors the TicTacToe grid pattern: many buttons share one callback
        family and an identical (empty) label. Each cell's id must be
        anchored to its tree coordinates so a label change on one cell
        does not shift the ids of the others.
        """

        def build(view):
            view.clear_items()
            for _ in range(3):
                view.add_item(
                    ActionRow(
                        StatefulButton(label=""),
                        StatefulButton(label=""),
                        StatefulButton(label=""),
                    )
                )

        view = self._make_view_with_build(build)
        view.build_ui()
        ids = [c.custom_id for c in view.walk_children() if isinstance(c, StatefulButton)]
        # All nine cells must have distinct ids despite identical content.
        assert len(set(ids)) == 9

    def test_label_change_does_not_shift_neighbor_ids(self):
        """The regression the hybrid algorithm exists to prevent.

        Turn 1: nine empty cells -> collide on content -> position-anchored.
        Turn 2: one cell gets "X" -> its content key becomes unique.
        The remaining eight empty cells must keep their turn 1 ids.
        """
        marks = [""] * 9

        def build(view):
            view.clear_items()
            for row in range(3):
                view.add_item(
                    ActionRow(*(StatefulButton(label=marks[row * 3 + c]) for c in range(3)))
                )

        view = self._make_view_with_build(build)
        view.build_ui()
        turn1 = {
            i: c.custom_id
            for i, c in enumerate(b for b in view.walk_children() if isinstance(b, StatefulButton))
        }

        marks[4] = "X"  # center cell played
        view.build_ui()
        turn2 = {
            i: c.custom_id
            for i, c in enumerate(b for b in view.walk_children() if isinstance(b, StatefulButton))
        }

        # The played cell's id is allowed to change (it is now disabled
        # and no click will ever route to it again).
        # Every other cell's id must be identical to turn 1.
        for i in range(9):
            if i == 4:
                continue
            assert turn1[i] == turn2[i], f"cell {i} id shifted: {turn1[i]} -> {turn2[i]}"

    def test_a_button_added_after_the_tree_was_stabilized_gets_an_id_of_its_own(self):
        """A second pass counts only the items it rewrites.

        A button added outside ``build_ui`` (in ``on_load``, say) after an
        identical one was already stabilized took the same content-only id,
        and the send refused the view naming a custom_id the caller never set.
        """

        async def go(interaction):
            pass

        def build(view):
            view.clear_items()
            view.add_item(ActionRow(StatefulButton(label="Go", callback=go)))

        view = self._make_view_with_build(build)
        view.build_ui()
        view.add_item(ActionRow(StatefulButton(label="Go", callback=go)))
        view._stabilize_custom_ids()

        ids = [c.custom_id for c in view.walk_children() if isinstance(c, StatefulButton)]
        assert len(set(ids)) == 2
        view._check_placement()


async def _pick(interaction, value):
    pass


def _registered(view, message_id=1):
    from discord.ui.view import ViewStore

    store = ViewStore(MagicMock())
    store.add_view(view, message_id)
    return store


def _routed(store, custom_id, kind=discord.ComponentType.button, message_id=1):
    """The item a click carrying ``custom_id`` reaches, as ``dispatch_view`` looks it up."""
    return store._views.get(message_id, {}).get((kind.value, custom_id))


def _buttons(view):
    return [c for c in view.walk_children() if isinstance(c, Button)]


class TestStaleClicksReachOnlyAnEquivalentButton:
    """A click sent from a render that has since changed reaches a button
    only when that button would do the same thing.

    discord.py resolves a click by custom_id alone, against the newest
    render registered for the message. An id that named a position or a
    label sent a click on one row to whichever row took its place, so a
    double-click on "Delete" removed the next row too.
    """

    def _rows(self, names):
        class _V(StatefulLayoutView):
            def build_ui(self):
                self.clear_items()
                for name in self.names:

                    async def remove(interaction, name=name):
                        pass

                    button = StatefulButton(label="Delete", callback=remove)
                    button._target = name
                    self.add_item(Section(TextDisplay(f"Task: {name}"), accessory=button))

        view = _V(interaction=_make_interaction())
        view.names = list(names)
        view.build_ui()
        return view

    def _rerender(self, view, store, names):
        view.names = list(names)
        view.build_ui()
        store.add_view(view, 1)

    async def test_a_removed_row_is_dropped_and_the_rows_after_it_still_route(self):
        view = self._rows(["a", "b", "c"])
        store = _registered(view)
        before = {b._target: b.custom_id for b in _buttons(view)}

        self._rerender(view, store, ["b", "c"])

        assert _routed(store, before["a"]) is None
        assert _routed(store, before["b"])._target == "b"
        assert _routed(store, before["c"])._target == "c"

    async def test_a_turned_page_drops_every_click_from_the_old_page(self):
        view = self._rows(["a", "b", "c"])
        store = _registered(view)
        before = [b.custom_id for b in _buttons(view)]

        self._rerender(view, store, ["d", "e", "f"])

        assert [_routed(store, cid) for cid in before] == [None, None, None]

    async def test_equal_values_of_different_types_are_different_targets(self):
        view = self._rows([1])
        store = _registered(view)
        before = _buttons(view)[0].custom_id

        self._rerender(view, store, [True])

        assert _routed(store, before) is None

    async def test_a_button_that_acts_on_the_view_keeps_its_id_when_the_screen_changes(self):
        class _V(StatefulLayoutView):
            count = 0

            async def _bump(self, interaction):
                pass

            def build_ui(self):
                self.clear_items()
                self.add_item(TextDisplay(f"Count: {self.count}"))
                self.add_item(ActionRow(StatefulButton(label="+1", callback=self._bump)))

        view = _V(interaction=_make_interaction())
        view.build_ui()
        store = _registered(view)
        before = _buttons(view)[0].custom_id

        view.count = 1
        view.build_ui()
        store.add_view(view, 1)

        assert _buttons(view)[0].custom_id == before
        assert _routed(store, before) is _buttons(view)[0]

    async def test_captured_values_without_a_hash_compare_by_value(self):
        class _V(StatefulLayoutView):
            def build_ui(self):
                self.clear_items()
                row = dict(self.row)

                async def open_row(interaction):
                    return row

                self.add_item(ActionRow(StatefulButton(label="Open", callback=open_row)))

        view = _V(interaction=_make_interaction())
        view.row = {"id": 7, "tags": ["x"]}
        view.build_ui()
        first = _buttons(view)[0].custom_id

        view.build_ui()
        assert _buttons(view)[0].custom_id == first

        view.row = {"id": 8, "tags": ["x"]}
        view.build_ui()
        assert _buttons(view)[0].custom_id != first

    async def test_a_capture_whose_equality_cannot_answer_does_not_break_the_render(self):
        class _Ambiguous:
            def __bool__(self):
                raise ValueError("truth value is ambiguous")

        class _ArrayLike:
            __hash__ = None

            def __eq__(self, other):
                return _Ambiguous()

        class _V(StatefulLayoutView):
            def build_ui(self):
                self.clear_items()
                data = self.data

                async def plot(interaction):
                    return data

                self.add_item(ActionRow(StatefulButton(label="Plot", callback=plot)))

        view = _V(interaction=_make_interaction())
        view.data = _ArrayLike()
        view.build_ui()
        first = _buttons(view)[0].custom_id

        # The same object is the same target, whatever its equality says.
        view.build_ui()
        assert _buttons(view)[0].custom_id == first

        view.data = _ArrayLike()
        view.build_ui()
        assert _buttons(view)[0].custom_id != first

    async def test_a_partial_keeps_its_id_while_it_binds_the_same_arguments(self):
        import functools

        async def open_task(task_id, interaction):
            pass

        class _V(StatefulLayoutView):
            def build_ui(self):
                self.clear_items()
                callback = functools.partial(open_task, self.task_id)
                self.add_item(ActionRow(StatefulButton(label="Open", callback=callback)))

        view = _V(interaction=_make_interaction())
        view.task_id = 7
        view.build_ui()
        first = _buttons(view)[0].custom_id

        view.build_ui()
        assert _buttons(view)[0].custom_id == first

        view.task_id = 8
        view.build_ui()
        assert _buttons(view)[0].custom_id != first

    async def test_a_keyword_only_default_names_the_target(self):
        view = self._rows([])

        def button(name):
            async def remove(interaction, *, name=name):
                pass

            return StatefulButton(label="Delete", callback=remove)

        assert view._custom_id_token(button("a")) != view._custom_id_token(button("b"))
        assert view._custom_id_token(button("a")) == view._custom_id_token(button("a"))

    async def test_a_callback_that_refers_to_itself_still_renders(self):
        class _V(StatefulLayoutView):
            def build_ui(self):
                self.clear_items()

                async def retry(interaction):
                    await retry(interaction)

                self.add_item(ActionRow(StatefulButton(label="Retry", callback=retry)))

        view = _V(interaction=_make_interaction())
        view.build_ui()
        first = _buttons(view)[0].custom_id
        view.build_ui()

        assert _buttons(view)[0].custom_id == first

    async def test_a_closure_over_an_unassigned_name_still_renders(self):
        class _V(StatefulLayoutView):
            def build_ui(self):
                self.clear_items()

                async def report(interaction):
                    return detail

                self.add_item(ActionRow(StatefulButton(label="Report", callback=report)))
                if self.verbose:
                    detail = "full"

        view = _V(interaction=_make_interaction())
        view.verbose = False
        view.build_ui()

        assert _buttons(view)[0].custom_id

    async def test_a_button_subclass_is_told_apart_by_its_own_attributes(self):
        class _Row(discord.ui.Button):
            def __init__(self, task_id):
                super().__init__(label="Delete")
                self.task_id = task_id

            async def callback(self, interaction):
                pass

        class _V(StatefulLayoutView):
            def build_ui(self):
                self.clear_items()
                for task_id in self.ids:
                    self.add_item(ActionRow(_Row(task_id)))

        view = _V(interaction=_make_interaction())
        view.ids = [1, 2]
        view.build_ui()
        store = _registered(view)
        before = {b.task_id: b.custom_id for b in _buttons(view)}

        view.ids = [2]
        view.build_ui()
        store.add_view(view, 1)

        assert _routed(store, before[1]) is None
        assert _routed(store, before[2]).task_id == 2

    async def test_a_method_bound_to_each_row_is_told_apart_by_its_row(self):
        class _Task:
            def __init__(self, name):
                self.name = name

            async def delete(self, interaction):
                pass

        class _V(StatefulLayoutView):
            def build_ui(self):
                self.clear_items()
                for task in self.tasks:
                    self.add_item(ActionRow(StatefulButton(label="Delete", callback=task.delete)))

        a, b = _Task("a"), _Task("b")
        view = _V(interaction=_make_interaction())
        view.tasks = [a, b]
        view.build_ui()
        store = _registered(view)
        before = [btn.custom_id for btn in _buttons(view)]

        view.tasks = [b]
        view.build_ui()
        store.add_view(view, 1)

        assert _routed(store, before[0]) is None
        assert _routed(store, before[1]).original_callback.__self__ is b

    async def test_a_toggle_button_keeps_its_id_across_a_rebuild(self):
        from cascadeui.components.buttons import ToggleButton

        class _V(StatefulLayoutView):
            def build_ui(self):
                self.clear_items()
                self.add_item(ActionRow(ToggleButton(label="Alerts")))

        view = _V(interaction=_make_interaction())
        view.build_ui()
        first = _buttons(view)[0].custom_id
        view.build_ui()

        assert _buttons(view)[0].custom_id == first

    async def test_the_remembered_signatures_are_bounded(self):
        from cascadeui.views.base import _CUSTOM_ID_TOKEN_HISTORY

        view = self._rows([])
        for n in range(_CUSTOM_ID_TOKEN_HISTORY + 50):

            async def cb(interaction, n=n):
                pass

            view._custom_id_token(StatefulButton(label="x", callback=cb))

        assert len(view._custom_id_tokens) == _CUSTOM_ID_TOKEN_HISTORY

    def _choices(self, options, **kwargs):
        from cascadeui.components.patterns.v2 import choice_row

        class _V(StatefulLayoutView):
            def build_ui(self):
                self.clear_items()
                self.add_item(choice_row(self.options, on_select=_pick, **kwargs))

        view = _V(interaction=_make_interaction())
        view.options = options
        view.build_ui()
        return view

    async def test_a_choice_row_click_follows_its_option_when_the_options_change(self):
        view = self._choices({"A": 1, "B": 2, "C": 3})
        store = _registered(view)
        before = {b.label: b.custom_id for b in _buttons(view)}

        view.options = {"B": 2, "C": 3}
        view.build_ui()
        store.add_view(view, 1)

        assert _routed(store, before["A"]) is None
        assert _routed(store, before["B"]).label == "B"
        assert _routed(store, before["C"]).label == "C"

    async def test_a_choice_row_dropdown_drops_a_pick_from_a_changed_option_list(self):
        options = {str(i): i for i in range(8)}
        view = self._choices(options)
        store = _registered(view)
        select = next(c for c in view.walk_children() if isinstance(c, StatefulSelect))
        before = select.custom_id

        view.build_ui()
        assert (
            next(c for c in view.walk_children() if isinstance(c, StatefulSelect)).custom_id
            == before
        )

        view.options = {str(i): i for i in range(1, 9)}
        view.build_ui()
        store.add_view(view, 1)

        assert _routed(store, before, kind=discord.ComponentType.string_select) is None

    async def test_two_choice_rows_on_their_default_custom_id_do_not_collide(self):
        from cascadeui.components.patterns.v2 import choice_row

        async def _other(interaction, value):
            pass

        class _V(StatefulLayoutView):
            def build_ui(self):
                self.clear_items()
                self.add_item(choice_row({"A": 1, "B": 2}, on_select=_pick))
                self.add_item(choice_row({"A": 1, "B": 2}, on_select=_other))

        view = _V(interaction=_make_interaction())
        view.build_ui()

        view._check_placement()
        ids = [b.custom_id for b in _buttons(view)]
        assert len(set(ids)) == 4

    async def test_button_row_and_tab_nav_ids_given_a_base_are_derived_in_a_live_view(self):
        from cascadeui.components.patterns.v2 import button_row, tab_nav

        async def save(interaction):
            pass

        async def reset(interaction):
            pass

        class _V(StatefulLayoutView):
            def build_ui(self):
                self.clear_items()
                self.add_item(button_row({"Save": save, "Reset": reset}, custom_id="actions"))
                self.add_item(tab_nav({"One": save, "Two": reset}, custom_id="tabs"))

        view = _V(interaction=_make_interaction())
        view.build_ui()

        ids = [b.custom_id for b in _buttons(view)]
        assert not any(cid.startswith(("actions_", "tabs_")) for cid in ids), ids
        assert len(set(ids)) == 4

    async def test_a_persistent_view_keeps_the_composed_ids(self):
        from cascadeui.components.patterns.v2 import choice_row
        from cascadeui.views.persistent import PersistentLayoutView

        class _Panel(PersistentLayoutView):
            def build_ui(self):
                self.clear_items()
                self.add_item(choice_row({"A": 1, "B": 2}, on_select=_pick, custom_id="prefs"))

        view = _Panel(persistence_key="prefs-panel")
        view.build_ui()

        assert [b.custom_id for b in _buttons(view)] == ["prefs_0", "prefs_1"]


class TestBuildUiThatReturnsACoroutine:
    """The wrapper checks what ``build_ui`` returned, not what it looks like.

    ``__init_subclass__`` picks a sync or async wrapper with
    ``inspect.iscoroutinefunction``, which answers False for several
    shapes that still hand back a coroutine: a callable instance whose
    ``__call__`` is async, a ``functools.partial`` around one, a plain
    function that returns one. Those take the sync wrapper, so it has to
    look at the result. Trusting the dispatch instead meant the wrapper
    did its work against a tree the body had not built yet -- ids were
    stabilized before any component existed, leaving the random hex
    discord.py assigns, which is the exact dispatch-table churn
    stabilization exists to prevent, and the ambient theme was already
    torn down by the time the body ran.
    """

    @staticmethod
    def _body(view):
        async def _cb(interaction):
            return None

        view.clear_items()
        view.add_item(ActionRow(StatefulButton(label="Go", callback=_cb)))

    def _view_for(self, build_fn):
        cls = type("_V", (StatefulLayoutView,), {"owner_only": False, "build_ui": build_fn})
        return cls(interaction=_make_interaction())

    async def _resolve(self, result):
        if inspect.isawaitable(result):
            return await result
        return result

    async def test_async_dunder_call_builds_and_stabilizes(self):
        body = self._body

        class AsyncCallable:
            async def __call__(self, view):
                body(view)

        view = self._view_for(AsyncCallable())
        await self._resolve(view.build_ui())

        ids = [i.custom_id for i in view.walk_children() if isinstance(i, StatefulButton)]
        assert ids and ids[0].endswith(":Go"), f"not stabilized: {ids}"

    async def test_partial_around_a_coroutine_function_builds_and_stabilizes(self):
        import functools

        body = self._body

        class AsyncCallable:
            async def __call__(self, view):
                body(view)

        view = self._view_for(functools.partial(AsyncCallable()))
        await self._resolve(view.build_ui())

        ids = [i.custom_id for i in view.walk_children() if isinstance(i, StatefulButton)]
        assert ids and ids[0].endswith(":Go"), f"not stabilized: {ids}"

    async def test_sync_function_returning_a_coroutine_builds_and_stabilizes(self):
        body = self._body

        def returns_a_coroutine(view):
            async def inner():
                body(view)

            return inner()

        view = self._view_for(returns_a_coroutine)
        await self._resolve(view.build_ui())

        ids = [i.custom_id for i in view.walk_children() if isinstance(i, StatefulButton)]
        assert ids and ids[0].endswith(":Go"), f"not stabilized: {ids}"

    async def test_theme_is_ambient_while_the_deferred_body_runs(self):
        from cascadeui.theming.context import get_current_theme
        from cascadeui.theming.core import Theme

        theme = Theme("probe", {"accent_colour": discord.Color.gold()})
        seen = []

        class AsyncCallable:
            async def __call__(self, view):
                seen.append(get_current_theme())
                view.clear_items()
                view.add_item(TextDisplay("built"))

        cls = type(
            "_V",
            (StatefulLayoutView,),
            {"owner_only": False, "theme": theme, "build_ui": AsyncCallable()},
        )
        view = cls(interaction=_make_interaction())
        await self._resolve(view.build_ui())

        assert seen and seen[0] is theme

    async def test_plain_sync_build_is_untouched(self):
        view = self._view_for(lambda self: self._body_marker())
        view._body_marker = lambda: self._body(view)

        result = view.build_ui()

        assert not inspect.isawaitable(result), "a sync build must stay sync"


class TestStableCustomIdsAtRefresh:
    """``refresh()`` stabilizes custom_ids for rebuild paths that bypass ``build_ui``.

    Tab switches, paginated page flips, wizard/form step advances, and menu
    category changes all rebuild the component tree outside ``build_ui``.
    Without the refresh-time stabilization, fresh interactive items carry
    ``os.urandom(16).hex()`` ids and the ViewStore dispatch table churns on
    every message.edit -- dropping any in-flight click that arrived after
    the edit but before the client rendered the new payload.
    """

    def _make_view(self):
        class _V(StatefulLayoutView):
            def build_ui(self):
                self.clear_items()
                self.add_item(ActionRow(StatefulButton(label="Initial")))

        view = _V(interaction=_make_interaction())
        view.build_ui()
        view._message = MagicMock()
        view._message.edit = AsyncMock()
        view._message.id = 12345
        return view

    async def test_refresh_stabilizes_ids_for_rebuild_outside_build_ui(self):
        view = self._make_view()

        # Simulate a tab/paginated-style rebuild: clear + add new items.
        # These bypass ``build_ui`` so the __init_subclass__ wrapper does
        # not run ``_stabilize_custom_ids``.
        view.clear_items()
        view.add_item(
            ActionRow(
                StatefulButton(label="Enable"),
                StatefulButton(label="Clear Samples"),
            )
        )

        await view.refresh()

        buttons = [c for c in view.walk_children() if isinstance(c, StatefulButton)]
        prefix = view.id[:8]
        for btn in buttons:
            assert btn.custom_id.startswith(
                f"{prefix}:"
            ), f"custom_id {btn.custom_id!r} missing stable prefix"
        assert "Enable" in buttons[0].custom_id
        assert "Clear Samples" in buttons[1].custom_id

    async def test_repeat_rebuild_outside_build_ui_produces_identical_ids(self):
        """The dispatch-table-churn regression this fix exists to prevent."""
        view = self._make_view()

        def rebuild():
            view.clear_items()
            view.add_item(
                ActionRow(
                    StatefulButton(label="Enable"),
                    StatefulButton(label="Clear Samples"),
                )
            )

        rebuild()
        await view.refresh()
        first = [c.custom_id for c in view.walk_children() if isinstance(c, StatefulButton)]

        rebuild()
        await view.refresh()
        second = [c.custom_id for c in view.walk_children() if isinstance(c, StatefulButton)]

        assert first == second

    async def test_refresh_is_idempotent_on_already_stable_ids(self):
        """A second refresh must not mutate already-stabilized ids."""
        view = self._make_view()

        view.clear_items()
        view.add_item(ActionRow(StatefulButton(label="Action")))
        await view.refresh()
        first = [c.custom_id for c in view.walk_children() if isinstance(c, StatefulButton)]

        # No rebuild -- same items, second refresh.
        await view.refresh()
        second = [c.custom_id for c in view.walk_children() if isinstance(c, StatefulButton)]

        assert first == second

    async def test_refresh_preserves_explicit_custom_ids_during_rebuild(self):
        """Items with ``_provided_custom_id = True`` must remain untouched."""
        view = self._make_view()

        view.clear_items()
        view.add_item(
            ActionRow(
                StatefulButton(label="Auto"),
                StatefulButton(label="Explicit", custom_id="user_chosen"),
            )
        )
        await view.refresh()

        buttons = [c for c in view.walk_children() if isinstance(c, StatefulButton)]
        assert buttons[1].custom_id == "user_chosen"
        # Auto button gets stable prefix, untouched button keeps explicit id.
        assert buttons[0].custom_id != buttons[1].custom_id


class TestStableCustomIdsAtSendAndNavigation:
    """The send and a push destination's edit stabilize ids, as refresh() does.

    A tree built in ``on_load`` or ``__init__`` never passes through the
    ``build_ui`` wrapper, so it shipped discord.py's random ids and the first
    refresh renamed them in an edit of its own, with nothing else changed.
    """

    @staticmethod
    def _ids(view):
        return [i.custom_id for i in view.walk_children() if isinstance(i, discord.ui.Button)]

    @staticmethod
    def _random(custom_id):
        import re

        return re.fullmatch(r"[0-9a-f]{32}", custom_id) is not None

    async def _send(self, view_cls):
        interaction = _make_interaction()
        shipped = []

        async def send_message(**kwargs):
            shipped.append(self._ids(kwargs["view"]))
            interaction.response.is_done.return_value = True

        interaction.response.send_message = AsyncMock(side_effect=send_message)
        view = view_cls(interaction=interaction)
        await view.send()
        view._message.edit = AsyncMock()
        return view, shipped[0]

    async def test_a_tree_built_in_on_load_ships_stable_ids(self):
        class _Loaded(StatefulLayoutView):
            async def on_load(self):
                self.clear_items()
                self.add_item(ActionRow(StatefulButton(label="Go", callback=AsyncMock())))

        view, shipped = await self._send(_Loaded)

        assert shipped and not any(self._random(cid) for cid in shipped)
        assert await view.refresh() is RenderOutcome.SKIPPED
        view._message.edit.assert_not_called()

    async def test_a_tree_built_in_init_ships_stable_ids(self):
        class _Built(StatefulLayoutView):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.add_item(ActionRow(StatefulButton(label="Go", callback=AsyncMock())))

        view, shipped = await self._send(_Built)

        assert shipped and not any(self._random(cid) for cid in shipped)
        assert await view.refresh() is RenderOutcome.SKIPPED

    async def test_a_push_destination_built_in_on_load_ships_stable_ids(self):
        class _Dest(StatefulLayoutView):
            async def on_load(self):
                self.clear_items()
                self.add_item(ActionRow(StatefulButton(label="Back", callback=AsyncMock())))

        source = RenderableLayoutView(interaction=_make_interaction())
        await source.send()
        nav = _make_interaction()
        dest = await source.push(_Dest, interaction=nav)

        shipped = self._ids(nav.response.edit_message.await_args.kwargs["view"])
        assert shipped and not any(self._random(cid) for cid in shipped)
        # The landing records what it shipped, so an unchanged refresh skips
        # rather than renaming the ids in an edit of its own.
        dest._message.edit = AsyncMock()
        assert await dest.refresh() is RenderOutcome.SKIPPED
        assert self._ids(dest) == shipped
        dest._message.edit.assert_not_called()


class TestRenderHashShortCircuit:
    """``refresh()`` skips the Discord REST edit when the component tree
    has not changed since the last successful send/refresh.

    Every Battleship shot wakes 4 subscribers but only 1 or 2 of their
    views actually change content. The short-circuit eliminates the
    redundant message.edit() calls, cutting Discord REST traffic
    proportionally and relieving per-channel rate-limit pressure.
    """

    def _make_view_with_build(self, build_fn):
        class _V(StatefulLayoutView):
            def build_ui(self):
                build_fn(self)

        return _V(interaction=_make_interaction())

    def test_digest_is_deterministic_for_identical_tree(self):
        def build(view):
            view.clear_items()
            view.add_item(
                ActionRow(
                    StatefulButton(label="Fire", custom_id="a"),
                    StatefulButton(label="Close", custom_id="b"),
                )
            )

        view = self._make_view_with_build(build)
        view.build_ui()
        d1 = view._compute_tree_digest()
        view.build_ui()
        d2 = view._compute_tree_digest()
        assert d1 == d2

    def test_digest_changes_when_label_mutates(self):
        marks = ["Fire"]

        def build(view):
            view.clear_items()
            view.add_item(ActionRow(StatefulButton(label=marks[0], custom_id="a")))

        view = self._make_view_with_build(build)
        view.build_ui()
        before = view._compute_tree_digest()

        marks[0] = "Armed"
        view.build_ui()
        after = view._compute_tree_digest()
        assert before != after

    def test_digest_changes_when_disabled_toggles(self):
        enabled = [True]

        def build(view):
            view.clear_items()
            btn = StatefulButton(label="Fire", custom_id="a")
            btn.disabled = not enabled[0]
            view.add_item(ActionRow(btn))

        view = self._make_view_with_build(build)
        view.build_ui()
        before = view._compute_tree_digest()

        enabled[0] = False
        view.build_ui()
        after = view._compute_tree_digest()
        assert before != after

    def test_digest_captures_textdisplay_content(self):
        text = ["Hello"]

        def build(view):
            view.clear_items()
            view.add_item(TextDisplay(text[0]))

        view = self._make_view_with_build(build)
        view.build_ui()
        before = view._compute_tree_digest()

        text[0] = "World"
        view.build_ui()
        after = view._compute_tree_digest()
        assert before != after

    def test_digest_detects_in_place_content_mutation(self):
        """EmojiGrid and similar live TextDisplay subclasses are mutated
        in place and dropped back into a rebuilt tree. The digest must
        detect the content change even when the Python object identity
        is preserved across rebuilds -- content-based hashing is the
        whole point, and any shortcut that compared identity instead
        would silently miss every in-place mutation.
        """
        shared_text = TextDisplay("red")

        def build(view):
            view.clear_items()
            view.add_item(shared_text)

        view = self._make_view_with_build(build)
        view.build_ui()
        before = view._compute_tree_digest()

        # Mutate the same object in place -- no rebind, no new instance.
        shared_text.content = "blue"
        view.build_ui()
        after = view._compute_tree_digest()
        assert before != after

    def test_digest_reflects_select_selection(self):
        """A select's rendered selection lives in opt.default, which the
        scalar wire attributes (placeholder/disabled/...) do not capture.
        A selection-only rebuild must change the digest, or refresh()
        short-circuits and the re-render is silently dropped -- the client
        keeps the stale selection and the next interaction submits stale
        values.
        """
        selected = ["a"]

        def build(view):
            view.clear_items()
            view.add_item(
                ActionRow(
                    StatefulSelect(
                        placeholder="Pick",
                        options=[
                            discord.SelectOption(
                                label="A", value="a", default=(selected[0] == "a")
                            ),
                            discord.SelectOption(
                                label="B", value="b", default=(selected[0] == "b")
                            ),
                        ],
                        min_values=0,
                        max_values=1,
                    )
                )
            )

        view = self._make_view_with_build(build)
        view.build_ui()
        before = view._compute_tree_digest()

        selected[0] = "b"
        view.build_ui()
        after = view._compute_tree_digest()
        assert before != after

    def test_digest_reflects_set_selected(self):
        """set_selected() is the canonical selection-mutation API; it
        rewrites opt.default in place without rebuilding the component.
        The digest must reflect the change so the same select object
        flipped to a new selection re-renders.
        """
        select = StatefulSelect(
            placeholder="Pick",
            options=[
                discord.SelectOption(label="A", value="a"),
                discord.SelectOption(label="B", value="b"),
            ],
            min_values=0,
            max_values=1,
        )

        def build(view):
            view.clear_items()
            view.add_item(ActionRow(select))

        view = self._make_view_with_build(build)
        select.set_selected("a")
        view.build_ui()
        before = view._compute_tree_digest()

        select.set_selected("b")
        view.build_ui()
        after = view._compute_tree_digest()
        assert before != after

    def test_digest_captures_container_accent(self):
        """A theme switch that only recolors a card must change the digest,
        or refresh() would skip the themed re-render.
        """
        accent = [discord.Color.red()]

        def build(view):
            view.clear_items()
            view.add_item(Container(TextDisplay("body"), accent_color=accent[0]))

        view = self._make_view_with_build(build)
        view.build_ui()
        before = view._compute_tree_digest()

        accent[0] = discord.Color.blue()
        view.build_ui()
        after = view._compute_tree_digest()
        assert before != after

    def test_digest_captures_gallery_media_url(self):
        """A rebuild that swaps only a gallery item's URL must change the
        digest, or refresh() short-circuits and the stale image stays on
        screen -- a regenerated banner would silently never re-render.
        """
        url = ["https://example.com/banner_v1.png"]

        def build(view):
            view.clear_items()
            view.add_item(MediaGallery(discord.MediaGalleryItem(url[0])))

        view = self._make_view_with_build(build)
        view.build_ui()
        before = view._compute_tree_digest()

        url[0] = "https://example.com/banner_v2.png"
        view.build_ui()
        after = view._compute_tree_digest()
        assert before != after

    def test_digest_stable_for_identical_gallery(self):
        def build(view):
            view.clear_items()
            view.add_item(MediaGallery(discord.MediaGalleryItem("https://example.com/banner.png")))

        view = self._make_view_with_build(build)
        view.build_ui()
        d1 = view._compute_tree_digest()
        view.build_ui()
        d2 = view._compute_tree_digest()
        assert d1 == d2

    def test_digest_captures_thumbnail_media_url(self):
        """Thumbnails are Section accessories; walk_children() yields them,
        and an avatar-URL-only change must produce a new digest.
        """
        url = ["https://example.com/avatar_v1.png"]

        def build(view):
            view.clear_items()
            view.add_item(Container(Section(TextDisplay("row"), accessory=Thumbnail(media=url[0]))))

        view = self._make_view_with_build(build)
        view.build_ui()
        before = view._compute_tree_digest()

        url[0] = "https://example.com/avatar_v2.png"
        view.build_ui()
        after = view._compute_tree_digest()
        assert before != after

    def test_digest_captures_file_media_url(self):
        url = ["attachment://report_v1.txt"]

        def build(view):
            view.clear_items()
            view.add_item(UIFile(url[0]))

        view = self._make_view_with_build(build)
        view.build_ui()
        before = view._compute_tree_digest()

        url[0] = "attachment://report_v2.txt"
        view.build_ui()
        after = view._compute_tree_digest()
        assert before != after

    async def test_refresh_skips_edit_when_digest_matches(self):
        def build(view):
            view.clear_items()
            view.add_item(ActionRow(StatefulButton(label="Fire", custom_id="a")))

        view = self._make_view_with_build(build)
        view.build_ui()
        view._message = MagicMock()
        view._message.edit = AsyncMock()
        # Simulate a prior successful send that recorded the baseline.
        view._last_tree_digest = view._compute_tree_digest()

        await view.refresh()

        view._message.edit.assert_not_called()

    async def test_refresh_edits_when_digest_differs(self):
        label = ["Fire"]

        def build(view):
            view.clear_items()
            view.add_item(ActionRow(StatefulButton(label=label[0], custom_id="a")))

        view = self._make_view_with_build(build)
        view.build_ui()
        view._message = MagicMock()
        view._message.edit = AsyncMock()
        view._last_tree_digest = view._compute_tree_digest()

        # Mutate and rebuild -- tree now differs.
        label[0] = "Armed"
        view.build_ui()
        await view.refresh()

        view._message.edit.assert_awaited_once()

    async def test_refresh_with_kwargs_never_skips(self):
        """V1 views pass embed= / content= kwargs; those are outside the
        digest, so the short-circuit must not apply."""

        def build(view):
            view.clear_items()
            view.add_item(ActionRow(StatefulButton(label="Fire", custom_id="a")))

        view = self._make_view_with_build(build)
        view.build_ui()
        view._message = MagicMock()
        view._message.edit = AsyncMock()
        view._last_tree_digest = view._compute_tree_digest()

        await view.refresh(content="new content")

        view._message.edit.assert_awaited_once()

    async def test_first_refresh_always_runs(self):
        """A view with no baseline digest has nothing to compare against."""

        def build(view):
            view.clear_items()
            view.add_item(ActionRow(StatefulButton(label="Fire", custom_id="a")))

        view = self._make_view_with_build(build)
        view.build_ui()
        view._message = MagicMock()
        view._message.edit = AsyncMock()
        # _last_tree_digest remains None (no send yet).
        assert view._last_tree_digest is None

        await view.refresh()

        view._message.edit.assert_awaited_once()

    async def test_refresh_updates_baseline_after_successful_edit(self):
        label = ["A"]

        def build(view):
            view.clear_items()
            view.add_item(ActionRow(StatefulButton(label=label[0], custom_id="x")))

        view = self._make_view_with_build(build)
        view.build_ui()
        view._message = MagicMock()
        view._message.edit = AsyncMock()
        view._last_tree_digest = view._compute_tree_digest()

        label[0] = "B"
        view.build_ui()
        first_new_digest = view._compute_tree_digest()
        await view.refresh()
        assert view._last_tree_digest == first_new_digest

        # Second refresh with no changes should now skip.
        await view.refresh()
        assert view._message.edit.await_count == 1  # still 1

    async def test_skip_recorded_in_perf_sample(self):
        def build(view):
            view.clear_items()
            view.add_item(ActionRow(StatefulButton(label="Fire", custom_id="a")))

        view = self._make_view_with_build(build)
        view.build_ui()
        view._message = MagicMock()
        view._message.edit = AsyncMock()
        view._last_tree_digest = view._compute_tree_digest()

        store = get_store()
        store.clear_perf()
        store.enable_perf()
        try:
            await view.refresh()
        finally:
            store.disable_perf()

        assert len(store._refresh_samples) == 1
        sample = store._refresh_samples[-1]
        assert sample["skipped"] is True

    async def test_refresh_increments_edit_counter_only_when_editing(self):
        """End-to-end: a real edit bumps the current dispatch's counter,
        a short-circuited refresh does not. ``refresh()``'s internal
        wiring feeds the store's ``_perf_edit_stack`` on every edit.
        """
        label = ["A"]

        def build(view):
            view.clear_items()
            view.add_item(ActionRow(StatefulButton(label=label[0], custom_id="a")))

        view = self._make_view_with_build(build)
        view.build_ui()
        view._message = MagicMock()
        view._message.edit = AsyncMock()
        view._last_tree_digest = view._compute_tree_digest()

        store = view.state_store
        store.clear_perf()
        store.enable_perf()
        try:
            # Push a fresh counter frame (simulating being inside a dispatch).
            store._perf_edit_stack.append(0)

            # First refresh: tree unchanged, short-circuit fires, no edit.
            await view.refresh()
            assert store._perf_edit_stack[-1] == 0

            # Mutate the tree and refresh -- real edit, counter bumps.
            label[0] = "B"
            view.build_ui()
            await view.refresh()
            assert store._perf_edit_stack[-1] == 1

            # Second real edit stacks on top.
            label[0] = "C"
            view.build_ui()
            await view.refresh()
            assert store._perf_edit_stack[-1] == 2
        finally:
            store._perf_edit_stack.clear()
            store.disable_perf()


class TestRenderDigestWireCoverage:
    """The digest has to move whenever the serialized payload moves.

    ``_compute_tree_digest`` hand-enumerates fields per component type,
    which is a proxy for the payload and drifts from it independently:
    every gap found so far was a wire-visible field the walk never read,
    and each one made ``refresh()`` report ``SKIPPED`` over a tree that
    differed from the one on screen. These tests derive the expected
    field set from ``to_component_dict`` rather than from the walk, so a
    field discord.py adds later fails here instead of silently skipping
    a render.

    The derivation reads top-level keys, which bounds what it can claim: a
    key holding sub-objects (``items``, ``default_values``, ``options``,
    and a custom ``emoji``'s id/animated dict) counts as covered once any
    row mutates any part of it, so a regression inside one would pass. The
    subfield tests below decompose those four by hand, and a fifth such
    key would owe the same. ``media`` and ``file`` are also dicts but
    carry a single wire subfield (``url``), which their whole-key rows
    already mutate.
    """

    # Keys whose content reaches the digest through another entry rather
    # than through a branch of their own. Child components are walked, so
    # their own rows cover them; ``type`` is the discriminator and never
    # varies for a given class.
    STRUCTURAL_KEYS = {
        "type": "component-type discriminator, fixed per class",
        "components": "children are walked, so each child's own row covers it",
        "accessory": "a Section's accessory is walked like any other child",
        "id": "read once by the walk for every component, not per branch",
        # Serialized on every select and read by Discord only inside a modal:
        # "only available for String Selects in modals. It is ignored in
        # messages." Every select this library builds lives in a view, so
        # hashing it would ship an edit for a change Discord discards. This
        # entry is where the rule stops being "present in the payload" and
        # becomes "Discord compares it", which is what the digest promises.
        "required": "modal-only; Discord ignores it on a message component",
    }

    # Buttons and every select class share one digest branch, so a row proving
    # a field reaches the digest proves it for the whole family. Pooling keeps
    # the corpus from demanding five near-identical rows for ``disabled``,
    # which would read as coverage without adding any.
    COVERAGE_POOL = {
        "Button": "interactive",
        "Select": "interactive",
        "UserSelect": "interactive",
        "ChannelSelect": "interactive",
    }

    # (component label, wire key, factory, mutation). The factory returns a
    # fresh top-level child; the mutation changes exactly one wire field on
    # it in place, so ``custom_id`` stays fixed and cannot supply the
    # difference on its own.
    MUTATIONS = [
        (
            "TextDisplay",
            "content",
            lambda: TextDisplay("before"),
            lambda i: setattr(i, "content", "after"),
        ),
        ("TextDisplay", "id", lambda: TextDisplay("t", id=5), lambda i: setattr(i, "id", 6)),
        (
            "Container",
            "accent_color",
            lambda: Container(TextDisplay("t"), accent_colour=discord.Colour.red()),
            lambda i: setattr(i, "accent_colour", discord.Colour.blue()),
        ),
        (
            "Container",
            "spoiler",
            lambda: Container(TextDisplay("t"), spoiler=False),
            lambda i: setattr(i, "spoiler", True),
        ),
        (
            "Thumbnail",
            "media",
            lambda: Section(TextDisplay("t"), accessory=Thumbnail("https://x/a.png")),
            lambda i: setattr(i.accessory, "media", "https://x/b.png"),
        ),
        (
            "Thumbnail",
            "description",
            lambda: Section(
                TextDisplay("t"), accessory=Thumbnail("https://x/a.png", description="a")
            ),
            lambda i: setattr(i.accessory, "description", "b"),
        ),
        (
            "Thumbnail",
            "spoiler",
            lambda: Section(
                TextDisplay("t"), accessory=Thumbnail("https://x/a.png", spoiler=False)
            ),
            lambda i: setattr(i.accessory, "spoiler", True),
        ),
        (
            "MediaGallery",
            "items",
            lambda: MediaGallery(discord.MediaGalleryItem("https://x/a.png")),
            lambda i: setattr(i, "items", [discord.MediaGalleryItem("https://x/b.png")]),
        ),
        (
            "UIFile",
            "file",
            lambda: UIFile("attachment://a.txt"),
            lambda i: setattr(i, "media", "attachment://b.txt"),
        ),
        (
            "UIFile",
            "spoiler",
            lambda: UIFile("attachment://a.txt", spoiler=False),
            lambda i: setattr(i, "spoiler", True),
        ),
        (
            "Separator",
            "spacing",
            lambda: Separator(),
            lambda i: setattr(i, "spacing", discord.SeparatorSpacing.large),
        ),
        (
            "Separator",
            "divider",
            lambda: Separator(visible=True),
            lambda i: setattr(i, "visible", False),
        ),
        (
            "Button",
            "label",
            lambda: ActionRow(discord.ui.Button(label="a", custom_id="b1")),
            lambda i: setattr(i.children[0], "label", "b"),
        ),
        (
            "Button",
            "custom_id",
            lambda: ActionRow(discord.ui.Button(label="a", custom_id="b1")),
            lambda i: setattr(i.children[0], "custom_id", "b2"),
        ),
        (
            "Button",
            "style",
            lambda: ActionRow(discord.ui.Button(label="a", custom_id="b1")),
            lambda i: setattr(i.children[0], "style", discord.ButtonStyle.danger),
        ),
        (
            "Button",
            "disabled",
            lambda: ActionRow(discord.ui.Button(label="a", custom_id="b1")),
            lambda i: setattr(i.children[0], "disabled", True),
        ),
        (
            "Button",
            "emoji",
            lambda: ActionRow(discord.ui.Button(label="a", custom_id="b1", emoji="\N{FIRE}")),
            lambda i: setattr(i.children[0], "emoji", "\N{SNOWFLAKE}"),
        ),
        (
            "Button",
            "url",
            lambda: ActionRow(discord.ui.Button(label="a", url="https://x/a")),
            lambda i: setattr(i.children[0], "url", "https://x/b"),
        ),
        (
            "Button",
            "sku_id",
            lambda: ActionRow(discord.ui.Button(sku_id=111, style=discord.ButtonStyle.premium)),
            lambda i: setattr(i.children[0], "sku_id", 222),
        ),
        (
            "Select",
            "placeholder",
            lambda: ActionRow(
                discord.ui.Select(
                    custom_id="s",
                    placeholder="a",
                    options=[discord.SelectOption(label="L", value="v")],
                )
            ),
            lambda i: setattr(i.children[0], "placeholder", "b"),
        ),
        (
            "Select",
            "min_values",
            lambda: ActionRow(
                discord.ui.Select(
                    custom_id="s", options=[discord.SelectOption(label="L", value="v")]
                )
            ),
            lambda i: setattr(i.children[0], "min_values", 0),
        ),
        (
            "Select",
            "max_values",
            lambda: ActionRow(
                discord.ui.Select(
                    custom_id="s", options=[discord.SelectOption(label="L", value="v")]
                )
            ),
            lambda i: setattr(i.children[0], "max_values", 2),
        ),
        (
            "Select",
            "options",
            lambda: ActionRow(
                discord.ui.Select(
                    custom_id="s", options=[discord.SelectOption(label="L", value="v")]
                )
            ),
            lambda i: setattr(i.children[0].options[0], "label", "Relabelled"),
        ),
        (
            "UserSelect",
            "default_values",
            lambda: ActionRow(discord.ui.UserSelect(custom_id="u")),
            lambda i: setattr(i.children[0], "default_values", [discord.Object(id=7)]),
        ),
        (
            "ChannelSelect",
            "channel_types",
            lambda: ActionRow(
                discord.ui.ChannelSelect(custom_id="c", channel_types=[discord.ChannelType.text])
            ),
            lambda i: setattr(i.children[0], "channel_types", [discord.ChannelType.voice]),
        ),
    ]

    # Fully populated samples, so optional keys (``id``, ``placeholder``,
    # ``emoji``, ``default_values``, ``channel_types``) appear in the key
    # set. discord.py omits an unset optional entirely, so a bare sample
    # would under-enumerate and the coverage test would pass by measuring
    # a smaller surface than the one that ships.
    @staticmethod
    def _populated_samples():
        return {
            "TextDisplay": TextDisplay("t", id=1),
            "Container": Container(
                TextDisplay("t"), accent_colour=discord.Colour.red(), spoiler=True, id=2
            ),
            "Thumbnail": Thumbnail("https://x/a.png", description="d", spoiler=True, id=3),
            "MediaGallery": MediaGallery(discord.MediaGalleryItem("https://x/a.png"), id=4),
            "UIFile": UIFile("attachment://a.txt", spoiler=True, id=5),
            "Separator": Separator(spacing=discord.SeparatorSpacing.large, visible=False, id=6),
            "Button": discord.ui.Button(label="a", custom_id="b1", emoji="\N{FIRE}", disabled=True),
            "Select": discord.ui.Select(
                custom_id="s",
                placeholder="p",
                min_values=0,
                max_values=2,
                options=[discord.SelectOption(label="L", value="v", description="d", default=True)],
            ),
            "UserSelect": discord.ui.UserSelect(
                custom_id="u", default_values=[discord.Object(id=7)]
            ),
            "ChannelSelect": discord.ui.ChannelSelect(
                custom_id="c", channel_types=[discord.ChannelType.text]
            ),
            # Every key on these two is structural today (children are
            # walked, ``id`` rides the walk), so they need no MUTATIONS
            # rows -- they sit in the sample set so a field discord.py
            # adds to either later fails the derivation instead of
            # silently skipping a render.
            "Section": Section(TextDisplay("t"), accessory=Thumbnail("https://x/a.png"), id=8),
            "ActionRow": ActionRow(discord.ui.Button(label="a", custom_id="r1"), id=9),
        }

    def _digest_of(self, child):
        view = RenderableLayoutView(user_id=1, guild_id=2)
        view.add_item(child)
        return view._compute_tree_digest()

    @pytest.mark.parametrize(
        "label,wire_key,factory,mutate",
        MUTATIONS,
        ids=[f"{c}.{k}" for c, k, _, _ in MUTATIONS],
    )
    def test_a_wire_field_change_moves_the_digest(self, label, wire_key, factory, mutate):
        child = factory()
        view = RenderableLayoutView(user_id=1, guild_id=2)
        view.add_item(child)

        before_payload = child.to_component_dict()
        before_digest = view._compute_tree_digest()

        mutate(child)

        after_payload = child.to_component_dict()
        after_digest = view._compute_tree_digest()

        # Guard the guard: a mutation that does not move the payload would
        # make the digest assertion below pass for the wrong reason.
        assert before_payload != after_payload, (
            f"{label}.{wire_key}: the mutation did not change the serialized "
            f"payload, so this row proves nothing about the digest"
        )
        assert before_digest != after_digest, (
            f"{label}.{wire_key} is wire-visible but does not reach "
            f"_compute_tree_digest, so refresh() reports SKIPPED over a tree "
            f"that differs from the one on screen"
        )

    def test_the_mutation_corpus_covers_every_serialized_field(self):
        covered = {}
        for label, wire_key, _, _ in self.MUTATIONS:
            pool = self.COVERAGE_POOL.get(label, label)
            covered.setdefault(pool, set()).add(wire_key)

        missing = []
        for label, sample in self._populated_samples().items():
            pool = self.COVERAGE_POOL.get(label, label)
            for key in sample.to_component_dict():
                if key in self.STRUCTURAL_KEYS:
                    continue
                if key not in covered.get(pool, set()):
                    missing.append(f"{label}.{key}")

        assert not missing, (
            f"these serialized fields have no mutation row, so nothing checks "
            f"whether the digest sees them: {sorted(missing)}. Add a row to "
            f"MUTATIONS, then extend _compute_tree_digest if it fails."
        )

    def test_option_subfields_each_reach_the_digest(self):
        # SelectOption is not a walkable child, so the digest reads it inside
        # the select branch. Only value and default were read before, which
        # let a relabel keeping the same values skip its render.
        for attr, new in (
            ("label", "Relabelled"),
            ("value", "v2"),
            ("description", "described"),
            ("emoji", "\N{FIRE}"),
            ("default", True),
        ):
            row = ActionRow(
                discord.ui.Select(
                    custom_id="s",
                    options=[discord.SelectOption(label="L", value="v")],
                )
            )
            view = RenderableLayoutView(user_id=1, guild_id=2)
            view.add_item(row)
            before = view._compute_tree_digest()
            setattr(row.children[0].options[0], attr, new)
            assert (
                view._compute_tree_digest() != before
            ), f"SelectOption.{attr} does not reach the digest"

    def test_media_gallery_item_subfields_each_reach_the_digest(self):
        # A MediaGalleryItem is not a walkable child either, so the whole
        # list arrives at the walk under one serialized key. The coverage
        # derivation reads top-level keys only, so a row mutating the url
        # marks "items" covered and nothing then checks the siblings.
        for attr, new in (
            ("description", "described"),
            ("spoiler", True),
        ):
            gallery_node = MediaGallery(
                discord.MediaGalleryItem("https://x/a.png", description="a", spoiler=False)
            )
            view = RenderableLayoutView(user_id=1, guild_id=2)
            view.add_item(gallery_node)
            before = view._compute_tree_digest()
            setattr(gallery_node.items[0], attr, new)
            assert (
                view._compute_tree_digest() != before
            ), f"MediaGalleryItem.{attr} does not reach the digest"

    def test_default_value_subfields_each_reach_the_digest(self):
        # Same shape one select family over: default_values is a single
        # serialized key holding id and type, and only a row swapping the id
        # exists above. Without the type, a user pick and a role pick of the
        # same snowflake hash alike.
        row = ActionRow(discord.ui.MentionableSelect(custom_id="m"))
        view = RenderableLayoutView(user_id=1, guild_id=2)
        view.add_item(row)
        select = row.children[0]

        select.default_values = [
            discord.SelectDefaultValue(id=7, type=discord.SelectDefaultValueType.user)
        ]
        as_user = view._compute_tree_digest()
        select.default_values = [
            discord.SelectDefaultValue(id=7, type=discord.SelectDefaultValueType.role)
        ]
        as_role = view._compute_tree_digest()

        assert as_user != as_role, "SelectDefaultValue.type does not reach the digest"

    def test_emoji_subfields_each_reach_the_digest(self):
        # A custom emoji serializes as a dict of name, id, and animated, and
        # the corpus row above swaps two unicode glyphs, which only moves the
        # name. The digest hashes str(emoji), whose <a:name:id> form carries
        # all three -- this pins that, so a narrowing to emoji.name (under
        # which both corpus glyphs still differ) cannot land silently.
        row = ActionRow(
            discord.ui.Button(
                label="a",
                custom_id="b1",
                emoji=discord.PartialEmoji(name="x", id=123, animated=False),
            )
        )
        view = RenderableLayoutView(user_id=1, guild_id=2)
        view.add_item(row)

        before = view._compute_tree_digest()
        row.children[0].emoji = discord.PartialEmoji(name="x", id=123, animated=True)
        assert view._compute_tree_digest() != before, "emoji.animated does not reach the digest"

        before = view._compute_tree_digest()
        row.children[0].emoji = discord.PartialEmoji(name="x", id=124, animated=True)
        assert view._compute_tree_digest() != before, "emoji.id does not reach the digest"

    def test_removing_a_separator_moves_the_digest(self):
        # The walk records no structural signal (no child counts, no depth),
        # so before the Separator branch existed an id-less rule contributed
        # nothing and removing one left the digest byte-identical.
        with_rule = self._digest_of(card(TextDisplay("a"), divider(), TextDisplay("b")))
        without_rule = self._digest_of(card(TextDisplay("a"), TextDisplay("b")))

        assert with_rule != without_rule


class _FakeResponse:
    """Stand-in for the ``aiohttp`` response behind an ``HTTPException``.

    ``HTTPException.__init__`` reads ``status`` and formats ``reason`` into
    its message, so both are required for the real constructor to run.
    """

    def __init__(self, status: int, headers: dict = None):
        self.status = status
        self.reason = "Too Many Requests" if status == 429 else "Error"
        self.headers = headers or {}


def _FakeRateLimit(retry_after: float = 0.5) -> discord.HTTPException:
    """Build a 429 through the real ``HTTPException`` constructor.

    A hand-rolled subclass that assigns ``self.retry_after`` tests a path
    production cannot reach: the real parser keeps only ``code`` and
    ``message`` from the body and never stores ``retry_after``, so the
    delay is only ever available from the ``Retry-After`` header. Building
    the exception the way discord.py does keeps the header the single
    source, matching what ``_handle_rate_limit`` reads at runtime.
    """
    return discord.HTTPException(
        _FakeResponse(429, {"Retry-After": str(retry_after)}),
        {"message": "You are being rate limited.", "code": 0},
    )


class TestRefreshThrottling:
    """Reactive 429 backoff (always on) + proactive cooldown (opt-in via
    ``refresh_cooldown_ms``) arm separate windows: ``_ratelimit_not_before``
    and ``_cooldown_not_before``. Refreshes landing inside either window
    defer via a single scheduled task that re-enters ``on_state_changed``
    once the window expires. Acting-interaction edits waive the cooldown
    window only -- the rate-limit window binds every edit.
    """

    def _make_view(self, build_fn, **class_attrs):
        class _V(StatefulLayoutView):
            def build_ui(self):
                build_fn(self)

        for name, value in class_attrs.items():
            setattr(_V, name, value)
        return _V(interaction=_make_interaction())

    def _prime(self, view):
        """Set up mocked message and initial digest so short-circuit is inactive."""
        view.build_ui()
        view._message = MagicMock()
        view._message.edit = AsyncMock()
        view._last_tree_digest = None  # force first edit through

    def _build_simple(self, view):
        view.clear_items()
        view.add_item(ActionRow(StatefulButton(label="Fire", custom_id="a")))

    def test_cooldown_ms_is_none_by_default(self):
        """Zero-config views never touch throttle state on the hot path."""
        assert _StatefulMixin.refresh_cooldown_ms is None

    def test_cooldown_ms_zero_rejected_at_class_def(self):
        """Zero is not a meaningful cooldown; the ``_POSITIVE_INT_ATTRS``
        validator rejects it so users don't assume 0 means 'off'.
        """
        with pytest.raises(ValueError, match="refresh_cooldown_ms"):

            class _Bad(StatefulLayoutView):
                refresh_cooldown_ms = 0

    def test_cooldown_ms_negative_rejected_at_class_def(self):
        with pytest.raises(ValueError, match="refresh_cooldown_ms"):

            class _Bad(StatefulLayoutView):
                refresh_cooldown_ms = -100

    def test_cooldown_ms_none_accepted(self):
        class _OK(StatefulLayoutView):
            refresh_cooldown_ms = None

        assert _OK.refresh_cooldown_ms is None

    def test_cooldown_ms_positive_int_accepted(self):
        class _OK(StatefulLayoutView):
            refresh_cooldown_ms = 250

        assert _OK.refresh_cooldown_ms == 250

    async def test_cooldown_off_does_not_advance_throttle(self):
        """With ``refresh_cooldown_ms = None`` (default), successful edits
        leave ``_cooldown_not_before`` at 0 -- the proactive path is dead.
        """
        view = self._make_view(self._build_simple)
        self._prime(view)
        await view.refresh()
        assert view._cooldown_not_before == 0.0

    async def test_proactive_cooldown_stamps_after_success(self):
        view = self._make_view(self._build_simple, refresh_cooldown_ms=200)
        self._prime(view)
        before = time.monotonic()
        await view.refresh()
        # Stamp should be at least now + cooldown (minus small scheduling slack).
        assert view._cooldown_not_before >= before + 0.19
        # The cooldown must not bleed into Discord's window.
        assert view._ratelimit_not_before == 0.0

    async def test_refresh_in_cooldown_window_defers(self):
        """Second rapid refresh inside the window must not hit message.edit."""
        view = self._make_view(self._build_simple, refresh_cooldown_ms=500)
        self._prime(view)

        await view.refresh()  # first edit
        assert view._message.edit.await_count == 1

        # Force the digest to differ so short-circuit can't hide the skip.
        view._last_tree_digest = 0
        await view.refresh()  # should defer, not edit
        assert view._message.edit.await_count == 1
        assert view._deferred_refresh_task is not None

        # Cancel the deferred task to avoid leaking into the test runner.
        # Let the event loop pick up the task so its coroutine enters
        # the sleep before cancellation -- avoids a 'never awaited' warning.
        await asyncio.sleep(0)
        view._deferred_refresh_task.cancel()
        try:
            await view._deferred_refresh_task
        except asyncio.CancelledError:
            pass

    async def test_cooldown_drop_intermediate_produces_one_deferred_task(self):
        """N refreshes during the window schedule 1 deferred task, not N."""
        view = self._make_view(self._build_simple, refresh_cooldown_ms=500)
        self._prime(view)

        await view.refresh()  # enters cooldown
        view._last_tree_digest = 0  # force subsequent calls past short-circuit

        await view.refresh()
        first_task = view._deferred_refresh_task
        await view.refresh()
        await view.refresh()
        # Same task instance across all four deferred refreshes.
        assert view._deferred_refresh_task is first_task

        await asyncio.sleep(0)
        first_task.cancel()
        try:
            await first_task
        except asyncio.CancelledError:
            pass

    async def test_deferred_refresh_reenters_on_state_changed(self):
        """Deferred task runs ``on_state_changed`` so ``build_ui`` sees
        the *latest* store state, not kwargs captured at defer time.
        """
        view = self._make_view(self._build_simple, refresh_cooldown_ms=50)
        self._prime(view)
        # Spy on on_state_changed.
        view.on_state_changed = AsyncMock()

        # Run the deferred path directly with a tiny wait.
        await view._deferred_refresh(0.01)

        view.on_state_changed.assert_awaited_once_with(view.state_store.state)

    async def test_deferred_refresh_noop_on_torn_down_view(self):
        view = self._make_view(self._build_simple, refresh_cooldown_ms=50)
        self._prime(view)
        view.on_state_changed = AsyncMock()
        # Teardown, not a bare stop(): a stopped view is still intact, and
        # discord.py stops a view before calling on_timeout, so a pending
        # render belonging to the timeout window still has to ship.
        await view.exit(delete_message=False)

        await view._deferred_refresh(0.01)

        view.on_state_changed.assert_not_awaited()

    async def test_deferred_refresh_still_renders_for_a_stopped_view(self):
        """A stopped view is the timeout window, and its render still owes an edit.

        discord.py stops a view before calling ``on_timeout``, so a render
        coalesced into the cooldown window belongs to a view that is intact.
        """
        view = self._make_view(self._build_simple, refresh_cooldown_ms=50)
        self._prime(view)
        view.on_state_changed = AsyncMock()
        view.stop()

        await view._deferred_refresh(0.01)

        view.on_state_changed.assert_awaited_once_with(view.state_store.state)

    async def test_a_stopped_view_requeues_a_reload_coalesced_mid_render(self):
        """The tail that respawns a successor reads the same signal."""
        view = self._make_view(self._build_simple, refresh_cooldown_ms=50)
        self._prime(view)
        view.stop()

        async def _render(_state):
            # A reload lands while this render is in flight, so the tail has
            # something to hand to a successor.
            view._reload_pending = True

        view.on_state_changed = AsyncMock(side_effect=_render)

        await view._deferred_refresh(0.01)

        assert view._deferred_refresh_task is not None
        view._deferred_refresh_task.cancel()

    async def test_deferred_render_does_not_inherit_the_spawning_interaction(self):
        """``asyncio.create_task`` copies the caller's context, so the
        interaction bound when a deferred task was scheduled is still
        readable inside it. A deferred render must not answer to it.

        Two things go wrong if it does. It claims the acting waiver it is no
        longer owed and skips the cooldown stamp, leaving the next background
        edit unpaced; and it edits through a response slot whose 3-second ack
        window closed long before the boundary arrived.
        """
        loads = []

        class _V(StatefulLayoutView):
            refresh_cooldown_ms = 200

            async def on_load(self):
                loads.append(1)
                self.clear_items()
                self.add_item(ActionRow(StatefulButton(label="Loaded", custom_id="l")))

        view = _V(interaction=_make_interaction())
        view.add_item(ActionRow(StatefulButton(label="Initial", custom_id="i")))
        view._message = MagicMock()
        view._message.id = 555
        view._message.edit = AsyncMock()
        view._last_tree_digest = None

        await view.refresh()  # arms the cooldown window
        armed_at = view._cooldown_not_before
        assert armed_at > 0

        # A click on a manual-refresh button: reload() takes no acting waiver,
        # so it defers -- and the task is created with the interaction bound.
        interaction = _make_interaction()
        interaction.type = discord.InteractionType.component
        interaction.message = MagicMock()
        interaction.message.id = 555
        interaction.response.is_done.return_value = False  # slot never acked
        interaction.response.edit_message = AsyncMock()

        token = _CURRENT_INTERACTION.set(interaction)
        try:
            await view.reload()
        finally:
            _CURRENT_INTERACTION.reset(token)
        assert view._reload_pending is True

        await asyncio.sleep(0.45)  # let the boundary task run

        assert loads == [1]  # the coalesced reload did fetch
        # The boundary render is background work: it stamps the window it owes.
        assert view._cooldown_not_before > armed_at
        # ...and it never touches the spent response slot.
        interaction.response.edit_message.assert_not_awaited()
        assert view._message.edit.await_count == 2

    async def test_deferred_refresh_survives_a_window_extended_mid_sleep(self):
        """A window that grows while the task sleeps must not lose the edit.

        The task wakes, finds the window still active, and re-enters
        ``refresh()``. The gate sees this very task registered as the pending
        retry and declines to schedule a replacement; the task's ``finally``
        then clears the last reference to it. Nothing remains to ship the
        edit, and nothing reports the loss -- so the deferred sleep re-checks
        the window instead of trusting the wait it was handed.
        """
        view = self._make_view(self._build_simple, refresh_cooldown_ms=200)
        self._prime(view)

        await view.refresh()  # ships, stamps a 200ms window
        assert view._message.edit.await_count == 1

        view._last_tree_digest = 0  # force the next edit past the digest skip
        await view.refresh()  # inside the window -> deferred
        assert view._message.edit.await_count == 1
        assert view._deferred_refresh_task is not None

        # Extend the window while the task sleeps.
        await asyncio.sleep(0.1)
        view._ratelimit_not_before = time.monotonic() + 0.3

        # Past the ORIGINAL boundary, the task must still be waiting.
        await asyncio.sleep(0.2)
        assert view._message.edit.await_count == 1
        assert view._deferred_refresh_task is not None

        # Past the EXTENDED boundary, the edit finally ships.
        await until(lambda: view._message.edit.await_count == 2)

    async def test_deferred_refresh_ships_edit_without_build_ui(self):
        """A view that composes its tree outside ``build_ui`` still ships a
        throttled edit at the cooldown boundary.

        Tabs and wizards mutate the tree from their own rebuild methods and
        define no ``build_ui``, so the default ``on_state_changed`` the
        deferred task re-enters had nothing to rebuild and returned without
        editing. The throttled refresh was then dropped outright rather than
        delayed, stranding the message on the previous tree.
        """

        class _NoBuildUI(StatefulLayoutView):
            refresh_cooldown_ms = 50

        view = _NoBuildUI(interaction=_make_interaction())
        view.add_item(ActionRow(StatefulButton(label="Before", custom_id="a")))
        view._message = MagicMock()
        view._message.edit = AsyncMock()
        view._last_tree_digest = None

        await view.refresh()
        assert view._message.edit.await_count == 1

        # Recompose the way a tab switch does, then refresh inside the window.
        view.clear_items()
        view.add_item(ActionRow(StatefulButton(label="After", custom_id="b")))
        await view.refresh()
        assert view._message.edit.await_count == 1

        await asyncio.sleep(0.12)
        assert view._message.edit.await_count == 2

    async def test_reactive_429_stamps_backoff_window(self):
        """429 raised by ``message.edit`` → ``_ratelimit_not_before`` set
        from the ``Retry-After`` header, exception swallowed.

        The upper bound is what makes this honest: the one-second fallback
        would satisfy a lower bound on its own, so a window that lands at
        0.75 proves the header was actually read.
        """
        view = self._make_view(self._build_simple)
        self._prime(view)
        view._message.edit = AsyncMock(side_effect=_FakeRateLimit(retry_after=0.75))

        before = time.monotonic()
        await view.refresh()  # must not raise
        after = time.monotonic()

        # Bracketed by the two readings around the call rather than by a
        # tolerance: the stamp is monotonic() + retry_after taken inside the
        # handler, so this holds however long the refresh takes. The second
        # assertion carries the discrimination the docstring describes.
        assert before + 0.75 <= view._ratelimit_not_before <= after + 0.75
        # Excludes the 60s Cloudflare-ban fallback, which is what a missed
        # header would stamp. Five seconds separates the two by a wide
        # margin and leaves the bound independent of how slow the box is.
        assert view._ratelimit_not_before < before + 5.0
        # A 429 is Discord's window, not the library's opt-in pacing.
        assert view._cooldown_not_before == 0.0

    async def test_reactive_429_without_header_backs_off_for_a_ban(self):
        """A header-less 429 is a Cloudflare ban, and is paced like one.

        discord.py absorbs and retries every ordinary rate-limit itself; it
        raises only when the response carries no ``Via`` header, its own test
        for an IP-level block. A one-second window would put the view back on
        the wire ~1Hz against a block each attempt can extend, so the
        fallback is minutes-scale. The lower bound is what makes this honest:
        a one-second fallback would satisfy any looser assertion.
        """
        view = self._make_view(self._build_simple)
        self._prime(view)
        error = discord.HTTPException(_FakeResponse(429), {"message": "banned", "code": 0})
        view._message.edit = AsyncMock(side_effect=error)

        before = time.monotonic()
        await view.refresh()

        assert view._ratelimit_not_before >= before + 30

    async def test_rate_limited_sibling_exception_is_caught(self):
        """``discord.RateLimited`` subclasses ``DiscordException``, not
        ``HTTPException``, so an ``except discord.HTTPException`` clause
        misses it entirely. It is also the only rate-limit type that carries
        ``retry_after`` as an attribute, so the window comes straight off it.
        """
        view = self._make_view(self._build_simple)
        self._prime(view)
        assert not issubclass(discord.RateLimited, discord.HTTPException)
        view._message.edit = AsyncMock(side_effect=discord.RateLimited(2.0))

        before = time.monotonic()
        await view.refresh()  # must not raise
        after = time.monotonic()

        # Exact bracket, not a tolerance. 2.0 also excludes both fallbacks
        # (one second header-less, thirty for a ban) by construction.
        assert before + 2.0 <= view._ratelimit_not_before <= after + 2.0

    async def test_reactive_429_defers_next_refresh(self):
        view = self._make_view(self._build_simple)
        self._prime(view)
        # First call: 429.
        view._message.edit = AsyncMock(side_effect=_FakeRateLimit(retry_after=0.5))
        await view.refresh()
        first_count = view._message.edit.await_count

        # Second call: still inside backoff window → no edit attempted.
        view._last_tree_digest = 0
        await view.refresh()
        assert view._message.edit.await_count == first_count

        if view._deferred_refresh_task is not None:
            await asyncio.sleep(0)
            view._deferred_refresh_task.cancel()
            try:
                await view._deferred_refresh_task
            except asyncio.CancelledError:
                pass

    async def test_non_429_http_exception_is_reraised(self):
        """Only 429 is swallowed by the reactive path; other HTTP errors
        must propagate to the caller.
        """

        error = discord.HTTPException(_FakeResponse(500), {"message": "server error", "code": 0})
        view = self._make_view(self._build_simple)
        self._prime(view)
        view._message.edit = AsyncMock(side_effect=error)

        with pytest.raises(discord.HTTPException):
            await view.refresh()

    async def test_render_hash_skip_does_not_stamp_cooldown(self):
        """Short-circuited refreshes didn't ship an edit -- they shouldn't
        consume the cooldown window either.
        """
        view = self._make_view(self._build_simple, refresh_cooldown_ms=200)
        self._prime(view)
        # Prime digest so the short-circuit path fires on next refresh.
        view._last_tree_digest = view._compute_tree_digest()

        await view.refresh()

        view._message.edit.assert_not_called()
        assert view._cooldown_not_before == 0.0


class TestActingEditCooldownExemption:
    """An edit answering a click on this view's own message waives the
    proactive cooldown, but never the reactive 429 window.

    ``refresh_cooldown_ms`` paces the library's own background re-renders.
    Applying it to interaction-driven edits taxed page turns by up to the
    full window on any view that also reloads out of band, because the
    background reloads kept the window permanently armed. The rate-limit
    window is Discord's answer rather than the library's pacing, so no
    edit waives it.
    """

    def _make_view(self, **class_attrs):
        class _V(StatefulLayoutView):
            def build_ui(self):
                self.clear_items()
                self.add_item(ActionRow(StatefulButton(label="Fire", custom_id="a")))

        for name, value in class_attrs.items():
            setattr(_V, name, value)
        view = _V(interaction=_make_interaction())
        view.build_ui()
        view._message = MagicMock()
        view._message.id = 555
        view._message.edit = AsyncMock()
        view._last_tree_digest = None
        return view

    def _acting_interaction(self, message_id=555, is_done=True):
        """A component click on the view's own message.

        ``is_done=True`` by default so the edit routes through the channel
        endpoint: it isolates the cooldown decision from the fast path,
        which has its own separate qualification.
        """
        interaction = _make_interaction()
        interaction.type = discord.InteractionType.component
        interaction.message = MagicMock()
        interaction.message.id = message_id
        interaction.response.is_done.return_value = is_done
        return interaction

    async def _refresh_as(self, view, interaction):
        token = _CURRENT_INTERACTION.set(interaction)
        try:
            await view.refresh()
        finally:
            _CURRENT_INTERACTION.reset(token)

    async def test_acting_edit_ships_inside_cooldown_window(self):
        """An acting refresh ships immediately inside an open cooldown window."""
        view = self._make_view(refresh_cooldown_ms=5000)
        view._cooldown_not_before = time.monotonic() + 5  # window wide open

        await self._refresh_as(view, self._acting_interaction())

        view._message.edit.assert_awaited_once()
        assert view._deferred_refresh_task is None

    async def test_background_edit_still_defers_inside_cooldown_window(self):
        """The exemption is scoped to acting edits; background pacing holds."""
        view = self._make_view(refresh_cooldown_ms=5000)
        view._cooldown_not_before = time.monotonic() + 5

        await view.refresh()  # no bound interaction

        view._message.edit.assert_not_called()
        assert view._deferred_refresh_task is not None
        view._deferred_refresh_task.cancel()

    async def test_acting_edit_defers_inside_ratelimit_window(self):
        """A 429 binds every edit. Waiving it would hammer an endpoint that
        has already said stop.
        """
        view = self._make_view(refresh_cooldown_ms=5000)
        view._ratelimit_not_before = time.monotonic() + 5

        await self._refresh_as(view, self._acting_interaction())

        view._message.edit.assert_not_called()
        assert view._deferred_refresh_task is not None
        view._deferred_refresh_task.cancel()

    async def test_acting_edit_does_not_stamp_the_cooldown(self):
        """Otherwise a user holding a button pushes the window ahead of
        every background reload indefinitely.
        """
        view = self._make_view(refresh_cooldown_ms=5000)

        await self._refresh_as(view, self._acting_interaction())

        view._message.edit.assert_awaited_once()
        assert view._cooldown_not_before == 0.0

    async def test_background_edit_still_stamps_the_cooldown(self):
        view = self._make_view(refresh_cooldown_ms=5000)
        before = time.monotonic()

        await view.refresh()

        assert view._cooldown_not_before >= before + 4.9

    async def test_cross_view_click_is_not_acting(self):
        """A click on a different message is a background edit here."""
        view = self._make_view(refresh_cooldown_ms=5000)
        view._cooldown_not_before = time.monotonic() + 5

        await self._refresh_as(view, self._acting_interaction(message_id=999))

        view._message.edit.assert_not_called()
        assert view._deferred_refresh_task is not None
        view._deferred_refresh_task.cancel()

    async def test_reload_never_takes_the_acting_waiver(self):
        """reload()'s gate throttles the on_load FETCH, not just the edit.
        Waiving it for clicks would turn a manual refresh button into an
        unbounded query against the caller's data source.
        """
        loads = []

        class _V(StatefulLayoutView):
            refresh_cooldown_ms = 5000

            async def on_load(self):
                loads.append(1)

            async def refresh(self, **kwargs):
                pass

        view = _V(interaction=_make_interaction())
        view._message = MagicMock()
        view._message.id = 555
        view._cooldown_not_before = time.monotonic() + 5

        token = _CURRENT_INTERACTION.set(self._acting_interaction())
        try:
            await view.reload()
        finally:
            _CURRENT_INTERACTION.reset(token)

        assert loads == []  # the fetch was deferred, not run
        assert view._reload_pending is True
        if view._deferred_refresh_task is not None:
            view._deferred_refresh_task.cancel()


class TestActingViewFastPath:
    """Acting-view ``interaction.response.edit_message`` fast path.

    When the currently-handled interaction targets the acting view's
    message and the response slot is still open, ``refresh()`` routes
    through ``interaction.response.edit_message(view=self, **kwargs)``
    -- one REST round-trip instead of two (ack + channel PATCH). The
    contextvar ``_CURRENT_INTERACTION`` is bound by the stateful
    callback for the duration of the callback + dispatch sequence.
    Disqualified cases (modal interactions, cross-view mismatch,
    already-deferred response, unbound contextvar) fall through to
    the existing webhook/channel paths.
    """

    def _make_view(self, build_fn, **class_attrs):
        class _V(StatefulLayoutView):
            def build_ui(self):
                build_fn(self)

        for name, value in class_attrs.items():
            setattr(_V, name, value)
        return _V(interaction=_make_interaction())

    def _prime(self, view, message_id=555):
        view.build_ui()
        view._message = MagicMock()
        view._message.id = message_id
        view._message.edit = AsyncMock()
        view._last_tree_digest = None

    def _build_simple(self, view):
        view.clear_items()
        view.add_item(ActionRow(StatefulButton(label="Fire", custom_id="a")))

    def _make_acting_interaction(self, message_id=555, is_done=False):
        """Build a component interaction whose ``message.id`` matches
        the primed view's message so the fast path engages.
        """
        interaction = _make_interaction()
        interaction.type = discord.InteractionType.component
        interaction.message = MagicMock()
        interaction.message.id = message_id
        interaction.response.is_done.return_value = is_done
        return interaction

    async def test_fast_path_edits_via_interaction_response(self):
        """Happy path: bound interaction targets the acting view's
        message, response is open -> edit ships through
        ``interaction.response.edit_message``, channel endpoint is
        never touched.
        """
        view = self._make_view(self._build_simple)
        self._prime(view)
        interaction = self._make_acting_interaction()

        token = _CURRENT_INTERACTION.set(interaction)
        try:
            await view.refresh()
        finally:
            _CURRENT_INTERACTION.reset(token)

        interaction.response.edit_message.assert_awaited_once_with(view=view)
        view._message.edit.assert_not_called()

    async def test_cross_view_message_mismatch_falls_through(self):
        """Interaction bound but its ``message.id`` does not match the
        view's message -> the subscriber is a cross-view listener, not
        the acting view. Falls through to the channel endpoint.
        """
        view = self._make_view(self._build_simple)
        self._prime(view, message_id=555)
        interaction = self._make_acting_interaction(message_id=999)

        token = _CURRENT_INTERACTION.set(interaction)
        try:
            await view.refresh()
        finally:
            _CURRENT_INTERACTION.reset(token)

        interaction.response.edit_message.assert_not_called()
        view._message.edit.assert_awaited_once()

    async def test_a_modal_opened_from_the_message_takes_the_fast_path(self):
        """Discord accepts UPDATE_MESSAGE for a modal submitted from a
        component, so the submission is answered with the edit. Sending it
        through the channel endpoint instead left it unordered with the
        next click's edit."""
        view = self._make_view(self._build_simple)
        self._prime(view)
        interaction = self._make_acting_interaction()
        interaction.type = discord.InteractionType.modal_submit

        token = _CURRENT_INTERACTION.set(interaction)
        try:
            await view.refresh()
        finally:
            _CURRENT_INTERACTION.reset(token)

        interaction.response.edit_message.assert_awaited_once()
        view._message.edit.assert_not_called()

    async def test_a_modal_opened_from_a_command_falls_through(self):
        """A modal a slash command opened has no message to update."""
        view = self._make_view(self._build_simple)
        self._prime(view)
        interaction = self._make_acting_interaction()
        interaction.type = discord.InteractionType.modal_submit
        interaction.message = None

        token = _CURRENT_INTERACTION.set(interaction)
        try:
            await view.refresh()
        finally:
            _CURRENT_INTERACTION.reset(token)

        interaction.response.edit_message.assert_not_called()
        view._message.edit.assert_awaited_once()

    async def test_already_deferred_response_falls_through(self):
        """Callback manually called ``respond()`` or ``defer()`` before
        refreshing -> response slot already consumed, fast path cannot
        piggyback. Channel endpoint carries the edit instead.
        """
        view = self._make_view(self._build_simple)
        self._prime(view)
        interaction = self._make_acting_interaction(is_done=True)

        token = _CURRENT_INTERACTION.set(interaction)
        try:
            await view.refresh()
        finally:
            _CURRENT_INTERACTION.reset(token)

        interaction.response.edit_message.assert_not_called()
        view._message.edit.assert_awaited_once()

    async def test_ack_race_interaction_responded_falls_through(self):
        """The is_done() guard passes, but the auto-defer timer acks in the
        window before edit_message's own internal guard, so edit_message raises
        InteractionResponded -- a sibling of HTTPException, not a subclass, so
        the HTTP handler would miss it. The guard raises before any HTTP (no
        edit shipped) and the interaction is already acked, so the fast path
        must fall through to the channel endpoint to ship the edit.
        """
        view = self._make_view(self._build_simple)
        self._prime(view)
        interaction = self._make_acting_interaction()
        interaction.response.edit_message = AsyncMock(
            side_effect=discord.InteractionResponded(MagicMock())
        )

        token = _CURRENT_INTERACTION.set(interaction)
        try:
            await view.refresh()
        finally:
            _CURRENT_INTERACTION.reset(token)

        interaction.response.edit_message.assert_awaited_once()
        view._message.edit.assert_awaited_once()  # fell through to channel

    async def test_no_bound_interaction_falls_through(self):
        """Programmatic dispatch (persistence rehydrate, hook-driven
        refresh) runs outside a component callback -- the contextvar
        default ``None`` disqualifies the fast path. Channel endpoint
        owns the edit.
        """
        view = self._make_view(self._build_simple)
        self._prime(view)

        assert _CURRENT_INTERACTION.get() is None
        await view.refresh()
        view._message.edit.assert_awaited_once()

    async def test_fast_path_429_arms_backoff_and_swallows(self):
        """429 on ``interaction.response.edit_message`` routes through
        ``_handle_rate_limit`` exactly like the channel path: it arms the
        backoff window, swallows the exception, and does NOT immediately
        fall through to the channel endpoint. The edit is re-queued to ship
        once the window clears rather than retried on the spot.

        The upper bound matters as much here as on the channel-path sibling:
        this branch shares the same ``_handle_rate_limit``, so a regression to
        the ban fallback would land here too, and a lower bound alone would
        stay green through it.
        """
        view = self._make_view(self._build_simple)
        self._prime(view)
        interaction = self._make_acting_interaction()
        interaction.response.edit_message = AsyncMock(side_effect=_FakeRateLimit(retry_after=0.75))

        token = _CURRENT_INTERACTION.set(interaction)
        before = time.monotonic()
        try:
            await view.refresh()
        finally:
            _CURRENT_INTERACTION.reset(token)
        after = time.monotonic()

        # Exact bracket; the second assertion excludes the ban fallback the
        # docstring warns a lower bound alone would stay green through.
        assert before + 0.75 <= view._ratelimit_not_before <= after + 0.75
        # Excludes the 60s Cloudflare-ban fallback, which is what a missed
        # header would stamp. Five seconds separates the two by a wide
        # margin and leaves the bound independent of how slow the box is.
        assert view._ratelimit_not_before < before + 5.0
        view._message.edit.assert_not_called()
        if view._deferred_refresh_task is not None:
            view._deferred_refresh_task.cancel()

    async def test_fast_path_general_http_error_falls_through(self):
        """Non-429 HTTP errors (500, 502, network blip) on the fast
        path fall through to the channel endpoint so a transient
        failure on the interaction-response route never drops the
        edit entirely.
        """

        class _OtherError(discord.HTTPException):
            def __init__(self):
                Exception.__init__(self, "500")
                self.status = 500
                self.retry_after = 0

        view = self._make_view(self._build_simple)
        self._prime(view)
        interaction = self._make_acting_interaction()
        interaction.response.edit_message = AsyncMock(side_effect=_OtherError())

        token = _CURRENT_INTERACTION.set(interaction)
        try:
            await view.refresh()
        finally:
            _CURRENT_INTERACTION.reset(token)

        interaction.response.edit_message.assert_awaited_once()
        view._message.edit.assert_awaited_once()

    async def test_a_stalled_render_ships_once_the_click_is_answered(self):
        """The render abandoned on a stall waited for the view's next render,
        so on a slow network a click seemed to do nothing and the next one
        jumped past it."""
        order = []

        async def _stall_forever(*args, **kwargs):
            await asyncio.sleep(60)

        async def go(interaction):
            await view.refresh()

        def build(v):
            v.clear_items()
            v.add_item(ActionRow(StatefulButton(label="Go", custom_id="go", callback=go)))

        view = self._make_view(build, auto_defer_delay=1.5)
        self._prime(view)
        view._message.edit = AsyncMock(side_effect=lambda **kwargs: order.append("edit"))
        interaction = self._make_acting_interaction()
        interaction.response.edit_message = AsyncMock(side_effect=_stall_forever)

        def _defer(*args, **kwargs):
            order.append("defer")
            interaction.response.is_done.return_value = True

        interaction.response.defer = AsyncMock(side_effect=_defer)
        button = next(c for c in view.walk_children() if getattr(c, "custom_id", None) == "go")

        await view._scheduled_task(button, interaction)

        interaction.response.edit_message.assert_awaited_once()
        assert order == ["defer", "edit"]

    async def test_a_stalled_render_ships_when_the_callback_raises_after_it(self):
        """The re-send ran after the callback, so a raise after the stall
        skipped it and the render never reached the message."""

        async def _stall_forever(*args, **kwargs):
            await asyncio.sleep(60)

        async def go(interaction):
            await view.refresh()
            raise RuntimeError("after the render")

        def build(v):
            v.clear_items()
            v.add_item(ActionRow(StatefulButton(label="Go", custom_id="go", callback=go)))

        view = self._make_view(build, auto_defer_delay=1.5)
        self._prime(view)
        view.on_error = AsyncMock()
        interaction = self._make_acting_interaction()
        interaction.response.edit_message = AsyncMock(side_effect=_stall_forever)
        button = next(c for c in view.walk_children() if getattr(c, "custom_id", None) == "go")

        await view._scheduled_task(button, interaction)

        view.on_error.assert_awaited_once()
        view._message.edit.assert_awaited_once()

    async def test_a_stalled_answer_ships_when_the_render_raises_after_it(self):
        async def _stall_forever(*args, **kwargs):
            await asyncio.sleep(60)

        view = self._make_view(self._build_simple, auto_defer_delay=1.5)
        self._prime(view)
        interaction = self._make_acting_interaction()
        interaction.type = discord.InteractionType.modal_submit
        interaction.response.edit_message = AsyncMock(side_effect=_stall_forever)

        async def render():
            await view.refresh()
            raise RuntimeError("after the render")

        with pytest.raises(RuntimeError, match="after the render"):
            await view._render_as_answer(interaction, render)

        view._message.edit.assert_awaited_once()

    async def test_a_stalled_render_is_not_sent_again_after_a_newer_one_landed(self):
        """Sending the stalled render after a newer one landed would put the
        older embed back on a V1 message."""
        from cascadeui.views.view import StatefulView

        class _Panel(StatefulView):
            auto_defer_delay = 1.5

        async def _stall_once(*args, **kwargs):
            interaction.response.edit_message.side_effect = None
            await asyncio.sleep(60)

        async def go(interaction):
            await view.refresh(embed=discord.Embed(title="older"))
            await view.refresh(embed=discord.Embed(title="newer"))

        view = _Panel(interaction=_make_interaction())
        button = StatefulButton(label="Go", custom_id="go", callback=go)
        view.add_item(button)
        view._message = MagicMock(id=555)
        view._message.edit = AsyncMock()
        interaction = self._make_acting_interaction()
        interaction.response.edit_message = AsyncMock(side_effect=_stall_once)

        await view._scheduled_task(button, interaction)

        assert interaction.response.edit_message.call_args.kwargs["embed"].title == "newer"
        view._message.edit.assert_not_awaited()

    async def test_a_stalled_render_is_not_sent_again_after_the_view_closed(self):
        """The close shipped the view's final screen."""

        async def _stall_once(*args, **kwargs):
            interaction.response.edit_message.side_effect = None
            await asyncio.sleep(60)

        async def go(interaction):
            await view.refresh()
            await view.exit()

        def build(v):
            v.clear_items()
            v.add_item(ActionRow(StatefulButton(label="Go", custom_id="go", callback=go)))

        view = self._make_view(build, auto_defer_delay=1.5)
        self._prime(view)
        interaction = self._make_acting_interaction()
        interaction.response.edit_message = AsyncMock(side_effect=_stall_once)
        button = next(c for c in view.walk_children() if getattr(c, "custom_id", None) == "go")
        shipped = view._message.edit.await_count

        await view._scheduled_task(button, interaction)

        # Only the close's own freeze reached the message.
        assert view._message.edit.await_count - shipped == 1

    async def test_an_older_stalled_render_is_not_sent_after_a_newer_one_began(self):
        """A modal's render and a click's both stalled. The older re-send used
        to land first, and the newer one was then skipped as superseded, so a
        V1 message kept the older embed under the newer controls."""
        from cascadeui.views.view import StatefulView

        class _Panel(StatefulView):
            auto_defer_delay = 1.5

        async def _stall(*args, **kwargs):
            await asyncio.sleep(60)

        def _slow_defer(inter):
            async def _defer(*args, **kwargs):
                await asyncio.sleep(0.3)
                inter.response.is_done.return_value = True

            return _defer

        async def newer(interaction):
            await view.refresh(embed=discord.Embed(title="newer"))

        view = _Panel(interaction=_make_interaction())
        button = StatefulButton(label="Next", custom_id="next", callback=newer)
        view.add_item(button)
        view._message = MagicMock(id=555)
        view._message.edit = AsyncMock()
        modal_interaction = self._make_acting_interaction()
        modal_interaction.type = discord.InteractionType.modal_submit
        click_interaction = self._make_acting_interaction()
        for inter in (modal_interaction, click_interaction):
            inter.response.edit_message = AsyncMock(side_effect=_stall)
            inter.response.defer = AsyncMock(side_effect=_slow_defer(inter))

        async def older():
            await view.refresh(embed=discord.Embed(title="older"))

        async def click_later():
            await asyncio.sleep(0.2)
            await view._scheduled_task(button, click_interaction)

        await asyncio.gather(view._render_as_answer(modal_interaction, older), click_later())

        shipped = [c.kwargs["embed"].title for c in view._message.edit.await_args_list]
        assert shipped == ["newer"]

    async def test_a_stalled_render_sends_its_file_again_from_the_start(self):
        """The stalled request had streamed the file, and discord.py does not
        rewind on a first attempt, so the re-send uploaded zero bytes."""
        payload = b"\x89PNG" + b"x" * 5000
        uploaded = []

        async def _upload_then_stall(*args, **kwargs):
            for f in kwargs.get("attachments", []):
                f.fp.read()
            await asyncio.sleep(60)

        async def _channel_edit(**kwargs):
            f = kwargs["attachments"][0]
            f.reset(seek=0)  # what discord.py's request() does on its first attempt
            uploaded.append(len(f.fp.read()))

        async def draw(interaction):
            await view.refresh(
                attachments=[discord.File(io.BytesIO(payload), filename="chart.png")]
            )

        def build(v):
            v.clear_items()
            v.add_item(ActionRow(StatefulButton(label="Draw", custom_id="draw", callback=draw)))

        view = self._make_view(build, auto_defer_delay=1.5)
        self._prime(view)
        view._message.edit = AsyncMock(side_effect=_channel_edit)
        interaction = self._make_acting_interaction()
        interaction.response.edit_message = AsyncMock(side_effect=_upload_then_stall)
        button = next(c for c in view.walk_children() if getattr(c, "custom_id", None) == "draw")

        await view._scheduled_task(button, interaction)

        assert uploaded == [len(payload)]

    async def test_a_fast_path_error_sends_its_file_to_the_channel_from_the_start(self):
        payload = b"x" * 3000
        uploaded = []

        class _ServerError(discord.HTTPException):
            def __init__(self):
                Exception.__init__(self, "500")
                self.status = 500
                self.code = 0

        async def _upload_then_fail(*args, **kwargs):
            for f in kwargs.get("attachments", []):
                f.fp.read()
            raise _ServerError()

        async def _channel_edit(**kwargs):
            f = kwargs["attachments"][0]
            f.reset(seek=0)
            uploaded.append(len(f.fp.read()))

        view = self._make_view(self._build_simple)
        self._prime(view)
        view._message.edit = AsyncMock(side_effect=_channel_edit)
        interaction = self._make_acting_interaction()
        interaction.response.edit_message = AsyncMock(side_effect=_upload_then_fail)

        token = _CURRENT_INTERACTION.set(interaction)
        try:
            await view.refresh(
                attachments=[discord.File(io.BytesIO(payload), filename="chart.png")]
            )
        finally:
            _CURRENT_INTERACTION.reset(token)

        assert uploaded == [len(payload)]

    async def test_a_kept_attachment_goes_to_the_channel_as_it_was(self, caplog):
        # An attachment already on the message has no stream to rewind; the
        # fall-through keeps it as passed and reports nothing about it.
        kept = discord.Attachment(
            data={
                "id": 1,
                "filename": "kept.png",
                "size": 3,
                "url": "https://cdn.example/kept.png",
                "proxy_url": "https://cdn.example/kept.png",
            },
            state=MagicMock(),
        )
        payload = b"x" * 3000
        sent = []

        class _ServerError(discord.HTTPException):
            def __init__(self):
                Exception.__init__(self, "500")
                self.status = 500
                self.code = 0

        async def _upload_then_fail(*args, **kwargs):
            for f in kwargs.get("attachments", []):
                if isinstance(f, discord.File):
                    f.fp.read()
            raise _ServerError()

        async def _channel_edit(**kwargs):
            kept_now, new = kwargs["attachments"]
            new.reset(seek=0)
            sent.append((kept_now, len(new.fp.read())))

        view = self._make_view(self._build_simple)
        self._prime(view)
        view._message.edit = AsyncMock(side_effect=_channel_edit)
        interaction = self._make_acting_interaction()
        interaction.response.edit_message = AsyncMock(side_effect=_upload_then_fail)

        token = _CURRENT_INTERACTION.set(interaction)
        try:
            with caplog.at_level(logging.DEBUG, logger="cascadeui"):
                await view.refresh(
                    attachments=[kept, discord.File(io.BytesIO(payload), filename="chart.png")]
                )
        finally:
            _CURRENT_INTERACTION.reset(token)

        assert sent == [(kept, len(payload))]
        assert not [r for r in caplog.records if "Could not read" in r.getMessage()]

    async def test_an_expired_webhook_sends_its_file_to_the_channel_from_the_start(self):
        from cascadeui.views.view import StatefulView

        payload = b"x" * 3000
        uploaded = []

        class _Expired(discord.HTTPException):
            def __init__(self):
                Exception.__init__(self, "401")
                self.status = 401
                self.code = 50027

        async def _upload_then_expire(*args, **kwargs):
            for f in kwargs.get("attachments", []):
                f.fp.read()
            raise _Expired()

        async def _channel_edit(**kwargs):
            f = kwargs["attachments"][0]
            f.reset(seek=0)
            uploaded.append(len(f.fp.read()))

        view = StatefulView(interaction=_make_interaction())
        view._message = MagicMock(id=555)
        view._message.edit = AsyncMock(side_effect=_channel_edit)
        view._webhook_message = MagicMock()
        view._webhook_message.edit = AsyncMock(side_effect=_upload_then_expire)

        await view.refresh(
            embed=discord.Embed(title="chart"),
            attachments=[discord.File(io.BytesIO(payload), filename="chart.png")],
        )

        assert uploaded == [len(payload)]

    async def test_a_stalled_render_is_sent_again_inside_the_cooldown(self):
        """The re-send still answers the click, so the cooldown the click's own
        edit waived stays waived, and the re-send stamps no window either."""
        from cascadeui.views.view import StatefulView

        class _Board(StatefulView):
            auto_defer_delay = 1.5
            refresh_cooldown_ms = 5000

        async def _stall(*args, **kwargs):
            await asyncio.sleep(60)

        async def turn(interaction):
            await view.refresh(embed=discord.Embed(title="page 2"))

        view = _Board(interaction=_make_interaction())
        button = StatefulButton(label="Next", custom_id="next", callback=turn)
        view.add_item(button)
        view._message = MagicMock(id=555)
        view._message.edit = AsyncMock()
        view._cooldown_not_before = window = time.monotonic() + 5.0
        interaction = self._make_acting_interaction()
        interaction.response.edit_message = AsyncMock(side_effect=_stall)

        await view._scheduled_task(button, interaction)

        assert view._message.edit.await_count == 1
        assert view._message.edit.call_args.kwargs["embed"].title == "page 2"
        assert view._cooldown_not_before == window
        assert view._deferred_refresh_task is None

    async def test_the_ack_after_a_stalled_render_does_not_hold_the_click_lock(self):
        """The ack runs inside the interaction lock, so an ack endpoint that
        hung held every queued click until it answered."""
        from cascadeui.views.view import StatefulView

        class _Panel(StatefulView):
            auto_defer_delay = 0.6

        second_started = asyncio.Event()

        async def _hang(*args, **kwargs):
            await asyncio.sleep(30)

        async def go(interaction):
            if interaction is second:
                second_started.set()
            await view.refresh(embed=discord.Embed(title="go"))

        view = _Panel(interaction=_make_interaction())
        button = StatefulButton(label="Go", custom_id="go", callback=go)
        view.add_item(button)
        view._message = MagicMock(id=555)
        view._message.edit = AsyncMock()
        first = self._make_acting_interaction()
        first.response.edit_message = AsyncMock(side_effect=_hang)
        first.response.defer = AsyncMock(side_effect=_hang)
        second = self._make_acting_interaction(message_id=777)
        second.response.defer = AsyncMock(side_effect=_hang)

        tasks = [asyncio.ensure_future(view._scheduled_task(button, first))]
        try:
            await asyncio.sleep(0.1)
            tasks.append(asyncio.ensure_future(view._scheduled_task(button, second)))
            await asyncio.wait_for(second_started.wait(), timeout=5)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    @staticmethod
    def _v1_board(callback, **class_attrs):
        from cascadeui.views.view import StatefulView

        class _Board(StatefulView):
            auto_defer_delay = 1.5

        for name, value in class_attrs.items():
            setattr(_Board, name, value)
        view = _Board(interaction=_make_interaction())
        button = StatefulButton(label="Next", custom_id="next", callback=callback)
        view.add_item(button)
        view._message = MagicMock(id=555)
        view._message.edit = AsyncMock()
        return view, button

    async def test_a_background_render_that_ships_nothing_leaves_the_re_send(self):
        """Any later refresh() cancelled the re-send, including one a
        transport blip dropped, so a quiet view never showed the click."""

        async def _stall(*args, **kwargs):
            await asyncio.sleep(60)

        async def turn(interaction):
            await view.refresh(embed=discord.Embed(title="page 2"))

        view, button = self._v1_board(turn)
        shipped = []
        blip = {"on": False}

        async def _channel_edit(**kwargs):
            if blip["on"]:
                blip["on"] = False
                raise aiohttp.ServerDisconnectedError()
            shipped.append(kwargs["embed"].title)

        view._message.edit = AsyncMock(side_effect=_channel_edit)
        interaction = self._make_acting_interaction()
        interaction.response.edit_message = AsyncMock(side_effect=_stall)

        async def _defer_after_a_dropped_render(*args, **kwargs):
            async def peer():
                _CURRENT_INTERACTION.set(None)
                blip["on"] = True
                await view.refresh(embed=discord.Embed(title="page 2"))

            await asyncio.ensure_future(peer())
            interaction.response.is_done.return_value = True

        interaction.response.defer = AsyncMock(side_effect=_defer_after_a_dropped_render)

        await view._scheduled_task(button, interaction)

        assert shipped == ["page 2"]

    async def test_the_re_send_does_not_waive_the_cooldown_for_the_retry_it_schedules(self):
        """The re-send's marker reached the retry its 429 scheduled, so that
        background render waived the cooldown too, and every one after it."""

        class _RateLimited(discord.HTTPException):
            def __init__(self):
                Exception.__init__(self, "429")
                self.status = 429
                self.code = 0
                self.retry_after = 0.2
                self.response = MagicMock(headers={"Retry-After": "0.2"})

        async def _stall(*args, **kwargs):
            await asyncio.sleep(60)

        async def turn(interaction):
            await view.refresh(embed=discord.Embed(title="page 2"))

        view, button = self._v1_board(turn, refresh_cooldown_ms=5000)
        edits = {"n": 0}

        async def _channel_edit(**kwargs):
            edits["n"] += 1
            if edits["n"] == 1:
                raise _RateLimited()

        view._message.edit = AsyncMock(side_effect=_channel_edit)
        interaction = self._make_acting_interaction()
        interaction.response.edit_message = AsyncMock(side_effect=_stall)

        await view._scheduled_task(button, interaction)
        await asyncio.wait_for(view._deferred_refresh_task, timeout=5)

        assert edits["n"] == 2
        assert view._cooldown_not_before > time.monotonic()

    async def test_a_v1_stalled_embed_is_sent_after_the_view_exits(self):
        """V1 exit() strips the controls and keeps the embed, so skipping the
        re-send on a closed view left the old embed on the message."""

        async def _stall(*args, **kwargs):
            await asyncio.sleep(60)

        async def place(interaction):
            await view.refresh(embed=discord.Embed(title="Order placed"))
            await view.exit()

        view, button = self._v1_board(place)
        interaction = self._make_acting_interaction()
        interaction.response.edit_message = AsyncMock(side_effect=_stall)

        await view._scheduled_task(button, interaction)

        embeds = [c.kwargs.get("embed") for c in view._message.edit.await_args_list]
        assert [e.title for e in embeds if e is not None] == ["Order placed"]

    async def test_a_v1_stalled_embed_survives_a_later_render_without_one(self):
        """A later render carries the tree, not the embed, so it does not
        replace what the stalled render was sending."""

        async def _stall(*args, **kwargs):
            await asyncio.sleep(60)

        async def report(interaction):
            await view.refresh(embed=discord.Embed(title="Report ready"))
            token = _CURRENT_INTERACTION.set(None)
            try:
                await view.refresh()
            finally:
                _CURRENT_INTERACTION.reset(token)

        view, button = self._v1_board(report)
        interaction = self._make_acting_interaction()
        interaction.response.edit_message = AsyncMock(side_effect=_stall)

        await view._scheduled_task(button, interaction)

        embeds = [c.kwargs.get("embed") for c in view._message.edit.await_args_list]
        assert [e.title for e in embeds if e is not None] == ["Report ready"]

    @pytest.mark.parametrize("endpoint", ["channel", "webhook"])
    async def test_a_v1_stalled_embed_is_not_sent_over_a_newer_one_that_landed(self, endpoint):
        async def _stall(*args, **kwargs):
            await asyncio.sleep(60)

        async def turn(interaction):
            await view.refresh(embed=discord.Embed(title="older"))
            token = _CURRENT_INTERACTION.set(None)
            try:
                await view.refresh(embed=discord.Embed(title="newer"))
            finally:
                _CURRENT_INTERACTION.reset(token)

        view, button = self._v1_board(turn)
        target = view._message.edit
        if endpoint == "webhook":
            view._webhook_message = MagicMock()
            view._webhook_message.edit = target = AsyncMock()
        interaction = self._make_acting_interaction()
        interaction.response.edit_message = AsyncMock(side_effect=_stall)

        await view._scheduled_task(button, interaction)

        assert [c.kwargs["embed"].title for c in target.await_args_list] == ["newer"]

    async def test_a_stalled_render_is_sent_when_the_click_stops_the_view(self):
        """stop() edits nothing, so skipping a finished view's re-send left the
        enabled buttons on screen, which discord.py no longer routes."""

        async def _stall(*args, **kwargs):
            await asyncio.sleep(60)

        async def confirm(interaction):
            for child in view.children:
                child.disabled = True
            await view.refresh()
            view.stop()

        view, button = self._v1_board(confirm)
        interaction = self._make_acting_interaction()
        interaction.response.edit_message = AsyncMock(side_effect=_stall)

        await view._scheduled_task(button, interaction)

        view._message.edit.assert_awaited_once()
        shipped = view._message.edit.await_args.kwargs["view"]
        assert shipped.children and all(c.disabled for c in shipped.children)

    async def test_two_stalls_in_one_click_keep_the_first_ones_embed(self):
        """The click's own render carried the embed and the dispatch's
        re-render did not. Only the last stall was kept, so the re-send
        carried no embed."""
        from cascadeui.views.view import StatefulView

        class _Counter(StatefulView):
            auto_defer_delay = 1.5
            subscribed_actions = {"COMPONENT_INTERACTION"}

            def build_ui(self):
                return None

        async def _stall(*args, **kwargs):
            await asyncio.sleep(60)

        async def inc(interaction):
            await view.refresh(embed=discord.Embed(title="count=1"))

        view = _Counter(interaction=_make_interaction())
        button = StatefulButton(label="+1", custom_id="inc", callback=inc)
        view.add_item(button)
        view._message = MagicMock(id=555)
        view._message.edit = AsyncMock()
        interaction = self._make_acting_interaction()
        interaction.response.edit_message = AsyncMock(side_effect=_stall)

        await view._scheduled_task(button, interaction)
        await view.state_store._flush_notifications()

        assert interaction.response.edit_message.await_count == 2
        embeds = [c.kwargs.get("embed") for c in view._message.edit.await_args_list]
        assert [e.title for e in embeds if e is not None] == ["count=1"]

    async def test_the_continue_button_lands_after_a_re_send_in_flight(self):
        """The arming did not wait for the view's own edits, so a re-send still
        on its way landed over the Continue button, and the armed view then
        dropped every render that could put it back."""
        from cascadeui.views.view import StatefulView

        view = StatefulView(interaction=_make_interaction())
        release = asyncio.Event()
        landed = []

        async def _channel_edit(*, view, **kwargs):
            labels = [getattr(c, "label", None) for c in view.walk_children()]
            if "Live" in labels:
                await release.wait()
            landed.append(labels)

        async def _stall(*args, **kwargs):
            await asyncio.sleep(60)

        async def click(interaction):
            await view.refresh()

        button = StatefulButton(label="Live", custom_id="live", callback=click)
        view.add_item(button)
        view._message = MagicMock(id=555)
        view._message.edit = AsyncMock(side_effect=_channel_edit)
        interaction = self._make_acting_interaction()
        interaction.response.edit_message = AsyncMock(side_effect=_stall)

        clicking = asyncio.ensure_future(view._scheduled_task(button, interaction))
        try:
            await until(lambda: view._message.edit.await_count == 1)
            arming = asyncio.ensure_future(view._arm_refresh_button())
            await asyncio.sleep(0.05)
            release.set()
            await asyncio.wait_for(arming, timeout=5)
            await asyncio.wait_for(clicking, timeout=5)
        finally:
            release.set()

        assert landed[-1] == [view.refresh_button_label]

    async def test_the_continue_button_waits_out_an_edit_started_as_the_last_one_ended(self):
        """The arming woke once. An edit another task started as the one it
        waited for finished still landed over the Continue button."""
        from cascadeui.views.view import StatefulView

        async def _noop(interaction):
            pass

        view = StatefulView(interaction=_make_interaction())
        gates = {"first": asyncio.Event(), "second": asyncio.Event()}
        landed = []

        async def _channel_edit(*, view, **kwargs):
            labels = [getattr(c, "label", None) for c in view.walk_children()]
            gate = gates.get(labels[0]) if labels else None
            if gate is not None:
                await gate.wait()
            landed.append(labels)

        button = StatefulButton(label="first", custom_id="live", callback=_noop)
        view.add_item(button)
        view._message = MagicMock(id=555)
        view._message.edit = AsyncMock(side_effect=_channel_edit)

        async def next_render():
            await asyncio.wait(set(view._edits_pending))
            button.label = "second"
            await view.refresh()

        first = asyncio.ensure_future(view.refresh())
        tasks = [first]
        try:
            await until(lambda: view._message.edit.await_count == 1)
            # Queued on the idle event ahead of the arming, so it wakes first.
            tasks.append(asyncio.ensure_future(next_render()))
            await asyncio.sleep(0)
            tasks.append(asyncio.ensure_future(view._arm_refresh_button()))
            await asyncio.sleep(0.05)
            gates["first"].set()
            await until(lambda: view._message.edit.await_count >= 2)
            await asyncio.sleep(0.05)
            gates["second"].set()
            await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
        finally:
            for gate in gates.values():
                gate.set()

        assert landed[-1] == [view.refresh_button_label]

    async def test_the_continue_button_waits_out_an_edit_started_during_an_async_build(self):
        """An async build_refresh_button() awaits, and a render another task
        started meanwhile landed over the button when the wait ran before it."""
        from cascadeui.views.view import StatefulView

        building = asyncio.Event()

        class _SlowBuild(StatefulView):
            async def build_refresh_button(self):
                building.set()
                await asyncio.sleep(0.05)
                return super().build_refresh_button()

        async def _noop(interaction):
            pass

        view = _SlowBuild(interaction=_make_interaction())
        gate = asyncio.Event()
        landed = []

        async def _channel_edit(*, view, **kwargs):
            labels = [getattr(c, "label", None) for c in view.walk_children()]
            if labels == ["second"]:
                await gate.wait()
            landed.append(labels)

        button = StatefulButton(label="first", custom_id="live", callback=_noop)
        view.add_item(button)
        view._message = MagicMock(id=555)
        view._message.edit = AsyncMock(side_effect=_channel_edit)

        arming = asyncio.ensure_future(view._arm_refresh_button())
        tasks = [arming]
        try:
            await asyncio.wait_for(building.wait(), timeout=5)
            button.label = "second"
            tasks.append(asyncio.ensure_future(view.refresh()))
            await asyncio.sleep(0.1)
            gate.set()
            await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
        finally:
            gate.set()

        assert landed[-1] == [view.refresh_button_label]

    @pytest.mark.parametrize("change", ["exit", "exit closing a child", "sent again"])
    async def test_the_arming_stands_down_for_a_change_made_while_it_waited(self, change):
        """The arming read the timer's checks before awaiting an edit in
        flight. An exit() meanwhile, finished or still closing the views
        attached to it, froze a disabled Continue button over the panel, and a
        send of the view meanwhile was armed at once."""
        from cascadeui.views.layout import StatefulLayoutView

        async def _noop(interaction):
            pass

        child_closing = asyncio.Event()

        class _SlowChild(StatefulLayoutView):
            async def exit(self, **kwargs):
                await child_closing.wait()
                await super().exit(**kwargs)

        view = StatefulLayoutView(interaction=_make_interaction())
        if change == "exit closing a child":
            view.attach_child(_SlowChild(interaction=_make_interaction()))
        else:
            child_closing.set()
        view._ephemeral = True
        view._refresh_handoff_resolved = True
        view._ephemeral_arm_deadline = time.monotonic()
        gate = asyncio.Event()
        landed = []

        async def _edit(*, view, **kwargs):
            labels = [c.label for c in view.walk_children() if isinstance(c, discord.ui.Button)]
            if labels == ["Live 2"]:
                await gate.wait()
            landed.append(labels)

        button = StatefulButton(label="Live", custom_id="live", callback=_noop)
        view.add_item(discord.ui.ActionRow(button))
        view._message = MagicMock(id=555)
        view._message.edit = AsyncMock(side_effect=_edit)
        button.label = "Live 2"
        tasks = [asyncio.ensure_future(view.refresh())]
        try:
            await until(lambda: view._message.edit.await_count == 1)
            tasks.append(asyncio.ensure_future(view._arm_refresh_button()))
            await asyncio.sleep(0.05)
            if change.startswith("exit"):
                tasks.append(asyncio.ensure_future(view.exit()))
                await asyncio.sleep(0.05)
            else:
                # What a send of this instance does first.
                view._ephemeral_arm_deadline = None
            gate.set()
            await asyncio.sleep(0.05)
            child_closing.set()
            await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
        finally:
            gate.set()
            child_closing.set()

        assert not view._refresh_armed
        assert all(view.refresh_button_label not in labels for labels in landed)

    async def test_an_exit_is_not_held_by_a_render_loop_that_keeps_editing(self):
        """The close waited until no edit was in flight, so another task
        rendering the view back to back held exit() for as long as it ran."""
        from cascadeui.views.layout import StatefulLayoutView

        async def _noop(interaction):
            pass

        async def _edit(**kwargs):
            await asyncio.sleep(0.02)

        view = StatefulLayoutView(interaction=_make_interaction())
        button = StatefulButton(label="0", custom_id="tick", callback=_noop)
        view.add_item(ActionRow(button))
        view._message = MagicMock(id=555)
        view._message.edit = AsyncMock(side_effect=_edit)
        ticking = True

        async def ticker():
            n = 0
            while ticking:
                n += 1
                button.label = str(n)
                await view.refresh()

        loop_task = asyncio.ensure_future(ticker())
        try:
            await until(lambda: view._message.edit.await_count >= 2)
            await asyncio.wait_for(view.exit(), timeout=2)
        finally:
            ticking = False
            await loop_task

        assert view.is_finished()

    async def test_an_exit_waits_for_a_render_that_falls_through_to_the_channel(self):
        """The close counted each request, so a render whose first request
        failed and went again through the channel was left out of the close's
        wait, and landed after the freeze with live controls."""
        from cascadeui.views.layout import StatefulLayoutView

        async def _noop(interaction):
            pass

        view = StatefulLayoutView(interaction=_make_interaction())
        view.add_item(ActionRow(StatefulButton(label="Live", custom_id="live", callback=_noop)))
        view._message = MagicMock(id=555)
        first_gate, render_gate = asyncio.Event(), asyncio.Event()
        landed = []

        async def _channel_edit(*, view=None, **kwargs):
            states = [c.disabled for c in view.walk_children() if isinstance(c, Button)]
            if states == [False]:
                await render_gate.wait()
            landed.append(states)

        async def _fails(**kwargs):
            await first_gate.wait()
            raise discord.HTTPException(MagicMock(status=500, reason="x"), "x")

        view._message.edit = AsyncMock(side_effect=_channel_edit)
        interaction = self._make_acting_interaction()
        interaction.response.edit_message = AsyncMock(side_effect=_fails)

        async def acting_render():
            token = _CURRENT_INTERACTION.set(interaction)
            try:
                return await view.refresh()
            finally:
                _CURRENT_INTERACTION.reset(token)

        tasks = [asyncio.ensure_future(acting_render())]
        try:
            await until(lambda: interaction.response.edit_message.await_count == 1)
            tasks.append(asyncio.ensure_future(view.exit(delete_message=False)))
            await asyncio.sleep(0.05)
            first_gate.set()
            await asyncio.sleep(0.1)
            render_gate.set()
            await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
        finally:
            first_gate.set()
            render_gate.set()

        assert landed[-1] == [True]

    async def test_a_v1_render_during_an_exits_wait_leaves_the_controls_off(self):
        """V1 exit() strips the controls. A render started while the exit
        waited for an older edit shipped them disabled, and landing last put
        them back under the embed."""
        from cascadeui.views.view import StatefulView

        async def _noop(interaction):
            pass

        gates = {"first": asyncio.Event(), "late": asyncio.Event()}
        landed = []

        async def _edit(*, view, **kwargs):
            label = next((c.label for c in view.children), None) if view else None
            gate = gates.get(label)
            if gate is not None:
                await gate.wait()
            landed.append([] if view is None else list(view.children))

        view = StatefulView(interaction=_make_interaction())
        button = StatefulButton(label="first", custom_id="b", callback=_noop)
        view.add_item(button)
        view._message = MagicMock(id=555)
        view._message.edit = AsyncMock(side_effect=_edit)
        tasks = [asyncio.ensure_future(view.refresh())]
        try:
            await until(lambda: view._message.edit.await_count == 1)
            tasks.append(asyncio.ensure_future(view.exit()))
            await until(view.is_finished)
            button.label = "late"
            tasks.append(asyncio.ensure_future(view.refresh()))
            await asyncio.sleep(0.05)
            gates["first"].set()
            await until(lambda: view._message.edit.await_count >= 3)
            await asyncio.sleep(0.05)
            gates["late"].set()
            await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
        finally:
            for gate in gates.values():
                gate.set()

        assert landed[-1] == []

    async def test_an_arming_a_failed_replace_held_off_arms_when_it_fails(self):
        """The arming found the view closing while a replace() was under way
        and gave up; the replace failed, the view stayed live, and nothing
        armed it again, so the panel reached the token's end with no
        Continue button."""
        from cascadeui.setup import setup_middleware
        from cascadeui.views.layout import StatefulLayoutView

        parked, fail = asyncio.Event(), asyncio.Event()

        async def gate(action, state, next_fn):
            if action["type"] == "NAVIGATION_REPLACE":
                parked.set()
                await fail.wait()
                raise RuntimeError("navigation refused")
            return await next_fn(action, state)

        await setup_middleware(gate)

        async def _noop(interaction):
            pass

        class _Dest(StatefulLayoutView):
            pass

        view = StatefulLayoutView(interaction=_make_interaction())
        view._ephemeral = True
        view._refresh_handoff_resolved = True
        view._ephemeral_arm_deadline = time.monotonic()
        edit_gate = asyncio.Event()

        async def _edit(*, view, **kwargs):
            labels = [c.label for c in view.walk_children() if isinstance(c, discord.ui.Button)]
            if labels == ["Live 2"]:
                await edit_gate.wait()

        button = StatefulButton(label="Live", custom_id="live", callback=_noop)
        view.add_item(ActionRow(button))
        view._message = MagicMock(id=555)
        view._message.edit = AsyncMock(side_effect=_edit)
        button.label = "Live 2"
        rendering = asyncio.ensure_future(view.refresh())
        try:
            await until(lambda: view._message.edit.await_count == 1)
            arming = asyncio.ensure_future(view._arm_refresh_button())
            await asyncio.sleep(0.02)
            replacing = asyncio.ensure_future(view.replace(_Dest))
            await asyncio.wait_for(parked.wait(), timeout=2)
            edit_gate.set()
            await asyncio.wait_for(arming, timeout=2)
            assert not view._refresh_armed
            fail.set()
            with pytest.raises(RuntimeError, match="navigation refused"):
                await replacing
            await until(lambda: view._refresh_armed)
        finally:
            edit_gate.set()
            fail.set()
            await rendering

    async def test_an_edit_that_never_lands_does_not_keep_the_continue_button_off(self):
        """The arming waited for the view's own edits with no bound, so one
        that never returned kept the button off until the token expired."""
        from cascadeui.views.view import StatefulView

        async def _noop(interaction):
            pass

        view = StatefulView(interaction=_make_interaction())
        stuck = asyncio.Event()
        landed = []

        async def _channel_edit(*, view, **kwargs):
            labels = [getattr(c, "label", None) for c in view.walk_children()]
            if labels == ["Live"]:
                await stuck.wait()
            landed.append(labels)

        view.add_item(StatefulButton(label="Live", custom_id="live", callback=_noop))
        view._message = MagicMock(id=555)
        view._message.edit = AsyncMock(side_effect=_channel_edit)
        view._ephemeral = True
        view._refresh_handoff_resolved = True
        # 0.2s of token left: the wait gives up after half of it.
        view._ephemeral_arm_deadline = time.monotonic() - view.refresh_warning_seconds + 0.2

        rendering = asyncio.ensure_future(view.refresh())
        try:
            await until(lambda: view._message.edit.await_count == 1)
            await asyncio.wait_for(view._arm_refresh_button(), timeout=5)
        finally:
            stuck.set()
            await rendering

        assert view._refresh_armed
        assert landed[0] == [view.refresh_button_label]

    async def test_a_stalled_embed_survives_a_later_render_that_set_only_the_text(self):
        """A later render that set content= counted as replacing everything
        the stalled one carried, so its embed never reached the message; the
        re-send also leaves the newer text in place."""

        async def _stall(*args, **kwargs):
            await asyncio.sleep(60)

        async def report(interaction):
            await view.refresh(content="working", embed=discord.Embed(title="Report ready"))
            token = _CURRENT_INTERACTION.set(None)
            try:
                await view.refresh(content="done")
            finally:
                _CURRENT_INTERACTION.reset(token)

        view, button = self._v1_board(report)
        interaction = self._make_acting_interaction()
        interaction.response.edit_message = AsyncMock(side_effect=_stall)

        await view._scheduled_task(button, interaction)

        resent = [c.kwargs for c in view._message.edit.await_args_list if "embed" in c.kwargs]
        assert [kwargs["embed"].title for kwargs in resent] == ["Report ready"]
        assert "content" not in resent[0]

    async def test_a_stall_whose_content_was_all_replaced_sends_nothing(self):
        """With every keyword it carried replaced by a later render, a stalled
        render is owed nothing. Sent anyway, it shipped a tree the view had
        changed since, which no code asked to render."""

        async def _stall(*args, **kwargs):
            await asyncio.sleep(60)

        async def turn(interaction):
            await view.refresh(embed=discord.Embed(title="older"))
            token = _CURRENT_INTERACTION.set(None)
            try:
                await view.refresh(embed=discord.Embed(title="newer"))
            finally:
                _CURRENT_INTERACTION.reset(token)
            # Changed and not rendered yet: the next refresh the view makes ships it.
            button.label = "Changed"

        view, button = self._v1_board(turn)
        interaction = self._make_acting_interaction()
        interaction.response.edit_message = AsyncMock(side_effect=_stall)

        await view._scheduled_task(button, interaction)

        assert [c.kwargs["embed"].title for c in view._message.edit.await_args_list] == ["newer"]

    @pytest.mark.parametrize("later", ["embed", "attachments"])
    async def test_a_stalled_embed_and_file_resend_only_what_nothing_replaced(self, later):
        """An edit keeps whatever it does not name, so the message a render
        that never stalled leaves has the later render's part and the stalled
        render's other part. Treating the embed and the file as one unit sent
        neither, and the message then showed the part from before the click
        whenever Discord had not applied the abandoned request."""

        async def _stall(*args, **kwargs):
            await asyncio.sleep(60)

        async def report(interaction):
            await view.refresh(
                embed=discord.Embed(title="chart"),
                attachments=[discord.File(io.BytesIO(b"png"), filename="chart.png")],
            )
            token = _CURRENT_INTERACTION.set(None)
            try:
                if later == "embed":
                    await view.refresh(embed=discord.Embed(title="text only"))
                else:
                    await view.refresh(attachments=[])
            finally:
                _CURRENT_INTERACTION.reset(token)

        view, button = self._v1_board(report)
        interaction = self._make_acting_interaction()
        interaction.response.edit_message = AsyncMock(side_effect=_stall)

        await view._scheduled_task(button, interaction)

        resent = view._message.edit.await_args_list[-1].kwargs
        assert view._message.edit.await_count == 2
        if later == "embed":
            assert "embed" not in resent
            assert [f.filename for f in resent["attachments"]] == ["chart.png"]
        else:
            assert "attachments" not in resent
            assert resent["embed"].title == "chart"

    async def test_a_view_sent_again_replaces_a_stalled_embed(self):
        """A send counted as no render, so the click's older embed was sent
        again onto the new message, over the one the send had just posted."""
        import contextvars

        from cascadeui.views.view import StatefulView

        async def _stall(*args, **kwargs):
            await asyncio.sleep(60)

        view = StatefulView(interaction=_make_interaction())
        await view.send(embed=discord.Embed(title="posted"))
        old = view._message
        slash = _make_interaction()
        new_message = slash.original_response.return_value
        new_message.id = 1001

        async def click(interaction):
            await view.refresh(embed=discord.Embed(title="click render (older)"))
            view.interaction = slash
            # A command sending the panel again while the click still runs, in a
            # context of its own (create_task's context= needs Python 3.11).
            await contextvars.Context().run(
                asyncio.create_task,
                view.send(embed=discord.Embed(title="sent again (newer)")),
            )

        button = StatefulButton(label="Go", custom_id="go", callback=click)
        view.add_item(button)
        interaction = self._make_acting_interaction(message_id=old.id)
        interaction.response.edit_message = AsyncMock(side_effect=_stall)

        await view._scheduled_task(button, interaction)

        assert view._message is new_message
        new_message.edit.assert_not_awaited()

    async def test_a_stalled_embed_after_a_push_logs_no_hand_over_warning(self, caplog):
        """The library's own re-send reached refresh() on the view that pushed,
        which warned the developer about a call no code of theirs made."""
        from cascadeui.views.view import StatefulView

        class _Child(StatefulView):
            pass

        calls = {"n": 0}

        async def _first_stalls(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                await asyncio.sleep(60)
            interaction.response.is_done.return_value = True
            interaction.response.type = discord.InteractionResponseType.message_update

        async def open_child(interaction):
            await view.refresh(embed=discord.Embed(title="selected"))
            await view.push(_Child, interaction)

        view, button = self._v1_board(open_child)
        interaction = self._make_acting_interaction()
        interaction.response.edit_message = AsyncMock(side_effect=_first_stalls)

        with caplog.at_level(logging.WARNING, logger="cascadeui"):
            await view._scheduled_task(button, interaction)

        assert view.current_view is not view
        assert not [r for r in caplog.records if "handed its panel on" in r.getMessage()]

    async def test_an_expired_webhook_sends_a_path_file_to_the_channel(self, tmp_path):
        """discord.py closes a file it opened from a path once the webhook edit
        ends, so the channel attempt raised on a closed file."""
        from discord.http import handle_message_parameters

        from cascadeui.views.view import StatefulView

        path = tmp_path / "chart.png"
        path.write_bytes(b"x" * 3000)
        uploaded = []

        class _Expired(discord.HTTPException):
            def __init__(self):
                Exception.__init__(self, "401")
                self.status = 401
                self.code = 50027

        async def _webhook_edit(**kwargs):
            # As WebhookMessage.edit does: the files close when it ends.
            with handle_message_parameters(attachments=kwargs["attachments"]):
                raise _Expired()

        async def _channel_edit(**kwargs):
            f = kwargs["attachments"][0]
            f.reset(seek=0)
            uploaded.append(len(f.fp.read()))
            f.close()

        view = StatefulView(interaction=_make_interaction())
        view._message = MagicMock(id=555)
        view._message.edit = AsyncMock(side_effect=_channel_edit)
        view._webhook_message = MagicMock()
        view._webhook_message.edit = AsyncMock(side_effect=_webhook_edit)

        await view.refresh(embed=discord.Embed(title="chart"), attachments=[discord.File(path)])

        assert uploaded == [3000]

    async def test_a_stalled_render_that_fails_to_send_logs_why(self, caplog):
        """A failure that is not Discord's logged ``status=? code=?`` and no
        traceback."""
        from cascadeui.utils.responses import ship_stalled_renders

        view = MagicMock()
        view._ship_stalled_render = AsyncMock(side_effect=ValueError("bad tree"))

        with caplog.at_level(logging.WARNING, logger="cascadeui"):
            await ship_stalled_renders({1: (view, [({}, 0)])})

        record = next(r for r in caplog.records if "stalled render" in r.getMessage())
        assert record.exc_info is not None and record.exc_info[1].args == ("bad tree",)

    async def test_fast_path_timeout_skips_channel_fallthrough(self):
        """Slow ``edit_message`` response (Discord latency spike, ephemeral
        backend under load) would starve the interaction ack past the 3s
        deadline under the fast path's one-HTTP-call contract. The
        ``wait_for`` guard caps the fast path at
        ``max(0.5, auto_defer_delay - 1.0)`` and cancels the in-flight
        edit on stall.

        On stall, refresh returns immediately rather than falling through
        to the channel endpoint. A second edit attempt on top of the
        cancelled fast path would consume the auto-defer timer's budget
        for its own ack call, producing the very interaction-failed
        toast the ack-coupling design exists to prevent. The auto-defer
        timer fires the standalone ack at ``auto_defer_delay`` seconds
        with the full remaining budget.

        The render-hash digest is invalidated so the next refresh ships
        unconditionally; whether Discord processed the cancelled edit
        server-side is indeterminate, and a redundant edit is cheaper
        than a stuck UI.

        ``_ratelimit_not_before`` is NOT armed: a stall is not a
        rate-limit signal, so the next refresh should not be throttled.
        """

        async def _stall_forever(*args, **kwargs):
            await asyncio.sleep(60)

        # Compress auto_defer_delay so the derived fast-path timeout
        # (max(0.5, delay - 1.0)) floors at 0.5s and the test is fast.
        view = self._make_view(self._build_simple, auto_defer_delay=1.5)
        self._prime(view)
        interaction = self._make_acting_interaction()
        interaction.response.edit_message = AsyncMock(side_effect=_stall_forever)

        # Seed the digest to a value that does NOT match the current
        # tree.  A matching digest would short-circuit refresh before
        # the fast path engages; a non-matching one lets the fast path
        # run AND lets the post-refresh ``is None`` assertion below
        # prove the new code path invalidated it (the old fall-through
        # code path would have set it to the current digest, not None).
        view._last_tree_digest = view._compute_tree_digest() + 1

        token = _CURRENT_INTERACTION.set(interaction)
        before = time.monotonic()
        try:
            await view.refresh()
        finally:
            _CURRENT_INTERACTION.reset(token)
        elapsed = time.monotonic() - before

        # Fast path was attempted and cancelled by wait_for.
        interaction.response.edit_message.assert_awaited_once()
        # Channel-endpoint fall-through was SKIPPED.  A second edit on
        # top of the cancelled fast path would drain the auto-defer
        # timer's ack budget under genuine Discord-side latency.
        view._message.edit.assert_not_called()
        # Returned within the derived timeout window (0.5s), not the
        # 60s sleep -- proves the wait_for guard fired.
        assert elapsed < 2.0
        # Stall is not a rate-limit signal: backoff window stays at zero.
        assert view._ratelimit_not_before == 0.0
        # Digest invalidated so the next refresh ships unconditionally.
        assert view._last_tree_digest is None


class TestAFileSentAgainUploadsWhole:
    """A ``discord.File`` passed to a second send or edit uploaded from where
    the first request's read stopped: zero bytes, which Discord shows as a
    broken image. Each request here is modeled on aiohttp 3.11, which closes
    the stream once it has written it (3.12 leaves it open)."""

    PAYLOAD = b"\x89PNG" + b"x" * 4000

    @staticmethod
    def _upload(f):
        """The bytes one request carrying ``f`` uploads."""
        f.reset(seek=0)  # discord.py's first attempt does not seek
        data = f.fp.read()
        f.fp.close()  # aiohttp 3.11 closes the stream it wrote
        f.close()  # discord.py closes the File when the request ends
        return data

    def _endpoint(self, uploads, returns=None):
        async def request(*args, **kwargs):
            files = list(kwargs.get("attachments") or kwargs.get("files") or ())
            if kwargs.get("file") is not None:
                files.append(kwargs["file"])
            uploads.extend(self._upload(f) for f in files)
            return returns

        return request

    @pytest.mark.parametrize("source", ["memory", "path"])
    async def test_one_file_goes_whole_with_every_refresh(self, source, tmp_path):
        if source == "path":
            path = tmp_path / "chart.png"
            path.write_bytes(self.PAYLOAD)
            chart = discord.File(str(path), filename="chart.png")
        else:
            chart = discord.File(io.BytesIO(self.PAYLOAD), filename="chart.png")
        uploads = []
        view = RenderableLayoutView(interaction=_make_interaction())
        view._message = MagicMock(id=555)
        view._message.edit = AsyncMock(side_effect=self._endpoint(uploads))

        for _ in range(3):
            await view.refresh(attachments=[chart])

        assert uploads == [self.PAYLOAD] * 3

    async def test_a_file_a_reply_carried_goes_whole_with_a_send(self):
        from cascadeui.utils.responses import respond_safe

        chart = discord.File(io.BytesIO(self.PAYLOAD), filename="chart.png")
        uploads = []
        interaction = _make_interaction(is_done=True)
        interaction.followup.send = AsyncMock(side_effect=self._endpoint(uploads))
        await respond_safe(interaction, "Here it is", file=chart)

        ctx = MagicMock()
        ctx.author.id = 100
        ctx.guild.id = 200
        ctx.send = AsyncMock(side_effect=self._endpoint(uploads, returns=MagicMock(id=999)))
        view = RenderableLayoutView(context=ctx, user_id=100, guild_id=200)
        await view.send(file=chart)

        assert uploads == [self.PAYLOAD] * 2

    @pytest.mark.parametrize("answered", [False, True], ids=["first_reply", "followup"])
    async def test_one_file_goes_whole_with_every_reply(self, answered):
        from cascadeui.utils.responses import respond_safe

        chart = discord.File(io.BytesIO(self.PAYLOAD), filename="chart.png")
        uploads = []

        for _ in range(2):
            interaction = _make_interaction(is_done=answered)
            interaction.response.send_message = AsyncMock(side_effect=self._endpoint(uploads))
            interaction.followup.send = AsyncMock(side_effect=self._endpoint(uploads))
            await respond_safe(interaction, "Here it is", file=chart)

        assert uploads == [self.PAYLOAD] * 2

    async def test_a_send_that_lost_the_ack_race_sends_its_file_whole_as_a_followup(self):
        """The refused request had streamed the file, and the followup
        uploaded it from where that read stopped."""
        chart = discord.File(io.BytesIO(self.PAYLOAD), filename="chart.png")
        uploads = []
        interaction = _make_interaction(is_done=False)

        async def lost_race(**kwargs):
            self._upload(kwargs["file"])
            raise discord.HTTPException(MagicMock(status=400), {"code": 40060, "message": "x"})

        interaction.response.send_message = AsyncMock(side_effect=lost_race)
        interaction.followup.send = AsyncMock(
            side_effect=self._endpoint(uploads, returns=MagicMock(id=999))
        )
        view = RenderableLayoutView(interaction=interaction)

        await view.send(file=chart)

        assert uploads == [self.PAYLOAD]

    def test_a_file_never_sent_is_handed_on_as_it_is(self):
        """A new File around it would take discord.py's guard on close() for
        the stream's own close, and a file opened from a path would then never
        be closed."""
        from cascadeui.utils.responses import _rewind_files

        chart = discord.File(io.BytesIO(self.PAYLOAD), filename="chart.png")
        kwargs = {"attachments": [chart]}

        _rewind_files(kwargs)

        assert kwargs["attachments"][0] is chart

    def test_a_file_sent_before_keeps_its_name_spoiler_and_description(self):
        from cascadeui.utils.responses import _rewind_files

        chart = discord.File(
            io.BytesIO(self.PAYLOAD), filename="chart.png", spoiler=True, description="Weekly"
        )
        self._upload(chart)
        kwargs = {"file": chart}

        _rewind_files(kwargs)

        again = kwargs["file"]
        assert again is not chart
        assert (again.filename, again.spoiler, again.description) == (
            "SPOILER_chart.png",
            True,
            "Weekly",
        )
        assert self._upload(again) == self.PAYLOAD


class TestEphemeralActingRefresh:
    """Ephemeral acting views use the same edit-as-ack fast path as
    non-ephemeral ones.

    ``interaction.response.edit_message()`` is an UPDATE_MESSAGE response
    scoped to the CURRENT click's own interaction and token (independent
    of the ephemeral message's original send token), so it works
    identically regardless of ephemeral status. That is unlike
    ``self._message.edit()``, a webhook PATCH bound to the ORIGINAL send's
    token, which is the genuinely slower path (and the one a disqualified
    or failed fast path still falls through to). ``acting``'s three
    preconditions (component type, message present, message id match)
    already establish everything the fast path needs; ephemeral status
    is not a fourth precondition.
    """

    def _make_ephemeral_view(self):
        class _V(StatefulLayoutView):
            def build_ui(self):
                self.clear_items()
                self.add_item(ActionRow(StatefulButton(label="Fire", custom_id="a")))

        view = _V(interaction=_make_interaction())
        view._ephemeral = True
        view.build_ui()
        view._message = MagicMock()
        view._message.id = 555
        view._message.edit = AsyncMock()
        view._last_tree_digest = None
        return view

    def _make_acting_interaction(self, message_id=555, is_done=False):
        interaction = _make_interaction()
        interaction.type = discord.InteractionType.component
        interaction.message = MagicMock()
        interaction.message.id = message_id
        interaction.response.is_done.return_value = is_done
        return interaction

    async def test_acting_ephemeral_uses_the_fast_path(self):
        """An open response slot on an acting ephemeral view ships the edit
        through ``interaction.response.edit_message`` -- the same one
        round-trip a non-ephemeral acting view gets. The webhook handle
        (``self._message``) is never touched.
        """
        view = self._make_ephemeral_view()
        interaction = self._make_acting_interaction()

        token = _CURRENT_INTERACTION.set(interaction)
        try:
            await view.refresh()
        finally:
            _CURRENT_INTERACTION.reset(token)

        interaction.response.edit_message.assert_awaited_once_with(view=view)
        view._message.edit.assert_not_called()

    async def test_already_deferred_ephemeral_falls_through_to_webhook(self):
        """The response slot was already consumed (a queued interaction the
        auto-defer timer beat) -- the fast path is disqualified and the edit
        falls through to the webhook handle, exactly as a disqualified
        non-ephemeral acting view does.
        """
        view = self._make_ephemeral_view()
        interaction = self._make_acting_interaction(is_done=True)

        token = _CURRENT_INTERACTION.set(interaction)
        try:
            await view.refresh()
        finally:
            _CURRENT_INTERACTION.reset(token)

        interaction.response.edit_message.assert_not_called()
        view._message.edit.assert_awaited_once_with(view=view)

    async def test_non_acting_ephemeral_edits_without_a_defer(self):
        """Background ephemeral refreshes (no bound interaction) edit straight
        through the webhook handle -- no deferred ack fires because there is
        nothing to acknowledge, and there is no acting click for the fast
        path to piggyback onto.
        """
        view = self._make_ephemeral_view()

        assert _CURRENT_INTERACTION.get() is None
        await view.refresh()

        view._message.edit.assert_awaited_once_with(view=view)


class TestEditTimeout:
    """``edit_timeout`` bounds every live-view and teardown edit through
    ``_bounded``.

    discord.py issues HTTP edits with no total timeout, so a connection
    that stalls without a response would pin the awaiting code -- and, on
    the interaction-locked refresh/navigation paths, the view itself. The
    ceiling cancels the stalled request and the view recovers on the next
    interaction. ``edit_timeout = None`` restores unbounded awaits.
    """

    def _make_view(self, **class_attrs):
        class _V(StatefulLayoutView):
            def build_ui(self):
                self.clear_items()
                self.add_item(ActionRow(StatefulButton(label="Fire", custom_id="a")))

        for name, value in class_attrs.items():
            setattr(_V, name, value)
        view = _V(interaction=_make_interaction())
        view.build_ui()
        view._message = MagicMock()
        view._message.id = 555
        view._message.edit = AsyncMock()
        view._last_tree_digest = None
        return view

    async def test_default_timeout_is_sixty(self):
        view = self._make_view()
        assert view.edit_timeout == 60.0

    async def test_bounded_awaits_directly_when_disabled(self):
        view = self._make_view(edit_timeout=None)

        async def quick():
            return "done"

        assert await view._bounded(quick()) == "done"

    async def test_bounded_returns_result_within_ceiling(self):
        view = self._make_view(edit_timeout=5.0)

        async def quick():
            return "done"

        assert await view._bounded(quick()) == "done"

    async def test_bounded_raises_on_stall(self):
        view = self._make_view(edit_timeout=0.05)

        async def stall():
            await asyncio.sleep(60)

        with pytest.raises(asyncio.TimeoutError):
            await view._bounded(stall())

    async def test_refresh_stall_logs_and_invalidates_digest(self, caplog):
        """A stalled channel edit is cancelled at ``edit_timeout``; refresh
        logs a warning, invalidates the digest so the next refresh re-ships,
        and returns instead of hanging on the socket.
        """
        view = self._make_view(edit_timeout=0.05)

        async def stall(*args, **kwargs):
            await asyncio.sleep(60)

        view._message.edit = AsyncMock(side_effect=stall)
        # Seed a non-matching, non-None digest so refresh skips neither the
        # short-circuit-on-match nor the edit; after the stall the timeout
        # branch should have reset it to None.
        view._last_tree_digest = view._compute_tree_digest() + 1

        assert _CURRENT_INTERACTION.get() is None  # background path, no fast path
        before = time.monotonic()
        with caplog.at_level(logging.WARNING, logger="cascadeui.views.base"):
            await view.refresh()
        elapsed = time.monotonic() - before

        assert elapsed < 2.0  # bounded by edit_timeout, not the 60s stall
        assert view._last_tree_digest is None
        assert any("stalled" in r.getMessage() for r in caplog.records)


class TestDisplayLayoutView:
    """``DisplayLayoutView`` renders a pre-built container without
    requiring a subclass. Used for one-shot ephemeral cards (stats,
    leaderboards, confirmations) where there's no view-local state to
    manage.
    """

    def test_is_subclass_of_stateful_layout_view(self):
        assert issubclass(DisplayLayoutView, StatefulLayoutView)

    def test_defaults_differ_from_stateful_layout_view(self):
        assert DisplayLayoutView.owner_only is False
        assert DisplayLayoutView.state_scope is None

    def test_container_is_rendered(self):
        interaction = _make_interaction()
        body = TextDisplay("hello")
        view = DisplayLayoutView(interaction=interaction, container=body)

        assert body in view.children

    def test_build_ui_clears_and_re_adds(self):
        interaction = _make_interaction()
        body = TextDisplay("hello")
        view = DisplayLayoutView(interaction=interaction, container=body)

        view.build_ui()

        assert list(view.children) == [body]

    def test_container_kwarg_is_required(self):
        interaction = _make_interaction()
        with pytest.raises(TypeError, match="container"):
            DisplayLayoutView(interaction=interaction)


class TestRateLimitSchedulesARetry:
    """A rate-limit arms the backoff window; something must still ship the edit.

    Every 429 seam stamps the window and returns, so the edit in flight is
    dropped. Most views live through that -- their next state notification
    renders whatever is current. An armed ephemeral view does not: the armed
    flag is set before the arming edit and then drops every notification that
    could repair it, so a 429 there left a frozen panel with no refresh button
    and nothing left to give it one.
    """

    def _make_view(self, cooldown=None):
        class _V(StatefulLayoutView):
            def build_ui(self):
                self.clear_items()
                self.add_item(ActionRow(StatefulButton(label="Fire", custom_id="a")))

        if cooldown is not None:
            _V.refresh_cooldown_ms = cooldown
        view = _V(interaction=_make_interaction())
        view.build_ui()
        view._message = MagicMock()
        view._message.id = 555
        view._last_tree_digest = None
        return view

    async def test_a_rate_limited_refresh_queues_a_retry(self):
        view = self._make_view()
        view._message.edit = AsyncMock(side_effect=_FakeRateLimit(retry_after=0.2))

        await view.refresh()  # 429 -> swallowed, window armed

        assert view._deferred_refresh_task is not None
        view._deferred_refresh_task.cancel()

    async def test_the_queued_retry_ships_the_edit_at_the_boundary(self):
        view = self._make_view()
        # 429 once, then succeed -- the retry is what lands the edit.
        view._message.edit = AsyncMock(side_effect=[_FakeRateLimit(retry_after=0.15), None])

        await view.refresh()
        assert view._message.edit.await_count == 1  # only the failed attempt

        await until(lambda: view._message.edit.await_count == 2)  # the retry shipped it

    async def test_a_rate_limited_arming_edit_still_reaches_the_message(self):
        """The case with no second chance: the armed flag freezes every other
        repair, so if this edit does not land the panel is dead at the token
        expiry. The queued retry is the only thing standing between a 429 here
        and a user holding a frozen view with no refresh button.
        """

        class _Eph(StatefulLayoutView):
            auto_refresh_ephemeral = True

            def build_ui(self):
                self.clear_items()
                self.add_item(ActionRow(StatefulButton(label="Normal", custom_id="n")))

        view = _Eph(interaction=_make_interaction())
        view.build_ui()
        view._message = MagicMock()
        view._message.id = 555
        view._ephemeral = True
        view._last_tree_digest = None
        view._message.edit = AsyncMock(side_effect=[_FakeRateLimit(retry_after=0.15), None])

        await view._arm_refresh_button()
        assert view._refresh_armed is True
        assert view._message.edit.await_count == 1  # the arming edit 429'd

        # The retry shipped, and it shipped the ARMED tree (no rebuild).
        await until(lambda: view._message.edit.await_count == 2)
        assert view._refresh_armed is True

    async def test_a_retry_that_is_rate_limited_again_schedules_a_successor(self):
        """One try is not enough: the view must recover when the block lifts."""
        view = self._make_view()
        view._message.edit = AsyncMock(
            side_effect=[
                _FakeRateLimit(retry_after=0.1),
                _FakeRateLimit(retry_after=0.1),
                None,
            ]
        )

        await view.refresh()
        await asyncio.sleep(0.5)

        assert view._message.edit.await_count == 3  # failed, failed, landed
        assert view._deferred_refresh_task is None  # settled, nothing left pending

    @staticmethod
    def _final_card(view):
        view.clear_items()
        view.add_item(TextDisplay("Final score"))
        view.add_item(ActionRow(StatefulButton(label="Fire", custom_id="a")))

    @staticmethod
    def _shipped(edit):
        shipped = edit.await_args.kwargs["view"]
        return (
            [c.content for c in shipped.walk_children() if isinstance(c, TextDisplay)],
            [c.disabled for c in shipped.walk_children() if isinstance(c, Button)],
        )

    async def test_a_closed_view_renders_when_a_rate_limit_window_ends(self):
        """A render asked of a closed view (an on_timeout() override's final
        card) inside a backoff window was dropped: the deferred render reads
        state, which a closed view no longer follows."""
        view = self._make_view()
        view._message.edit = AsyncMock()
        await view.exit(delete_message=False)
        self._final_card(view)
        view._message.edit = AsyncMock()
        view._ratelimit_not_before = time.monotonic() + 0.15

        assert await view.refresh() is RenderOutcome.DEFERRED
        await until(lambda: view._message.edit.await_count == 1)

        assert self._shipped(view._message.edit) == (["Final score"], [True])

    async def test_a_closed_views_render_discord_refuses_is_dropped_with_a_warning(self, caplog):
        """Replaying it would resend files discord.py closed after the refused
        request, and no teardown would end the retries."""
        view = self._make_view()
        view._message.edit = AsyncMock()
        await view.exit(delete_message=False)
        self._final_card(view)
        view._message.edit = AsyncMock(side_effect=_FakeRateLimit(retry_after=0.05))

        with caplog.at_level(logging.WARNING, logger="cascadeui"):
            assert await view.refresh() is RenderOutcome.DEFERRED
            await asyncio.sleep(0.15)

        assert view._message.edit.await_count == 1
        assert any("rate-limited the last render" in r.getMessage() for r in caplog.records)

    async def test_a_closed_view_renders_when_its_cooldown_ends(self):
        view = self._make_view(cooldown=150)
        view._message.edit = AsyncMock()
        await view.refresh()  # stamps the cooldown window
        await view.exit(delete_message=False)
        self._final_card(view)
        view._message.edit = AsyncMock()

        assert await view.refresh() is RenderOutcome.DEFERRED
        await until(lambda: view._message.edit.await_count == 1)

        assert self._shipped(view._message.edit) == (["Final score"], [True])


class TestSendPipelineAckBackstop:
    """A direct slash-command send arms an ack backstop over its pre-send I/O
    (on_load, on_pre_send, enforcement, seeding), which has no _scheduled_task
    timer of its own.
    """

    async def test_slow_on_load_defers_via_send_timer(self):
        """A slow on_load during send() triggers the send-scoped timer, acking
        the interaction before Stage 5 would otherwise blow the 3s wall.
        """

        class _SlowLoad(RenderableLayoutView):
            async def on_load(self):
                await asyncio.sleep(0.15)

        interaction = _make_interaction(is_done=False)
        interaction.response.defer = AsyncMock()
        view = _SlowLoad(interaction=interaction)
        view.auto_defer_delay = 0.05  # fires during the 0.15s on_load

        await view.send()

        interaction.response.defer.assert_called()

    async def test_fast_send_does_not_defer(self):
        """A fast send cancels the timer before it fires, so the send itself is
        the ack (no premature defer).
        """
        interaction = _make_interaction(is_done=False)
        interaction.response.defer = AsyncMock()
        view = RenderableLayoutView(interaction=interaction)
        view.auto_defer_delay = 10  # would never fire during a fast send

        await view.send()

        interaction.response.defer.assert_not_called()

    async def test_veto_cancels_send_timer(self):
        """An on_pre_send veto cancels the send-scoped timer, so no phantom
        defer fires after send() returns None.
        """

        class _Veto(RenderableLayoutView):
            async def on_pre_send(self, interaction):
                return False

        interaction = _make_interaction(is_done=False)
        interaction.response.defer = AsyncMock()
        view = _Veto(interaction=interaction)
        view.auto_defer_delay = 0.05

        result = await view.send()

        assert result is None
        await asyncio.sleep(0.1)  # a leaked timer would fire by now
        interaction.response.defer.assert_not_called()

    async def test_on_pre_send_runs_on_open_slot(self):
        """The send-scoped timer arms AFTER on_pre_send, so a slow veto hook
        keeps its documented open response slot instead of being deferred.
        """
        slot_open = {}

        class _Gate(RenderableLayoutView):
            async def on_pre_send(self, interaction):
                await asyncio.sleep(0.15)  # slow veto
                slot_open["value"] = not interaction.response.is_done()
                return False

        interaction = _make_interaction(is_done=False)
        interaction.response.defer = AsyncMock()
        view = _Gate(interaction=interaction)
        view.auto_defer_delay = 0.05  # would fire mid-veto if armed before it

        await view.send()

        assert slot_open["value"] is True

    async def test_stage5_40060_ships_via_followup(self):
        """A server-side 40060 on send_message (a cancelled send-defer landing
        in the ack race) ships via followup instead of rolling the send back.
        """
        interaction = _make_interaction(is_done=False)
        err = discord.HTTPException(MagicMock(status=400), {"code": 40060, "message": "x"})
        interaction.response.send_message = AsyncMock(side_effect=err)
        interaction.followup.send = AsyncMock(return_value=MagicMock())
        view = RenderableLayoutView(interaction=interaction)

        await view.send()

        interaction.followup.send.assert_called_once()


class TestReopenInstanceLimit:
    """A reopen swap must not self-replace a limited view: the dying instance
    is excluded from the replacement's instance-limit count.
    """

    async def test_replacing_view_id_excluded_from_count(self):
        from cascadeui.state.singleton import get_store

        class _Limited(StatefulLayoutView):
            instance_limit = 1
            instance_policy = "replace"

        store = get_store()
        old = _Limited(interaction=_make_interaction(user_id=1, guild_id=1))
        store._register_view(old)
        old.exit = AsyncMock()
        try:
            new = _Limited(interaction=_make_interaction(user_id=1, guild_id=1))
            new._replacing_view_id = old.id

            await new._enforce_instance_limit()

            old.exit.assert_not_called()  # excluded from count -> not replaced
        finally:
            store._unregister_view(old.id)


class TestReactiveRenderDurationWarning:
    """A slow overridden on_state_changed warns once per class at the reactive
    rebuild seam -- the always-on budget mirror of _run_on_load. The budget is an
    ack deadline, so the timing only runs while an interaction is in flight.
    """

    async def test_slow_reactive_rebuild_warns_once(self, caplog):
        from cascadeui.state.store import _CURRENT_INTERACTION
        from cascadeui.views.base import _slow_render_warned

        class _SlowRender(StatefulLayoutView):
            async def on_state_changed(self, state):
                await asyncio.sleep(0.05)

        _slow_render_warned.discard("_SlowRender")

        view = _SlowRender(interaction=_make_interaction())
        view.auto_defer_delay = 0.01  # 0.05s rebuild overruns the budget

        token = _CURRENT_INTERACTION.set(_make_interaction())
        try:
            with caplog.at_level(logging.WARNING, logger="cascadeui.views.base"):
                await view._run_state_changed({})
                await view._run_state_changed({})  # second call must not re-warn
        finally:
            _CURRENT_INTERACTION.reset(token)

        warnings = [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING and "on_state_changed" in r.getMessage()
        ]
        assert len(warnings) == 1

    async def test_no_warning_when_no_interaction_is_in_flight(self, caplog):
        """A timeout-driven rebuild races no ack, so the budget does not apply.

        Three views timing out at once each fire a rebuild plus a message edit;
        the elapsed time there is the edit, not the rebuild, and warning on it
        would train operators to ignore the diagnostic.
        """
        from cascadeui.state.store import _CURRENT_INTERACTION
        from cascadeui.views.base import _slow_render_warned

        class _SlowBackgroundRender(StatefulLayoutView):
            async def on_state_changed(self, state):
                await asyncio.sleep(0.05)

        _slow_render_warned.discard("_SlowBackgroundRender")

        view = _SlowBackgroundRender(interaction=_make_interaction())
        view.auto_defer_delay = 0.01

        assert _CURRENT_INTERACTION.get() is None
        with caplog.at_level(logging.WARNING, logger="cascadeui.views.base"):
            await view._run_state_changed({})

        assert not any("on_state_changed() took" in r.getMessage() for r in caplog.records)

    async def test_default_on_state_changed_not_timed(self, caplog):
        """A view that does not override on_state_changed is not warned (the
        default path is build_ui + edit, which this coarse budget cannot isolate).
        """
        from cascadeui.views.base import _slow_render_warned

        _slow_render_warned.clear()
        view = StatefulLayoutView(interaction=_make_interaction())
        view.refresh = AsyncMock()  # the default on_state_changed calls refresh()

        with caplog.at_level(logging.WARNING, logger="cascadeui.views.base"):
            await view._run_state_changed({})

        assert not any("on_state_changed() took" in r.getMessage() for r in caplog.records)


# // ========================================( allowed_mentions Seam )======================================== // #


class TestAllowedMentions:
    """The mention-rules seam on send() and refresh().

    Three tiers: class attribute, explicit argument, and neither (which
    defers to the client-level rules discord.py already applies).
    """

    @staticmethod
    def _context():
        ctx = MagicMock()
        ctx.author.id = 100
        ctx.guild.id = 200
        sent = MagicMock()
        sent.id = 999
        ctx.send = AsyncMock(return_value=sent)
        ctx.channel.fetch_message = AsyncMock(return_value=sent)
        return ctx

    async def test_explicit_argument_reaches_the_send_call(self):
        ctx = self._context()
        view = RenderableLayoutView(context=ctx, user_id=100, guild_id=200)
        rules = discord.AllowedMentions(everyone=False, users=True, roles=False)

        await view.send(allowed_mentions=rules)

        assert ctx.send.await_args.kwargs["allowed_mentions"] is rules

    async def test_class_attribute_is_the_standing_default(self):
        class _Quiet(RenderableLayoutView):
            allowed_mentions = discord.AllowedMentions.none()

        ctx = self._context()
        view = _Quiet(context=ctx, user_id=100, guild_id=200)

        await view.send()

        assert ctx.send.await_args.kwargs["allowed_mentions"].to_dict() == {"parse": []}

    async def test_explicit_argument_overrides_the_class_attribute(self):
        class _Quiet(RenderableLayoutView):
            allowed_mentions = discord.AllowedMentions.none()

        ctx = self._context()
        view = _Quiet(context=ctx, user_id=100, guild_id=200)
        rules = discord.AllowedMentions(everyone=False, users=True, roles=False)

        await view.send(allowed_mentions=rules)

        assert ctx.send.await_args.kwargs["allowed_mentions"] is rules

    async def test_omitted_on_both_tiers_sends_no_key(self):
        """Absent means "defer to the client", not "allow everything"."""
        ctx = self._context()
        view = RenderableLayoutView(context=ctx, user_id=100, guild_id=200)

        await view.send()

        assert "allowed_mentions" not in ctx.send.await_args.kwargs

    async def test_refresh_channel_path_carries_the_rules(self):
        """``Message.edit`` only forwards the client default when ``content``
        is supplied, and a V2 view never supplies content, so the rules
        must be injected explicitly or this path ships without them.
        """

        class _Quiet(RenderableLayoutView):
            allowed_mentions = discord.AllowedMentions.none()

        view = _Quiet(interaction=_make_interaction())
        view._message = MagicMock()
        view._message.id = 999
        view._message.edit = AsyncMock()
        view._webhook_message = None
        view._last_tree_digest = None

        await view.refresh()

        assert view._message.edit.await_args.kwargs["allowed_mentions"].to_dict() == {"parse": []}

    async def test_leaderboard_suppresses_its_own_mentions_by_default(self):
        """format_name renders ``<@id>`` for entries without a display_name,
        so the pattern owns a suppression default its siblings do not need.
        """
        from cascadeui.views.patterns.leaderboard import LeaderboardLayoutView

        assert LeaderboardLayoutView.allowed_mentions.to_dict() == {"parse": []}
        assert StatefulLayoutView.allowed_mentions is None

    def test_non_allowed_mentions_value_rejected_at_class_definition(self):
        with pytest.raises(TypeError, match="allowed_mentions must be"):

            class _Bad(StatefulLayoutView):
                allowed_mentions = "users"

    @pytest.mark.parametrize("slot_open", [True, False])
    async def test_navigation_edit_carries_the_destination_rules(self, slot_open):
        """push()/pop() edit through their own endpoints rather than refresh(),
        so the destination's rules must ride those calls too. A menu pushing a
        leaderboard would otherwise ping every ranked player on the edit.
        """

        class _Quiet(RenderableLayoutView):
            allowed_mentions = discord.AllowedMentions.none()

        source = RenderableLayoutView(interaction=_make_interaction())
        message = MagicMock()
        message.id = 999
        message.edit = AsyncMock()
        source._message = message

        interaction = _make_interaction()
        interaction.message = message
        interaction.response.edit_message = AsyncMock()
        interaction.edit_original_response = AsyncMock()
        if not slot_open:
            interaction.response.is_done = MagicMock(return_value=True)

        await source.push(_Quiet(interaction=_make_interaction()), interaction)

        edited = (
            interaction.response.edit_message if slot_open else interaction.edit_original_response
        )
        assert edited.await_args.kwargs["allowed_mentions"].to_dict() == {"parse": []}

    async def test_navigation_leaves_the_digest_short_circuit_intact(self):
        """The rules go onto a copy, not onto ``edit_kwargs``: a non-empty
        kwargs dict would disable refresh()'s render-hash skip on the two
        navigation paths that route through refresh().
        """

        class _Quiet(RenderableLayoutView):
            allowed_mentions = discord.AllowedMentions.none()

        source = RenderableLayoutView(interaction=_make_interaction())
        message = MagicMock()
        message.id = 999
        message.edit = AsyncMock()
        source._message = message

        destination = _Quiet(interaction=_make_interaction())
        # Foreign interaction: message ids differ, so _apply_navigation_edit
        # takes the refresh() branch rather than either direct edit.
        interaction = _make_interaction()
        interaction.message = MagicMock()
        interaction.message.id = 111

        destination.refresh = AsyncMock()
        await source.push(destination, interaction)

        assert destination.refresh.await_args.kwargs == {}


# // ========================================( Portable Edit Kwargs )======================================== // #


class TestPortableEditKwargs:
    """refresh() rejects kwargs only some of its three endpoints accept.

    Which endpoint runs depends on runtime conditions the caller cannot
    see, so a non-portable kwarg would work until the ack race flipped.
    """

    async def test_portable_kwargs_pass_through(self):
        view = RenderableLayoutView(interaction=_make_interaction())
        view._message = MagicMock()
        view._message.id = 999
        view._message.edit = AsyncMock()
        view._webhook_message = None
        view._last_tree_digest = None

        await view.refresh(content="ok")

        assert view._message.edit.await_args.kwargs["content"] == "ok"

    @pytest.mark.parametrize("stray", ["suppress", "suppress_embeds", "delete_after"])
    async def test_non_portable_kwarg_rejected(self, stray):
        view = RenderableLayoutView(interaction=_make_interaction())
        view._message = MagicMock()
        view._message.edit = AsyncMock()

        with pytest.raises(TypeError, match="cannot forward"):
            await view.refresh(**{stray: True})

        assert view._message.edit.await_count == 0

    def test_portable_set_matches_the_three_endpoints(self):
        """Canary: the allowlist is derived from discord.py's own signatures,
        so an upstream signature change surfaces here rather than as a
        runtime TypeError on one endpoint.
        """
        import inspect

        from discord.webhook import async_ as webhook_module

        def names(fn):
            return set(inspect.signature(fn).parameters) - {"self"}

        common = (
            names(discord.InteractionResponse.edit_message)
            & names(webhook_module.WebhookMessage.edit)
            & names(discord.Message.edit)
        )
        # ``view`` is library-owned and never accepted from a caller.
        assert _StatefulMixin._PORTABLE_EDIT_KWARGS == common - {"view"}


class TestNavRebuildValidation:
    """``nav_rebuild`` must not be a value that binds as a method.

    A bare function or a ``functools.partial`` in a class body receives
    ``self`` as its first argument instead of the destination view.
    ``callable()`` cannot tell those apart from a ``staticmethod``, since
    all three are callable; descriptor-ness is what decides it.
    """

    def test_bare_lambda_rejected(self):
        with pytest.raises(TypeError, match="binds as a method"):

            class _Bad(StatefulLayoutView):
                nav_rebuild = lambda v: {"content": "x"}  # noqa: E731

    def test_partial_rejected_on_every_supported_python(self):
        """``functools.partial`` became a descriptor in 3.14, so it binds on
        3.14 and not on 3.10-3.13. Rejecting it only where it binds would let
        the same class pass on one supported Python and fail on another, so
        it is rejected everywhere and ``staticmethod`` is the portable answer.
        """
        import functools

        with pytest.raises(TypeError, match="binds as a method"):

            class _Bad(StatefulLayoutView):
                nav_rebuild = functools.partial(lambda v: {"content": "x"})

    def test_staticmethod_accepted(self):
        class _Good(StatefulLayoutView):
            nav_rebuild = staticmethod(lambda v: {"content": "x"})

        assert _Good.nav_rebuild is not None

    def test_plain_callable_object_accepted(self):
        """An object with ``__call__`` but no ``__get__`` does not bind."""

        class _Rebuilder:
            def __call__(self, view):
                return {"content": "x"}

        class _Good(StatefulLayoutView):
            nav_rebuild = _Rebuilder()

        assert _Good.nav_rebuild is not None

    def test_non_callable_rejected(self):
        with pytest.raises(TypeError, match="must be callable or None"):

            class _Bad(StatefulLayoutView):
                nav_rebuild = "oops"


class TestStabilizedCustomIdFitsTheCap:
    """The id stabilizer folds an over-cap anchor instead of raising.

    It runs inside every ``build_ui``, so a rebuild must not fail on a
    label the user is allowed to set, unlike the builders, which reject
    at construction where the caller can act on it.
    """

    def test_short_anchor_passes_through(self):
        anchor = "a" * 50
        assert _StatefulMixin._fit_custom_id(anchor) == anchor

    def test_long_anchor_is_folded_to_the_cap(self):
        folded = _StatefulMixin._fit_custom_id("x" * 250)
        assert len(folded) == 100
        assert "~" in folded

    def test_fold_is_deterministic(self):
        anchor = "y" * 250
        assert _StatefulMixin._fit_custom_id(anchor) == _StatefulMixin._fit_custom_id(anchor)

    def test_distinct_anchors_fold_distinctly(self):
        a = _StatefulMixin._fit_custom_id("z" * 250)
        b = _StatefulMixin._fit_custom_id("z" * 250 + "q")
        assert a != b

    async def test_a_long_label_does_not_break_build_ui(self):
        """End to end: an 80-character label (Discord's own cap) on a
        deeply-qualified callback must still produce a shippable id.
        """

        async def a_callback_with_a_long_qualified_name(interaction):
            pass

        class _Long(RenderableLayoutView):
            def build_ui(self):
                self.clear_items()
                self.add_item(
                    ActionRow(
                        StatefulButton(
                            label="L" * 80,
                            callback=a_callback_with_a_long_qualified_name,
                        )
                    )
                )

        view = _Long(interaction=_make_interaction())
        view.build_ui()

        ids = [c.custom_id for c in view.walk_children() if getattr(c, "custom_id", None)]
        assert ids and all(len(i) <= 100 for i in ids)


class TestClientAllowedMentionsFallback:
    """The third resolution tier: the bot's own client-level rules.

    ``Message.edit`` forwards the client default only when ``content`` is
    supplied, and a V2 view never supplies content, so without this tier
    a bot that configured suppression globally would get it on send and
    lose it on any refresh that took the channel endpoint.
    """

    @staticmethod
    def _client(rules):
        client = MagicMock(spec=discord.Client)
        client.allowed_mentions = rules
        return client

    async def test_client_rules_reach_the_channel_edit(self):
        interaction = _make_interaction()
        interaction.client = self._client(discord.AllowedMentions.none())
        view = RenderableLayoutView(interaction=interaction)
        view._message = MagicMock()
        view._message.id = 999
        view._message.edit = AsyncMock()
        view._webhook_message = None
        view._last_tree_digest = None

        await view.refresh()

        assert view._message.edit.await_args.kwargs["allowed_mentions"].to_dict() == {"parse": []}

    async def test_no_client_rules_sends_no_key(self):
        interaction = _make_interaction()
        interaction.client = self._client(None)
        view = RenderableLayoutView(interaction=interaction)
        view._message = MagicMock()
        view._message.id = 999
        view._message.edit = AsyncMock()
        view._webhook_message = None
        view._last_tree_digest = None

        await view.refresh()

        assert "allowed_mentions" not in view._message.edit.await_args.kwargs

    def test_a_mock_client_attribute_is_not_put_on_the_wire(self):
        """A bare Mock auto-creates any attribute, so the resolution is
        type-checked rather than truthiness-checked.
        """
        view = RenderableLayoutView(interaction=_make_interaction())
        view.interaction.client = MagicMock()

        assert view._resolve_allowed_mentions(None) is None

    def test_class_attribute_wins_over_client_rules(self):
        class _Quiet(RenderableLayoutView):
            allowed_mentions = discord.AllowedMentions(everyone=False, users=True, roles=False)

        interaction = _make_interaction()
        interaction.client = self._client(discord.AllowedMentions.none())
        view = _Quiet(interaction=interaction)

        assert view._resolve_allowed_mentions(None) is _Quiet.allowed_mentions


class TestReloadRespectsArmedRefresh:
    """An armed ephemeral view must not rebuild over its refresh button.

    Between the arming edit and the token cliff, re-running ``on_load``
    would replace the button, and the armed flag then drops every
    notification that could put it back.
    """

    async def test_reload_skips_on_load_while_armed(self):
        calls = []

        class _Loader(RenderableLayoutView):
            async def on_load(self):
                calls.append("on_load")

        view = _Loader(interaction=_make_interaction())
        view._message = MagicMock()
        view._message.id = 999
        view._message.edit = AsyncMock()
        view._webhook_message = None
        view._refresh_armed = True

        await view.reload()

        assert calls == []
        assert view._message.edit.await_count == 1

    async def test_reload_runs_on_load_when_not_armed(self):
        calls = []

        class _Loader(RenderableLayoutView):
            async def on_load(self):
                calls.append("on_load")

        view = _Loader(interaction=_make_interaction())
        view._message = MagicMock()
        view._message.id = 999
        view._message.edit = AsyncMock()
        view._webhook_message = None

        await view.reload()

        assert calls == ["on_load"]

    @pytest.mark.parametrize("method", ["reload", "load"])
    async def test_arming_waits_for_a_load_in_flight(self, method):
        """The arming edit did not take the reload turn, so an on_load already
        running rebuilt the tree over the refresh button after it shipped."""
        gate = asyncio.Event()

        class _Loader(RenderableLayoutView):
            loads = 0

            async def on_load(self):
                self.loads += 1
                if self.loads == 1:
                    return
                await gate.wait()
                self.clear_items()
                self.add_item(TextDisplay("reloaded"))

        view = _Loader(interaction=_make_interaction())
        await view.send(ephemeral=True)
        # Engaged, as a timeout past 900s derives; the timer arms no other view.
        view._refresh_handoff_resolved = True
        shipped = []

        async def edit(**kwargs):
            shipped.append(kwargs["view"].to_components())

        view._message.edit = AsyncMock(side_effect=edit)
        loading = asyncio.create_task(getattr(view, method)())
        await asyncio.sleep(0)
        arming = asyncio.create_task(view._arm_refresh_button())
        await asyncio.sleep(0)
        gate.set()
        await asyncio.wait_for(asyncio.gather(loading, arming), timeout=2)

        assert any(c.get("type") == 1 for c in shipped[-1])
        assert any(isinstance(item, discord.ui.Button) for item in view.walk_children())


class TestRateLimitedSiblingOnSendRefetch:
    """``RateLimited`` is a sibling of ``HTTPException``, not a subclass.

    Uncaught on the post-send re-fetch it would escape a send that already
    succeeded, leaving the view with no ``_message``: no cleanup listener,
    no parent attach, and a failure reported for a live message.
    """

    def test_rate_limited_is_not_an_http_exception(self):
        assert not issubclass(discord.RateLimited, discord.HTTPException)

    async def test_rate_limited_refetch_keeps_the_sent_message(self):
        ctx = MagicMock()
        ctx.author.id = 100
        ctx.guild.id = 200
        sent = MagicMock()
        sent.id = 999
        ctx.send = AsyncMock(return_value=sent)
        ctx.channel.fetch_message = AsyncMock(side_effect=discord.RateLimited(5.0))

        view = RenderableLayoutView(context=ctx, user_id=100, guild_id=200)
        result = await view.send()

        assert result is sent
        assert view._message is sent


class TestFreezeEditsCarryMentionRules:
    """A teardown freeze re-ships the same mention-bearing tree.

    ``on_timeout``, ``exit()``'s V2 branch, the empty-stack back clear,
    and the reopen fallback all hand Discord the tree ``refresh()`` hands
    it, so they owe the same rules. Otherwise the last edit a view ever
    makes is the one edit that ignores its own ``allowed_mentions``.
    """

    @staticmethod
    def _quiet_view():
        class _Quiet(RenderableLayoutView):
            allowed_mentions = discord.AllowedMentions.none()

            def build_ui(self):
                self.clear_items()
                self.add_item(
                    ActionRow(StatefulButton(label="Go", callback=AsyncMock(), custom_id="g"))
                )

        view = _Quiet(interaction=_make_interaction())
        view.build_ui()
        message = MagicMock()
        message.edit = AsyncMock()
        message.delete = AsyncMock()
        view._message = message
        return view, message

    async def test_on_timeout_freeze_carries_the_rules(self):
        view, message = self._quiet_view()

        await view.on_timeout()

        assert message.edit.await_args.kwargs["allowed_mentions"].to_dict() == {"parse": []}

    async def test_exit_freeze_carries_the_rules(self):
        view, message = self._quiet_view()

        await view.exit(delete_message=False)

        assert message.edit.await_args.kwargs["allowed_mentions"].to_dict() == {"parse": []}

    async def test_a_view_with_no_rules_ships_no_key(self):
        view = RenderableLayoutView(interaction=_make_interaction())
        view.interaction.client = MagicMock(spec=discord.Client)
        view.interaction.client.allowed_mentions = None
        view.add_item(ActionRow(StatefulButton(label="Go", callback=AsyncMock(), custom_id="g")))
        message = MagicMock()
        message.edit = AsyncMock()
        view._message = message

        await view.on_timeout()

        assert "allowed_mentions" not in message.edit.await_args.kwargs


class TestBuildUiInSyncInit:
    """Patterns that compose in ``__init__`` refuse an async ``build_ui``.

    ``DisplayLayoutView``, ``MenuLayoutView`` and ``RolesLayoutView`` build
    their tree in ``__init__``, a synchronous frame that cannot resolve a
    coroutine. An async override there ran nothing: construction
    succeeded, the tree stayed empty, and the only signal was a "never
    awaited" warning most bots never surface. The mistake surfaced later
    as a placement error naming "no top-level components", which is the
    symptom and points nowhere near the cause.
    """

    def _menu(self, build_fn):
        from cascadeui.views.patterns.menu import MenuLayoutView

        cls = type("_M", (MenuLayoutView,), {"build_ui": build_fn})
        return lambda: cls(interaction=_make_interaction(), user_id=1, categories=[])

    async def test_async_def_is_refused_at_construction(self):
        async def build(self):
            self.add_item(TextDisplay("x"))

        with pytest.raises(TypeError, match="cannot await"):
            self._menu(build)()

    async def test_async_dunder_call_is_refused(self):
        """The shape ``iscoroutinefunction`` answers False for."""

        class AsyncCallable:
            async def __call__(self, view):
                view.add_item(TextDisplay("x"))

        with pytest.raises(TypeError, match="cannot await"):
            self._menu(AsyncCallable())()

    async def test_partial_around_a_coroutine_function_is_refused(self):
        import functools

        class AsyncCallable:
            async def __call__(self, view):
                view.add_item(TextDisplay("x"))

        with pytest.raises(TypeError, match="cannot await"):
            self._menu(functools.partial(AsyncCallable()))()

    async def test_message_names_the_replacement_hook(self):
        async def build(self):
            self.add_item(TextDisplay("x"))

        with pytest.raises(TypeError, match=r"on_load\(\)"):
            self._menu(build)()

    async def test_refusal_leaves_no_never_awaited_warning(self):
        """The error is the whole report; a trailing warning naming the
        same function reads as a second, separate fault."""
        import gc
        import warnings

        class AsyncCallable:
            async def __call__(self, view):
                view.add_item(TextDisplay("x"))

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with pytest.raises(TypeError):
                self._menu(AsyncCallable())()
            gc.collect()

        assert not [w for w in caught if "never awaited" in str(w.message)]

    async def test_synchronous_build_still_constructs(self):
        from cascadeui.views.patterns.menu import MenuLayoutView

        view = MenuLayoutView(interaction=_make_interaction(), user_id=1, categories=[])

        assert view.children


class TestViewHooksAcceptPlainDef:
    """Every ``on_*`` override runs whether or not it is ``async def``.

    Each of these was a bare ``await self.on_x(...)``, so a synchronous
    override ran, returned, and then failed on the library awaiting its
    return value -- surfacing as an error against the user's own callback,
    or, for ``on_load``, as a view that would not send at all. Nothing in
    the suite declared a plain-``def`` override before this.
    """

    async def test_a_view_whose_every_hook_is_sync_sends(self):
        calls = []

        class SyncHooks(StatefulLayoutView):
            owner_only = False

            def on_pre_send(self, interaction):
                calls.append("on_pre_send")
                return True

            def on_load(self):
                calls.append("on_load")
                self.build_ui()

            def on_state_changed(self, state):
                calls.append("on_state_changed")

            def build_ui(self):
                self.clear_items()
                self.add_item(TextDisplay("built"))

        view = SyncHooks(interaction=_make_interaction(user_id=1), user_id=1)

        assert await view.send() is not None
        assert view.children, "a sync on_load must still have composed the tree"
        assert {"on_pre_send", "on_load"} <= set(calls)

    async def test_sync_seed_initial_state_runs(self):
        """The seed hook ran through a bare await while its siblings did not.

        Its siblings route through ``await_maybe``; this one was a bare
        ``await``, so a plain-``def`` override raised ``'NoneType' object
        can't be awaited`` from inside ``send()`` rather than running.
        """
        seeded = []

        class SyncSeed(StatefulLayoutView):
            owner_only = False

            def seed_initial_state(self, state):
                seeded.append(state is not None)

            def build_ui(self):
                self.clear_items()
                self.add_item(TextDisplay("seeded"))

        view = SyncSeed(interaction=_make_interaction(user_id=1), user_id=1)
        view.build_ui()

        assert await view.send() is not None
        assert seeded == [True]

    async def test_sync_on_state_changed_runs_on_notification(self):
        calls = []

        class SyncNotify(StatefulLayoutView):
            owner_only = False

            def on_load(self):
                self.build_ui()

            def on_state_changed(self, state):
                calls.append(state)

            def build_ui(self):
                self.clear_items()
                self.add_item(TextDisplay("built"))

        view = SyncNotify(interaction=_make_interaction(user_id=1), user_id=1)
        await view.send()

        await view._handle_state_notification(view.state_store.state, {"type": "X", "payload": {}})

        assert calls, "a sync on_state_changed must be awaited through await_maybe"

    async def test_sync_on_unauthorized_runs(self):
        calls = []

        class SyncGate(StatefulLayoutView):
            owner_only = True

            def on_unauthorized(self, interaction):
                calls.append(interaction.user.id)

            def build_ui(self):
                self.clear_items()
                self.add_item(TextDisplay("built"))

        view = SyncGate(interaction=_make_interaction(user_id=1), user_id=1)
        view.build_ui()

        allowed = await view.interaction_check(_make_interaction(user_id=999))

        assert allowed is False
        assert calls == [999]


class TestContainerAccentAcceptsBothSpellings:
    """discord.py types ``accent_colour`` ``Optional[Union[Colour, int]]``.

    ``Embed.colour``'s setter coerces an int, so the same hex literal has
    always worked on a V1 embed; ``Container.accent_colour`` stores one
    verbatim, so both spellings reach the digest from any Container the
    caller built without a builder.
    """

    def test_digest_reads_a_raw_int_accent(self):
        view = DisplayLayoutView(container=Container(TextDisplay("x"), accent_colour=0xD4AF37))

        assert view._compute_tree_digest() is not None

    def test_both_spellings_of_one_colour_hash_equal(self):
        as_int = DisplayLayoutView(container=Container(TextDisplay("x"), accent_colour=0xD4AF37))
        as_colour = DisplayLayoutView(
            container=Container(TextDisplay("x"), accent_colour=discord.Colour(0xD4AF37))
        )

        assert as_int._compute_tree_digest() == as_colour._compute_tree_digest()

    def test_a_black_accent_is_not_read_as_no_accent(self):
        black = DisplayLayoutView(container=Container(TextDisplay("x"), accent_colour=0x000000))
        unset = DisplayLayoutView(container=Container(TextDisplay("x")))

        assert black._compute_tree_digest() != unset._compute_tree_digest()


class TestPostSendSetupNeverReportsAFailedSend:
    """Bookkeeping after the send carries the send's own contract.

    The rollback path ends at the Discord call, so a raise past it leaves
    the view registered and the message live while reporting that neither
    happened. A caller that retries on that answer posts a second copy.
    """

    async def test_a_raising_digest_still_returns_the_live_message(self, caplog):
        ctx = MagicMock()
        ctx.author.id = 100
        ctx.guild.id = 200
        sent = MagicMock()
        sent.id = 999
        ctx.send = AsyncMock(return_value=sent)

        view = RenderableLayoutView(context=ctx, user_id=100, guild_id=200)
        view._compute_tree_digest = MagicMock(side_effect=RuntimeError("boom"))

        with caplog.at_level(logging.ERROR):
            result = await view.send()

        assert result is sent
        assert view._message is sent
        ctx.send.assert_awaited_once()
        # No baseline was recorded, so the next refresh ships unconditionally
        # rather than skipping an edit against a digest that never computed.
        assert view._last_tree_digest is None
        assert "could not digest its component tree" in caplog.text

    async def test_a_circular_parent_is_rejected_before_anything_is_sent(self):
        parent_ctx = MagicMock()
        parent_ctx.author.id = 100
        parent_ctx.guild.id = 200
        parent_ctx.send = AsyncMock(return_value=MagicMock(id=1))
        parent = RenderableLayoutView(context=parent_ctx, user_id=100, guild_id=200)
        await parent.send()

        child_ctx = MagicMock()
        child_ctx.author.id = 100
        child_ctx.guild.id = 200
        child_ctx.send = AsyncMock(return_value=MagicMock(id=2))
        child = RenderableLayoutView(context=child_ctx, user_id=100, guild_id=200, parent=parent)
        child.attach_child(parent)

        with pytest.raises(ValueError, match="Circular attachment"):
            await child.send()

        child_ctx.send.assert_not_called()
        assert child._message is None
        assert child.id not in child.state_store.get_active_views()
        assert child.id not in child.state_store.state["views"]

    @staticmethod
    def _frozen(view):
        buttons = [item for item in view.walk_children() if isinstance(item, Button)]
        return buttons and all(button.disabled for button in buttons)

    @staticmethod
    def _board(key):
        from cascadeui import PersistentLayoutView

        class Board(PersistentLayoutView):
            subscribed_actions = None

            def build_ui(self):
                # A real change on every render, so no render is skipped.
                self.clear_items()
                self.renders = getattr(self, "renders", 0) + 1
                self.add_item(TextDisplay(f"board {self.renders}"))
                self.add_item(ActionRow(StatefulButton(label="Go", custom_id="board_go")))

        first = _make_interaction()
        first.original_response.return_value.id = 100
        board = Board(interaction=first, persistence_key=key)
        board.build_ui()
        return board

    async def test_a_step_that_raises_does_not_skip_the_rest(self, caplog):
        """A middleware raising on the message's state row skipped the rest of
        the send: a re-sent panel's row stayed on the message it left, and that
        message kept live buttons."""
        panel = self._board("raising-step")
        await panel.send()
        old = panel._message
        store = panel.state_store

        async def failing(action, state, next_fn):
            result = await next_fn(action, state)
            if action["type"] == "VIEW_UPDATED":
                raise RuntimeError("audit sink down")
            return result

        store._add_middleware(failing)
        again = _make_interaction()
        again.original_response.return_value.id = 200
        panel.interaction = again
        before = old.edit.await_count
        with caplog.at_level(logging.ERROR, logger="cascadeui"):
            assert await panel.send() is panel._message

        assert store.state["persistent_views"]["raising-step"]["message_id"] == "200"
        assert old.edit.await_count == before + 1
        assert self._frozen(old.edit.await_args.kwargs["view"])
        assert "recording its message in the state store raised RuntimeError" in caplog.text

    async def test_a_new_message_found_deleted_during_the_send(self, caplog):
        """A render during the send found the new message deleted, and the
        registration then raised on the missing message: an ERROR claiming a
        live, registered view, and the message left was never closed."""
        panel = self._board("gone-mid-send")
        await panel.send()
        old = panel._message
        again = _make_interaction()
        again.original_response.return_value.id = 200
        again.original_response.return_value.edit = AsyncMock(
            side_effect=discord.NotFound(MagicMock(status=404), "Unknown Message")
        )
        panel.interaction = again
        before = old.edit.await_count
        with caplog.at_level(logging.ERROR, logger="cascadeui"):
            assert await panel.send() is None
            await asyncio.wait_for(panel._message_gone_task, 5)

        assert panel._torn_down()
        assert old.edit.await_count == before + 1
        assert self._frozen(old.edit.await_args.kwargs["view"])
        assert "gone-mid-send" not in panel.state_store.state["persistent_views"]
        assert "was sent, but" not in caplog.text


class TestTransportFailuresDoNotSurfaceAsErrors:
    """A request that never reached Discord is a third sibling.

    ``RateLimited`` is a sibling of ``HTTPException``; aiohttp's client
    errors are siblings of both, since a connection that drops carries no
    HTTP status at all. Uncaught on a repaint they reach ``on_error`` and
    render a failure card over content that is perfectly fine.
    """

    def test_transport_errors_are_not_http_exceptions(self):
        assert not issubclass(aiohttp.ClientOSError, discord.HTTPException)
        # The umbrella is ClientError, not OSError: ServerDisconnectedError
        # is the former and not the latter, and asyncio.TimeoutError IS an
        # OSError from 3.11 but not on 3.10, so an OSError clause would mean
        # two different things across the supported interpreters.
        assert not issubclass(aiohttp.ServerDisconnectedError, OSError)
        assert issubclass(aiohttp.ServerDisconnectedError, aiohttp.ClientError)

    @pytest.mark.parametrize(
        "error",
        [
            aiohttp.ClientOSError(104, "Connection reset by peer"),
            aiohttp.ServerDisconnectedError(),
        ],
        ids=["reset", "disconnected"],
    )
    async def test_refresh_degrades_instead_of_raising(self, error):
        view = RenderableLayoutView(interaction=_make_interaction(), user_id=1, guild_id=2)
        message = MagicMock()
        message.id = 999
        message.edit = AsyncMock(side_effect=error)
        view._message = message
        view._last_tree_digest = 12345

        await view.refresh()

        # No baseline, so the next refresh re-ships the tree unconditionally.
        assert view._last_tree_digest is None
        assert view._refresh_degraded is True

    async def test_a_transport_failure_on_the_post_send_refetch_keeps_the_message(self):
        ctx = MagicMock()
        ctx.author.id = 100
        ctx.guild.id = 200
        sent = MagicMock()
        sent.id = 999
        sent.__class__ = discord.InteractionMessage
        sent.channel.fetch_message = AsyncMock(
            side_effect=aiohttp.ClientOSError(104, "Connection reset by peer")
        )
        ctx.send = AsyncMock(return_value=sent)

        view = RenderableLayoutView(context=ctx, user_id=100, guild_id=200)
        result = await view.send()

        # The message is live; reporting a failed send would have the caller
        # retry and post a second copy.
        assert result is sent
        assert view._message is sent

    async def test_a_timed_out_post_send_refetch_keeps_the_message(self):
        """aiohttp's total timeout raises a bare TimeoutError, which escaped a
        send Discord had already accepted, so the rest of the send never ran."""
        ctx = MagicMock()
        ctx.author.id = 100
        ctx.guild.id = 200
        sent = MagicMock()
        sent.id = 999
        sent.__class__ = discord.InteractionMessage
        sent.channel.fetch_message = AsyncMock(side_effect=asyncio.TimeoutError())
        ctx.send = AsyncMock(return_value=sent)

        view = RenderableLayoutView(context=ctx, user_id=100, guild_id=200)
        result = await view.send()

        assert result is sent
        assert view._message is sent
        assert view._has_rendered is True, "the send stopped before its last stage"

    async def test_navigation_rolls_back_when_the_edit_never_landed(self):
        # An interaction whose own message differs from the view's routes the
        # edit through refresh() rather than the interaction fast path.
        interaction = _make_interaction()
        interaction.message = MagicMock()
        interaction.message.id = 111

        message = MagicMock()
        message.id = 999
        message.edit = AsyncMock(side_effect=aiohttp.ClientOSError(104, "reset"))

        source = RenderableLayoutView(interaction=interaction, user_id=1, guild_id=2)
        source._message = message
        destination = RenderableLayoutView(user_id=1, guild_id=2)
        destination._message = message

        from cascadeui.views._navigation import _NAV_FAILED

        outcome = await source._apply_navigation_edit(destination, interaction, None)

        # refresh() swallows the transport failure so a repaint can retry, but
        # navigation tears the source down on the strength of the edit, so a
        # swallowed failure has to read as "not shipped" here.
        assert outcome == _NAV_FAILED

    async def test_a_transport_blip_does_not_forfeit_the_webhook_handle(self):
        """The webhook handle is the only endpoint that can edit an embed.

        The channel endpoint silently strips embed edits on an
        interaction-owned message, so nulling this ref on a dropped
        connection would freeze a V1 view's embeds permanently over a blip.
        Only an HTTP error means the token is actually gone.
        """
        from cascadeui.views.view import StatefulView

        view = StatefulView(interaction=_make_interaction(), user_id=1, guild_id=2)
        channel_message = MagicMock()
        channel_message.id = 999
        channel_message.edit = AsyncMock()
        webhook = MagicMock()
        webhook.id = 999
        webhook.edit = AsyncMock(side_effect=aiohttp.ClientConnectionError("reset"))
        view._message = channel_message
        view._webhook_message = webhook
        view._last_tree_digest = None

        await view.refresh(embed=discord.Embed(title="x"))

        assert view._webhook_message is webhook
        assert view.refresh_degraded is True
        # No fall-through: the channel endpoint would drop the embed silently.
        channel_message.edit.assert_not_called()

    async def test_refresh_degraded_is_public_and_resets_per_call(self):
        view = RenderableLayoutView(interaction=_make_interaction(), user_id=1, guild_id=2)
        message = MagicMock()
        message.id = 999
        message.edit = AsyncMock(side_effect=aiohttp.ClientOSError(104, "reset"))
        view._message = message

        assert view.refresh_degraded is False
        await view.refresh()
        assert view.refresh_degraded is True

        message.edit = AsyncMock()
        await view.refresh()
        assert view.refresh_degraded is False

    def test_aiohttp_connect_timeouts_are_also_transport_errors(self):
        """The overlap that decides handler ordering.

        aiohttp's connect and socket timeouts inherit BOTH ``ClientError``
        and ``asyncio.TimeoutError``, so a timeout clause placed first sees
        them before the transport clause runs. discord.py builds its session
        with no ``timeout=``, so aiohttp's 30s ``sock_connect`` applies and
        this is the likeliest transport failure in production, not an exotic
        one.
        """
        assert issubclass(aiohttp.ConnectionTimeoutError, asyncio.TimeoutError)
        assert issubclass(aiohttp.ConnectionTimeoutError, aiohttp.ClientError)
        assert issubclass(aiohttp.SocketTimeoutError, asyncio.TimeoutError)
        assert issubclass(aiohttp.SocketTimeoutError, aiohttp.ClientError)
        # A cancelled wait_for is NOT one, so the stall branch keeps its case.
        assert not issubclass(asyncio.TimeoutError, aiohttp.ClientError)

    @pytest.mark.parametrize(
        "error,degraded",
        [
            (aiohttp.ConnectionTimeoutError("connect"), True),
            (aiohttp.SocketTimeoutError("sock"), True),
            (asyncio.TimeoutError(), False),
        ],
        ids=["connect-timeout", "socket-timeout", "stall"],
    )
    async def test_a_connect_timeout_reads_as_transport_not_as_a_stall(self, error, degraded):
        view = RenderableLayoutView(interaction=_make_interaction(), user_id=1, guild_id=2)
        message = MagicMock()
        message.id = 999
        message.edit = AsyncMock(side_effect=error)
        view._message = message
        view._last_tree_digest = None

        await view.refresh()

        # A connect that never completed is not the indeterminate case a
        # cancelled wait_for is: the request definitively never left the host.
        assert view.refresh_degraded is degraded

    async def test_reload_clears_the_flag_even_when_it_coalesces(self):
        """``reload()`` can return at the throttle gate without reaching
        ``refresh()``, so the flag must not describe an older call."""
        view = RenderableLayoutView(interaction=_make_interaction(), user_id=1, guild_id=2)
        message = MagicMock()
        message.id = 999
        message.edit = AsyncMock(side_effect=aiohttp.ClientOSError(104, "reset"))
        view._message = message

        await view.refresh()
        assert view.refresh_degraded is True

        view.refresh_cooldown_ms = 5000
        view._cooldown_not_before = time.monotonic() + 5
        await view.reload()

        assert view.refresh_degraded is False

    @pytest.mark.parametrize(
        "error",
        [
            aiohttp.ClientOSError(104, "reset"),
            aiohttp.ConnectionTimeoutError("connect"),
        ],
        ids=["reset", "connect-timeout"],
    )
    async def test_the_acting_fast_path_handles_transport_too(self, error):
        """A real click takes the fast path first, and it has its own handling.

        Every other transport test drives an unbound interaction, so the
        refresh falls straight through to the channel endpoint and the fast
        path's two branches (the hybrid-timeout split and the fall-through
        for a plain ClientError) go unexercised.
        """
        interaction = _make_interaction()
        interaction.message = MagicMock()
        interaction.message.id = 999
        interaction.response.is_done.return_value = False
        interaction.response.edit_message = AsyncMock(side_effect=error)

        view = RenderableLayoutView(interaction=interaction, user_id=1, guild_id=2)
        message = MagicMock()
        message.id = 999
        message.edit = AsyncMock(side_effect=error)
        view._message = message
        view._last_tree_digest = None

        token = _CURRENT_INTERACTION.set(interaction)
        try:
            await view.refresh()
        finally:
            _CURRENT_INTERACTION.reset(token)

        interaction.response.edit_message.assert_awaited()
        assert view.refresh_degraded is True


class TestRestoreOnDroppedRender:
    """A two-press confirmation must not become one press on a dropped render.

    The arming write lands on the view before the render that would show it.
    When that render is swallowed, the button on screen still looks unarmed,
    so the obvious response is to press again -- and that press executes,
    because the flag is already set.
    """

    class _ArmThenConfirm(RenderableLayoutView):
        def __init__(self, **kwargs):
            self.confirming = False
            self.executed = False
            super().__init__(**kwargs)

        async def press(self):
            if self.confirming:
                self.executed = True
                return
            with self.restore_on_dropped_render("confirming"):
                self.confirming = True
                await self.refresh()

    def _wire(self, view, *, error=None):
        message = MagicMock()
        message.id = 999
        message.edit = AsyncMock(side_effect=error)
        view._message = message
        return message

    async def test_a_dropped_arming_render_cannot_be_confirmed_by_the_next_press(self):
        view = self._ArmThenConfirm(interaction=_make_interaction(), user_id=1, guild_id=2)
        self._wire(view, error=aiohttp.ClientOSError(104, "reset"))

        await view.press()
        assert view.confirming is False, "the flag must match the unarmed button on screen"

        await view.press()
        assert view.executed is False, "a dropped packet must not collapse two presses into one"

    async def test_a_landed_render_keeps_the_armed_state(self):
        view = self._ArmThenConfirm(interaction=_make_interaction(), user_id=1, guild_id=2)
        self._wire(view)

        await view.press()
        assert view.confirming is True

        await view.press()
        assert view.executed is True

    async def test_an_exception_inside_the_block_restores_nothing(self):
        """A raise is its own signal, and the caller owns the recovery."""
        view = self._ArmThenConfirm(interaction=_make_interaction(), user_id=1, guild_id=2)
        self._wire(view)

        with pytest.raises(RuntimeError):
            with view.restore_on_dropped_render("confirming"):
                view.confirming = True
                raise RuntimeError("callback blew up")

        assert view.confirming is True

    async def test_it_restores_every_attribute_it_was_given(self):
        view = self._ArmThenConfirm(interaction=_make_interaction(), user_id=1, guild_id=2)
        self._wire(view, error=aiohttp.ClientOSError(104, "reset"))
        view.selection = "before"

        with view.restore_on_dropped_render("confirming", "selection"):
            view.confirming = True
            view.selection = "after"
            await view.refresh()

        assert view.confirming is False
        assert view.selection == "before"

    async def test_an_earlier_drop_does_not_answer_for_this_block(self):
        """The flag is sticky until a refresh clears it, so entry must reset it.

        A block whose refresh sits behind a conditional that did not run has
        made no render at all, and must keep its write no matter what the
        previous refresh did.
        """
        view = self._ArmThenConfirm(interaction=_make_interaction(), user_id=1, guild_id=2)
        self._wire(view, error=aiohttp.ClientOSError(104, "reset"))

        await view.refresh()
        assert view.refresh_degraded is True

        with view.restore_on_dropped_render("confirming"):
            view.confirming = True

        assert view.confirming is True, "no render was made here, so nothing is owed a rewind"

    async def test_a_drop_in_an_earlier_block_does_not_revert_a_later_one(self):
        view = self._ArmThenConfirm(interaction=_make_interaction(), user_id=1, guild_id=2)
        message = self._wire(view, error=aiohttp.ClientOSError(104, "reset"))
        view.selection = "before"

        await view.press()
        assert view.confirming is False

        message.edit = AsyncMock()
        with view.restore_on_dropped_render("selection"):
            view.selection = "kept"

        assert view.selection == "kept"

    async def test_rebuild_recomposes_the_tree_the_restored_value_names(self):
        """A V2 tree IS the content, so rebinding the flag is only half of it.

        Without the rebuild the attribute goes back and the tree keeps what
        the arming render composed, so the next refresh that does not
        rebuild first ships a screen the flag no longer names.
        """
        view = self._ArmThenConfirm(interaction=_make_interaction(), user_id=1, guild_id=2)
        self._wire(view, error=aiohttp.ClientOSError(104, "reset"))
        view.rebuilt_for = None

        def _rebuild():
            view.rebuilt_for = view.confirming

        with view.restore_on_dropped_render("confirming", rebuild=_rebuild):
            view.confirming = True
            await view.refresh()

        assert view.confirming is False
        assert view.rebuilt_for is False, "the rebuild must see the restored value"

    async def test_rebuild_is_skipped_when_the_render_lands(self):
        view = self._ArmThenConfirm(interaction=_make_interaction(), user_id=1, guild_id=2)
        self._wire(view)
        calls = []

        with view.restore_on_dropped_render("confirming", rebuild=lambda: calls.append(1)):
            view.confirming = True
            await view.refresh()

        assert view.confirming is True
        assert calls == []

    async def test_a_non_callable_rebuild_is_rejected_at_the_call(self):
        view = self._ArmThenConfirm(interaction=_make_interaction(), user_id=1, guild_id=2)

        with pytest.raises(TypeError, match="must be callable"):
            with view.restore_on_dropped_render("confirming", rebuild=42):
                pass

    async def test_an_async_rebuild_is_rejected_rather_than_left_unawaited(self):
        """The restore runs at a synchronous exit and cannot await."""
        view = self._ArmThenConfirm(interaction=_make_interaction(), user_id=1, guild_id=2)

        async def _rebuild():
            pass

        with pytest.raises(TypeError, match="must be synchronous"):
            with view.restore_on_dropped_render("confirming", rebuild=_rebuild):
                pass

    async def test_the_snapshot_holds_the_binding_not_the_contents(self):
        """Documented contract: a name rebound comes back, in-place edits do not."""
        view = self._ArmThenConfirm(interaction=_make_interaction(), user_id=1, guild_id=2)
        self._wire(view, error=aiohttp.ClientOSError(104, "reset"))
        view.rows = [1, 2]

        with view.restore_on_dropped_render("rows"):
            view.rows = view.rows + [3]
            await view.refresh()

        assert view.rows == [1, 2]


class TestRebuiltTreeShipsOnTeardown:
    """A farewell card composed before teardown has to reach the message.

    The V2 teardown edit was gated on whether anything froze, which skips a
    no-op PATCH for a display view that has no components to disable. A
    caller that clears the tree and composes a closing card first has
    nothing left to freeze either, so its card was dropped and the message
    kept the live-looking controls the teardown was replacing. The next
    press then landed on a stopped view and Discord reported it as failed.
    """

    def _wire(self, view):
        message = MagicMock()
        message.id = 4242
        message.edit = AsyncMock()
        message.delete = AsyncMock()
        view._message = message
        return message

    async def test_exit_ships_a_tree_rebuilt_with_no_freezable_items(self):
        view = RenderableLayoutView(interaction=_make_interaction(), user_id=1, guild_id=2)
        view.add_item(ActionRow(StatefulButton(label="Accept")))
        await view.send()
        message = self._wire(view)

        view.clear_items()
        view.add_item(card("Challenge expired."))
        await view.exit(delete_message=False)

        message.edit.assert_awaited_once()

    async def test_on_timeout_ships_a_tree_rebuilt_with_no_freezable_items(self):
        view = RenderableLayoutView(interaction=_make_interaction(), user_id=1, guild_id=2)
        view.add_item(ActionRow(StatefulButton(label="Accept")))
        await view.send()
        message = self._wire(view)

        view.clear_items()
        view.add_item(card("Challenge expired."))
        await view.on_timeout()

        message.edit.assert_awaited_once()

    async def test_an_unchanged_display_view_still_skips_the_edit(self):
        """The optimization the guard exists for has to survive the fix."""
        view = RenderableLayoutView(interaction=_make_interaction(), user_id=1, guild_id=2)
        await view.send()
        message = self._wire(view)

        await view.on_timeout()

        message.edit.assert_not_called()

    async def test_a_freezable_view_still_ships_its_disable_edit(self):
        view = RenderableLayoutView(interaction=_make_interaction(), user_id=1, guild_id=2)
        view.add_item(ActionRow(StatefulButton(label="Fire")))
        await view.send()
        message = self._wire(view)

        await view.on_timeout()

        message.edit.assert_awaited_once()

    class _BuildsInOnLoad(StatefulLayoutView):
        async def on_load(self):
            self.clear_items()
            self.add_item(TextDisplay("loaded"))

    async def test_exit_skips_an_empty_tree_on_a_view_that_never_rendered(self):
        """An empty V2 tree is error 50006 however the view got there."""
        view = self._BuildsInOnLoad(interaction=_make_interaction(), user_id=1, guild_id=2)
        message = self._wire(view)
        assert view._has_rendered is False and not view.children

        await view.exit(delete_message=False)

        message.edit.assert_not_called()

    async def test_on_timeout_skips_an_empty_tree_on_a_view_that_never_rendered(self):
        view = self._BuildsInOnLoad(interaction=_make_interaction(), user_id=1, guild_id=2)
        message = self._wire(view)

        await view.on_timeout()

        message.edit.assert_not_called()

    async def test_exit_skips_a_rendered_view_cleared_to_an_empty_tree(self, caplog):
        """A rendered view torn down with nothing left is an override bug the
        user needs to see, so the skip is logged rather than silent."""
        view = RenderableLayoutView(interaction=_make_interaction(), user_id=1, guild_id=2)
        await view.send()
        message = self._wire(view)

        view.clear_items()
        with caplog.at_level(logging.WARNING, logger="cascadeui.views.base"):
            await view.exit(delete_message=False)

        message.edit.assert_not_called()
        assert "empty component tree" in caplog.text

    async def test_exit_skips_a_tree_that_fails_placement(self, caplog):
        """Teardown runs the same placement check as the other render seams.
        An empty Container is non-empty at the top level but still a 400."""
        view = RenderableLayoutView(interaction=_make_interaction(), user_id=1, guild_id=2)
        await view.send()
        message = self._wire(view)

        view.clear_items()
        view.add_item(card())
        with caplog.at_level(logging.WARNING, logger="cascadeui.views.base"):
            await view.exit(delete_message=False)

        message.edit.assert_not_called()
        assert "Discord would reject" in caplog.text

    async def test_programmatic_push_ships_a_farewell_card_from_the_destination(self):
        """A push with no interaction edits through the channel, and a
        farewell card the destination composes on exit ships after it."""

        class _Farewell(RenderableLayoutView):
            async def exit(self, delete_message=None):
                self.clear_items()
                self.add_item(card("Session closed."))
                return await super().exit(delete_message=delete_message)

        root = RenderableLayoutView(user_id=1, guild_id=2)
        message = self._wire(root)
        child = await root.push(_Farewell)
        assert message.edit.await_args.kwargs["view"] is child
        message.edit.reset_mock()

        await child.exit(delete_message=False)

        message.edit.assert_awaited_once()

    async def test_programmatic_push_loads_and_ships_the_destination(self):
        """A push with no interaction never ran on_load() and never edited,
        leaving the old view's buttons on a message it no longer owned. It
        loads the destination and ships it through the channel."""
        root = RenderableLayoutView(user_id=1, guild_id=2)
        message = self._wire(root)
        child = await root.push(self._BuildsInOnLoad)

        assert [c.content for c in child.children] == ["loaded"]
        message.edit.assert_awaited_once()
        assert message.edit.await_args.kwargs["view"] is child

    async def test_v1_view_that_never_rendered_still_ships_on_timeout(self):
        """V1 messages carry embed content, so an empty V1 view is a valid
        edit that strips the buttons; the empty-tree refusal is V2-only."""
        from cascadeui.views.view import StatefulView

        class _Bound(StatefulView):
            pass

        view = _Bound(user_id=1, guild_id=2)
        message = self._wire(view)
        assert view._has_rendered is False

        await view.on_timeout()

        message.edit.assert_awaited_once()

    async def test_exit_still_ships_a_rebuild_when_the_digest_was_never_stamped(self):
        """``_last_tree_digest`` is ``None`` for more than one reason, and
        only "never rendered" means "skip".

        A push/pop navigation landing never stamps the destination's digest,
        so a pushed view carries ``_has_rendered = True`` (content is
        genuinely on screen) alongside ``_last_tree_digest = None`` for its
        whole life until some later refresh happens to land. Gating the
        teardown skip on the digest alone would misread that combination as
        "never rendered" and drop a farewell card composed just before
        teardown -- the exact defect the digest comparison exists to catch.
        """
        view = RenderableLayoutView(interaction=_make_interaction(), user_id=1, guild_id=2)
        view.add_item(ActionRow(StatefulButton(label="Accept")))
        await view.send()
        message = self._wire(view)

        # Simulate the state a navigation landing leaves behind: rendered,
        # but with no stamped digest.
        view._last_tree_digest = None
        view.clear_items()
        view.add_item(card("Challenge expired."))

        await view.exit(delete_message=False)

        message.edit.assert_awaited_once()

    async def test_on_timeout_still_ships_a_rebuild_when_the_digest_was_never_stamped(self):
        view = RenderableLayoutView(interaction=_make_interaction(), user_id=1, guild_id=2)
        view.add_item(ActionRow(StatefulButton(label="Accept")))
        await view.send()
        message = self._wire(view)

        view._last_tree_digest = None
        view.clear_items()
        view.add_item(card("Challenge expired."))

        await view.on_timeout()

        message.edit.assert_awaited_once()

    async def test_has_rendered_survives_a_raising_digest_after_send(self):
        """``_has_rendered`` means "the send landed", stamped before the
        digest computation that follows it.

        A user subclass's tree can raise inside ``_compute_tree_digest``
        (a property read on a live item) after the send already reached
        Discord. ``_send_pipeline`` swallows that raise and drops the
        digest to ``None``, but the flag must not depend on the digest
        computation succeeding -- otherwise a view Discord is genuinely
        showing would read as never rendered at its next teardown.
        """
        view = RenderableLayoutView(interaction=_make_interaction(), user_id=1, guild_id=2)

        with patch.object(type(view), "_compute_tree_digest", side_effect=RuntimeError("boom")):
            await view.send()

        assert view._has_rendered is True
        assert view._last_tree_digest is None

        message = self._wire(view)
        view.clear_items()
        view.add_item(card("Challenge expired."))

        await view.exit(delete_message=False)

        message.edit.assert_awaited_once()

    async def test_refresh_marks_the_view_as_rendered_without_a_prior_send(self):
        """A view can land its first tree through ``refresh()`` rather than
        ``send()`` -- a persistence-restored view's ``on_restore()`` calling
        ``reload()``/``refresh()`` is the production shape this covers. The
        flag has to flip there too, or a teardown right after would read a
        genuinely rendered view as never rendered.
        """
        view = RenderableLayoutView(interaction=_make_interaction(), user_id=1, guild_id=2)
        message = self._wire(view)
        assert view._has_rendered is False

        await view.refresh()

        assert view._has_rendered is True


class TestRendersAfterTheCloseShipDisabledControls:
    """Nothing answers a closed view's controls, so a later render disables them.

    A render after the close (a modal submitted late, a caller's own update
    loop) rebuilt the tree and shipped its buttons clickable, and every
    press on them failed.
    """

    def _wire(self, view):
        message = MagicMock()
        message.id = 4242
        message.edit = AsyncMock()
        message.delete = AsyncMock()
        view._message = message
        return message

    @staticmethod
    def _shipped(message):
        view = message.edit.await_args.kwargs["view"]
        return {
            item.label: item.disabled for item in view.walk_children() if isinstance(item, Button)
        }

    async def _closed(self, close):
        view = RenderableLayoutView(interaction=_make_interaction(), user_id=1, guild_id=2)
        view.add_item(ActionRow(StatefulButton(label="Rename")))
        await view.send()
        message = self._wire(view)
        if close == "timeout":
            await view.on_timeout()
        else:
            await view.exit(delete_message=False)
        return view, message

    @pytest.mark.parametrize("close", ["timeout", "exit"])
    async def test_a_rebuilt_tree_ships_its_buttons_disabled(self, close):
        view, message = await self._closed(close)
        view.clear_items()
        view.add_item(TextDisplay("renamed"))
        view.add_item(ActionRow(StatefulButton(label="Rename")))

        outcome = await view.refresh()

        assert outcome is RenderOutcome.RENDERED
        assert self._shipped(message) == {"Rename": True}

    async def test_a_render_after_a_close_that_deleted_the_message_sends_nothing(self):
        """The render edited the message the close had deleted, and the 404 it
        met fired on_message_gone() on the closed view."""
        view, message = await self._closed("exit")
        await view.exit(delete_message=True)
        edits = message.edit.await_count

        outcome = await view.refresh()

        assert outcome is RenderOutcome.NO_MESSAGE
        assert message.edit.await_count == edits

    async def test_a_link_button_stays_enabled(self):
        """A link opens its URL with no view behind it, so it still works."""
        view, message = await self._closed("timeout")
        view.clear_items()
        view.add_item(TextDisplay("Game over"))
        view.add_item(
            ActionRow(
                LinkButton(label="Results", url="https://example.com"),
                StatefulButton(label="Rematch"),
            )
        )

        await view.refresh()

        assert self._shipped(message) == {"Results": False, "Rematch": True}

    async def test_a_live_view_keeps_its_buttons_enabled(self):
        view = RenderableLayoutView(interaction=_make_interaction(), user_id=1, guild_id=2)
        view.add_item(ActionRow(StatefulButton(label="Rename")))
        await view.send()
        message = self._wire(view)
        view.add_item(TextDisplay("more"))

        await view.refresh()

        assert self._shipped(message) == {"Rename": False}

    async def test_a_v1_view_ships_its_buttons_disabled(self):
        from cascadeui.views.view import StatefulView

        view = StatefulView(interaction=_make_interaction(), user_id=1, guild_id=2)
        view.add_item(StatefulButton(label="Rename"))
        await view.send(content="panel")
        message = self._wire(view)
        await view.on_timeout()
        view.clear_items()
        view.add_item(StatefulButton(label="Rename"))

        await view.refresh(content="renamed")

        shipped = message.edit.await_args.kwargs["view"]
        assert [item.disabled for item in shipped.children] == [True]

    async def test_a_v1_exit_that_stripped_the_buttons_keeps_them_off(self):
        """The render put them back, disabled, on a message the exit had left
        with none."""
        from cascadeui.views.view import StatefulView

        view = StatefulView(interaction=_make_interaction(), user_id=1, guild_id=2)
        view.add_item(StatefulButton(label="Rename"))
        await view.send(content="panel")
        message = self._wire(view)
        await view.exit(delete_message=False)
        assert message.edit.await_args.kwargs["view"] is None
        view.clear_items()
        view.add_item(StatefulButton(label="Rename"))

        await view.refresh(content="renamed")

        assert message.edit.await_args.kwargs["view"].children == []

    async def test_the_close_leaves_link_buttons_clickable(self):
        view = RenderableLayoutView(interaction=_make_interaction(), user_id=1, guild_id=2)
        view.add_item(
            ActionRow(
                LinkButton(label="Docs", url="https://example.com"),
                StatefulButton(label="Go"),
            )
        )
        await view.send()
        message = self._wire(view)

        await view.exit(delete_message=False)

        assert self._shipped(message) == {"Docs": False, "Go": True}


class TestTeardownFromAnOwnedTask:
    """A teardown run in a task the torn-down view owns finishes its edit.

    The teardown cancels the view's tasks, the one running it included, so
    the cancel landed at the freeze edit's first suspension and the panel
    stayed up looking live. The task is now cancelled once the teardown
    returns: code after the call runs until its next await.
    """

    @staticmethod
    def _tracked(view, log, name):
        async def edit(**kwargs):
            log.append(f"{name}:start")
            await asyncio.sleep(0)
            log.append(f"{name}:done")
            return view._message

        view._message.edit = AsyncMock(side_effect=edit)

    @staticmethod
    async def _live(**kwargs):
        view = RenderableLayoutView(interaction=_make_interaction(), **kwargs)
        view.add_item(ActionRow(StatefulButton(label="Go", custom_id="go")))
        await view.send()
        return view

    async def test_an_exit_from_the_views_own_task_ships_its_freeze(self):
        view = await self._live()
        log = []
        self._tracked(view, log, "freeze")

        async def close():
            await view.exit(delete_message=False)
            log.append("returned")
            await asyncio.sleep(0)
            log.append("ran on")

        with pytest.raises(asyncio.CancelledError):
            await view.create_task(close())

        assert log == ["freeze:start", "freeze:done", "returned"]

    async def test_on_timeout_from_the_views_own_task_ships_its_freeze(self):
        view = await self._live()
        log = []
        self._tracked(view, log, "freeze")

        with pytest.raises(asyncio.CancelledError):
            await view.create_task(view.on_timeout())

        assert log == ["freeze:start", "freeze:done"]

    async def test_a_parent_exit_from_a_childs_own_task_ships_the_parents_freeze(self):
        """The child's exit runs inside the parent's and cancels the child's
        tasks; its task has to last until the parent's exit is done too."""
        parent = await self._live()
        child = RenderableLayoutView(interaction=_make_interaction(), parent=parent)
        await child.send()
        log = []
        self._tracked(parent, log, "parent")

        with pytest.raises(asyncio.CancelledError):
            await child.create_task(parent.exit(delete_message=False))

        assert log == ["parent:start", "parent:done"]
        assert child.is_finished()

    async def test_a_parent_exit_from_a_stopped_childs_task_ships_the_parents_freeze(self):
        """A child stopped without exiting (an on_timeout override that skips
        super) still owns its tasks, and the cascade cancels them itself."""
        parent = await self._live()
        child = RenderableLayoutView(interaction=_make_interaction(), parent=parent)
        await child.send()
        child.stop()
        log = []
        self._tracked(parent, log, "parent")

        with pytest.raises(asyncio.CancelledError):
            await child.create_task(parent.exit(delete_message=False))

        assert log == ["parent:start", "parent:done"]


class TestTeardownRunsOnce:
    """A view already torn down is not torn down again.

    A second exit() after an exit() or a timeout re-ran the whole teardown,
    so VIEW_DESTROYED reached store.on() hooks and subscribers twice for one
    view and the message was frozen twice. An exit() on a view that pushed
    away did the same for a view the push had already destroyed.
    """

    @staticmethod
    async def _live():
        view = RenderableLayoutView(interaction=_make_interaction())
        view.add_item(ActionRow(StatefulButton(label="Go", custom_id="go")))
        await view.send()
        return view

    @staticmethod
    def _count_destroyed(view):
        seen = []

        async def hook(action, state):
            if action["payload"].get("view_id") == view.id:
                seen.append(action["type"])

        view.state_store.on("VIEW_DESTROYED", hook)
        return seen

    async def test_a_second_exit_does_not_destroy_the_view_again(self):
        view = await self._live()
        destroyed = self._count_destroyed(view)
        view._message.edit.reset_mock()

        await view.exit()
        await view.exit()

        assert destroyed == ["VIEW_DESTROYED"]
        assert view._message.edit.await_count == 1

    async def test_an_exit_after_a_timeout_does_not_destroy_the_view_again(self):
        view = await self._live()
        destroyed = self._count_destroyed(view)
        view._message.edit.reset_mock()

        await view.on_timeout()
        await view.exit()

        assert destroyed == ["VIEW_DESTROYED"]
        assert view._message.edit.await_count == 1

    async def test_a_timeout_after_an_exit_does_nothing(self):
        # The timeout was scheduled before the exit tore the view down.
        view = await self._live()
        destroyed = self._count_destroyed(view)
        view._message.edit.reset_mock()

        await view.exit()
        await view.on_timeout()

        assert destroyed == ["VIEW_DESTROYED"]
        assert view._message.edit.await_count == 1

    async def test_an_exit_asking_to_delete_after_a_freeze_still_deletes(self):
        view = await self._live()
        destroyed = self._count_destroyed(view)

        await view.exit(delete_message=False)
        await view.exit(delete_message=True)

        assert destroyed == ["VIEW_DESTROYED"]
        view._message.delete.assert_awaited_once()

    async def test_an_exit_on_a_view_that_pushed_away_does_not_destroy_it_again(self):
        view = await self._live()
        destroyed = self._count_destroyed(view)

        new = await view.push(RenderableLayoutView)
        await view.exit()

        assert destroyed == ["VIEW_DESTROYED"]
        assert not new.is_finished()


class TestClosesAndSendsCoordinate:
    """A close that overlaps another close, or a send, of the same view.

    Two closes at once each ran the whole teardown. A close landing while the
    view was being sent tore it down under the send, which then registered it
    and posted a message whose buttons answered nothing.
    """

    @staticmethod
    async def _settle(n=40):
        for _ in range(n):
            await asyncio.sleep(0)

    @staticmethod
    async def _parent_with_parked_child():
        """A parent whose exit parks in its child's exit until ``gate`` is set."""
        entered, gate = asyncio.Event(), asyncio.Event()

        class Child(RenderableLayoutView):
            async def exit(self, delete_message=None):
                entered.set()
                await gate.wait()
                await super().exit(delete_message=delete_message)

        parent = await TestTeardownRunsOnce._live()
        child = Child(interaction=_make_interaction(), parent=parent)
        await child.send()
        return parent, entered, gate

    async def test_a_second_exit_waits_for_the_first_and_tears_down_once(self):
        parent, entered, gate = await self._parent_with_parked_child()
        destroyed = TestTeardownRunsOnce._count_destroyed(parent)
        parent._message.edit.reset_mock()

        first = asyncio.create_task(parent.exit())
        await asyncio.wait_for(entered.wait(), 1)
        second = asyncio.create_task(parent.exit())
        await self._settle()
        assert not second.done()
        gate.set()
        await first
        await second

        assert destroyed == ["VIEW_DESTROYED"]
        assert parent._message.edit.await_count == 1

    async def test_a_timeout_during_an_exit_waits_for_it(self):
        parent, entered, gate = await self._parent_with_parked_child()
        destroyed = TestTeardownRunsOnce._count_destroyed(parent)
        parent._message.edit.reset_mock()

        first = asyncio.create_task(parent.exit())
        await asyncio.wait_for(entered.wait(), 1)
        timeout = asyncio.create_task(parent.on_timeout())
        await self._settle()
        gate.set()
        await first
        await timeout

        assert destroyed == ["VIEW_DESTROYED"]
        assert parent._message.edit.await_count == 1

    async def test_a_close_waiting_on_one_that_is_cut_off_takes_over(self):
        parked, release = asyncio.Event(), asyncio.Event()

        class Parent(RenderableLayoutView):
            async def _retire_registration(self):
                parked.set()
                await release.wait()
                await super()._retire_registration()

        parent = Parent(interaction=_make_interaction())
        await parent.send()
        destroyed = TestTeardownRunsOnce._count_destroyed(parent)

        first = asyncio.create_task(parent.exit())
        await asyncio.wait_for(parked.wait(), 1)
        second = asyncio.create_task(parent.exit())
        await self._settle()
        parked.clear()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        await asyncio.wait_for(parked.wait(), 1)
        # The close that took over refuses clicks while it runs, though the
        # one cut off had let the view take them again.
        assert parent._closed()
        release.set()
        await asyncio.wait_for(second, 1)

        assert parent._torn_down()
        assert destroyed == ["VIEW_DESTROYED"]

    async def test_a_close_cut_off_leaves_nothing_for_a_later_send(self):
        # A live view whose exit was cut off is live again; sending it again
        # must not find that exit still recorded and close itself.
        parked = asyncio.Event()

        class Panel(RenderableLayoutView):
            async def _retire_registration(self):
                if not parked.is_set():
                    parked.set()
                    await asyncio.Event().wait()
                await super()._retire_registration()

        view = Panel(interaction=_make_interaction())
        await view.send()
        closing = asyncio.create_task(view.exit())
        await asyncio.wait_for(parked.wait(), 1)
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing
        view.interaction = _make_interaction()

        assert await asyncio.wait_for(view.send(), 1) is not None
        assert not view._torn_down()

    async def test_a_child_closing_its_parent_while_the_parent_closes_it_does_not_hang(self):
        # The child closes in one task and its teardown closes the parent,
        # while the parent's close, in another task, waits on the child.
        parent = await TestTeardownRunsOnce._live()
        parked, gate = asyncio.Event(), asyncio.Event()

        class Child(RenderableLayoutView):
            async def _retire_registration(self):
                parked.set()
                await gate.wait()
                await super()._retire_registration()

        child = Child(interaction=_make_interaction(), parent=parent)
        await child.send()

        async def close_parent(action, state):
            if action["payload"].get("view_id") == child.id:
                await parent.exit()

        parent.state_store.on("VIEW_DESTROYED", close_parent)
        try:
            child_exit = asyncio.create_task(child.exit())
            await asyncio.wait_for(parked.wait(), 1)
            parent_exit = asyncio.create_task(parent.exit())
            await self._settle()
            gate.set()
            await asyncio.wait_for(asyncio.gather(child_exit, parent_exit), 2)
        finally:
            parent.state_store.off("VIEW_DESTROYED", close_parent)

        assert parent._torn_down() and child._torn_down()

    async def test_a_wait_the_walk_cannot_see_ends_after_the_timeout(self, monkeypatch, caplog):
        # The close awaits, through gather, a task that closes the same view.
        monkeypatch.setattr("cascadeui.views.base._CLOSE_WAIT_SECONDS", 0.2)

        class Panel(RenderableLayoutView):
            spawned = False

            async def _retire_registration(self):
                if not self.spawned:
                    self.spawned = True
                    await asyncio.gather(self.exit())
                await super()._retire_registration()

        view = Panel(interaction=_make_interaction())
        await view.send()
        with caplog.at_level(logging.WARNING, logger="cascadeui"):
            await asyncio.wait_for(view.exit(), 2)

        assert view._torn_down()
        assert "leaves its request with that one" in caplog.text

    async def test_an_exit_from_the_views_own_on_load_posts_nothing(self):
        class Closes(RenderableLayoutView):
            async def on_load(self):
                await self.exit()

        interaction = _make_interaction()
        view = Closes(interaction=interaction)

        assert await view.send() is None
        interaction.response.send_message.assert_not_awaited()
        assert view.id not in view.state_store._active_views
        assert view._torn_down()

    async def test_an_exit_from_another_task_while_the_view_loads_posts_nothing(self):
        loading, gate = asyncio.Event(), asyncio.Event()

        class Slow(RenderableLayoutView):
            async def on_load(self):
                loading.set()
                await gate.wait()

        interaction = _make_interaction()
        view = Slow(interaction=interaction)
        sending = asyncio.create_task(view.send())
        await asyncio.wait_for(loading.wait(), 1)
        # Left with the send, which carries it out: the exit does not wait.
        await asyncio.wait_for(view.exit(), 1)
        gate.set()

        assert await sending is None
        interaction.response.send_message.assert_not_awaited()
        assert view.id not in view.state_store._active_views
        assert view.id not in view.state_store.state["views"]

    async def test_a_panel_replaced_while_it_posts_is_deleted_once_posted(self):
        class Panel(RenderableLayoutView):
            instance_limit = 1
            instance_policy = "replace"

            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.add_item(ActionRow(StatefulButton(label="Go", custom_id="go")))

        posting, gate = asyncio.Event(), asyncio.Event()
        slow = _make_interaction(user_id=42, guild_id=7)
        real_send = slow.response.send_message

        async def send_slowly(*args, **kwargs):
            posting.set()
            await gate.wait()
            return await real_send(*args, **kwargs)

        slow.response.send_message = send_slowly
        first = Panel(interaction=slow)
        first_send = asyncio.create_task(first.send())
        await asyncio.wait_for(posting.wait(), 1)
        second = Panel(interaction=_make_interaction(user_id=42, guild_id=7))
        await second.send()
        gate.set()

        # Posted before the close landed, then deleted: no live view came of it.
        assert await first_send is None
        first._message.delete.assert_awaited_once()
        assert first.id not in first.state_store._active_views
        assert not second.is_finished()

    async def test_sending_a_closed_view_raises(self):
        view = await TestTeardownRunsOnce._live()
        await view.exit()
        view.interaction = _make_interaction()

        with pytest.raises(RuntimeError, match="has closed"):
            await view.send()

    async def test_sending_a_view_already_being_sent_raises(self):
        loading, gate = asyncio.Event(), asyncio.Event()

        class Slow(RenderableLayoutView):
            async def on_load(self):
                loading.set()
                await gate.wait()

        view = Slow(interaction=_make_interaction())
        first = asyncio.create_task(view.send())
        await asyncio.wait_for(loading.wait(), 1)

        with pytest.raises(RuntimeError, match="already being sent"):
            await asyncio.wait_for(view.send(), 1)
        gate.set()
        await first

    async def test_a_child_that_exited_is_not_destroyed_again_by_its_parent(self):
        parent = await TestTeardownRunsOnce._live()
        child = RenderableLayoutView(interaction=_make_interaction(), parent=parent)
        await child.send()
        destroyed = TestTeardownRunsOnce._count_destroyed(child)

        await child.exit()
        await parent.exit()

        assert destroyed == ["VIEW_DESTROYED"]

    @staticmethod
    def _post_slowly(interaction):
        """Park ``interaction``'s send_message until the returned gate is set."""
        posting, gate = asyncio.Event(), asyncio.Event()
        real_send = interaction.response.send_message

        async def send_slowly(*args, **kwargs):
            posting.set()
            await gate.wait()
            return await real_send(*args, **kwargs)

        interaction.response.send_message = send_slowly
        return posting, gate

    async def test_a_close_the_send_carries_out_that_fails_leaves_the_view_live(self, caplog):
        failures = [RuntimeError("retire failed")]

        class Panel(RenderableLayoutView):
            async def _retire_registration(self):
                if failures:
                    raise failures.pop()
                await super()._retire_registration()

        interaction = _make_interaction()
        posting, gate = self._post_slowly(interaction)
        view = Panel(interaction=interaction)
        view.add_item(ActionRow(StatefulButton(label="Go", custom_id="go")))
        sending = asyncio.create_task(view.send())
        await asyncio.wait_for(posting.wait(), 1)
        await view.exit(delete_message=True)
        gate.set()
        with caplog.at_level(logging.ERROR, logger="cascadeui"):
            message = await sending

        # Posted, and the view still answers it: reported as sent, not raised.
        assert message is view._message
        assert not view._torn_down()
        assert "stays live on its posted message" in caplog.text
        # The failed close left nothing recorded, so a later timeout freezes
        # the message rather than delete it.
        message.edit.reset_mock()
        view.stop()
        await view.on_timeout()
        message.delete.assert_not_awaited()
        message.edit.assert_awaited_once()

    async def test_an_exit_left_with_a_send_cancelled_while_closing_still_happens(self):
        # The exit returned True once the send held its close; cancelling the
        # send dropped it, and the view stayed live after its exit.
        parked = asyncio.Event()

        class Panel(RenderableLayoutView):
            async def _retire_registration(self):
                if not parked.is_set():
                    parked.set()
                    await asyncio.Event().wait()
                await super()._retire_registration()

        interaction = _make_interaction()
        posting, gate = self._post_slowly(interaction)
        view = Panel(interaction=interaction)
        sending = asyncio.create_task(view.send())
        await asyncio.wait_for(posting.wait(), 1)
        assert await view.exit() is True
        gate.set()
        await asyncio.wait_for(parked.wait(), 1)
        sending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await sending
        await self._settle()

        assert view._torn_down()
        assert view._close_request is None
        view.interaction = _make_interaction()
        with pytest.raises(RuntimeError, match="has closed"):
            await asyncio.wait_for(view.send(), 1)

    async def test_a_child_whose_timer_fired_is_frozen_when_its_parent_closes_first(self):
        parent = await TestTeardownRunsOnce._live()
        child = RenderableLayoutView(interaction=_make_interaction(), parent=parent, timeout=60)
        child.add_item(ActionRow(StatefulButton(label="Go", custom_id="child_go")))
        await child.send()
        child._message.edit.reset_mock()

        # discord.py's timer stops the child and schedules on_timeout(); the
        # parent's cleanup reaches the child before that runs.
        child._dispatch_timeout()
        await parent.exit()
        await self._settle()

        assert child._torn_down()
        child._message.edit.assert_awaited_once()

    async def test_an_exit_after_the_parent_tore_down_a_stopped_child_freezes_it(self):
        parent = await TestTeardownRunsOnce._live()
        child = RenderableLayoutView(interaction=_make_interaction(), parent=parent)
        child.add_item(ActionRow(StatefulButton(label="Go", custom_id="child_go")))
        await child.send()
        child._message.edit.reset_mock()
        child.stop()
        await parent.exit()
        # The parent's cleanup tears a stopped child down and leaves its message.
        assert child._torn_down()
        child._message.edit.assert_not_awaited()

        await child.exit(delete_message=False)

        child._message.edit.assert_awaited_once()

    async def test_a_raising_participant_limit_hook_rolls_the_send_back(self):
        class Game(RenderableLayoutView):
            participant_limit = 1
            auto_register_participants = True
            instance_limit = 1
            instance_policy = "reject"

            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.allowed_users = {100, 2}

            async def on_participant_limit(self, user_id, interaction=None):
                raise RuntimeError("full")

        view = Game(interaction=_make_interaction(user_id=100))
        with pytest.raises(RuntimeError, match="full"):
            await view.send()

        store = view.state_store
        assert view._torn_down()
        assert view.id not in store._active_views
        assert view.id not in store.state["views"]
        # The failed send does not hold the owner's only instance slot.
        again = Game(interaction=_make_interaction(user_id=100))
        again.allowed_users = {100}
        assert await again.send() is not None

    async def test_a_send_cancelled_while_it_replaces_an_older_view_is_rolled_back(self):
        parked = asyncio.Event()

        class Panel(RenderableLayoutView):
            instance_limit = 1
            instance_policy = "replace"

            async def on_replaced(self):
                parked.set()
                await asyncio.Event().wait()

        await Panel(interaction=_make_interaction(user_id=5)).send()
        new = Panel(interaction=_make_interaction(user_id=5))
        sending = asyncio.create_task(new.send())
        await asyncio.wait_for(parked.wait(), 1)
        sending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await sending

        assert new.is_finished()
        assert new._torn_down()

    async def test_sending_a_view_while_its_timeout_runs_raises(self):
        parked, release = asyncio.Event(), asyncio.Event()

        class Parked(RenderableLayoutView):
            async def _cleanup_attached_children(self):
                if not parked.is_set():
                    parked.set()
                    await release.wait()
                await super()._cleanup_attached_children()

        view = Parked(interaction=_make_interaction())
        await view.send()
        destroyed = TestTeardownRunsOnce._count_destroyed(view)
        timing_out = asyncio.create_task(view.on_timeout())
        await asyncio.wait_for(parked.wait(), 1)
        view.interaction = _make_interaction()

        with pytest.raises(RuntimeError, match="has closed"):
            await view.send()
        release.set()
        await timing_out
        assert destroyed == ["VIEW_DESTROYED"]

    async def test_calling_on_timeout_directly_stops_the_view(self):
        # discord.py's timer stops the view first; a direct call left it
        # registered with discord.py, its timer still running.
        view = await TestTeardownRunsOnce._live()
        await view.on_timeout()
        assert view.is_finished()
        assert view._torn_down()

    @pytest.mark.parametrize("how", ["exit_in_on_load", "instance_limit", "failed_post"])
    async def test_a_view_the_parents_on_load_sent_closes_when_the_send_rolls_back(self, how):
        # The rollback tore the parent down without its exit(), which is what
        # closes attached views, so the companion stayed live on a dead parent.
        companions = []

        class Parent(RenderableLayoutView):
            instance_limit = 1
            instance_policy = "reject"

            async def on_load(self):
                companion = RenderableLayoutView(interaction=_make_interaction(), parent=self)
                companions.append(companion)
                await companion.send()
                if how == "exit_in_on_load":
                    await self.exit()

            async def on_instance_limit(self, error):
                pass

        if how == "instance_limit":
            await Parent(interaction=_make_interaction()).send()
        interaction = _make_interaction()
        if how == "failed_post":
            interaction.response.send_message = AsyncMock(
                side_effect=discord.HTTPException(MagicMock(status=500, reason="x"), "boom")
            )
        parent = Parent(interaction=interaction)
        try:
            assert await parent.send() is None
        except discord.HTTPException:
            assert how == "failed_post"

        assert parent._torn_down()
        assert companions[-1]._torn_down()

    @pytest.mark.parametrize("stopped_by", ["timer", "stop"])
    async def test_a_stopped_childs_own_children_close_with_it(self, stopped_by):
        # The parent's cleanup tore the stopped child down without closing the
        # grandchild, and the child's timeout then found it torn down.
        parent = await TestTeardownRunsOnce._live()
        child = RenderableLayoutView(interaction=_make_interaction(), parent=parent, timeout=60)
        await child.send()
        grandchild = RenderableLayoutView(interaction=_make_interaction(), parent=child)
        await grandchild.send()
        if stopped_by == "timer":
            child._dispatch_timeout()
        else:
            child.stop()

        await parent.exit()
        await self._settle()

        assert child._torn_down()
        assert grandchild._torn_down()

    async def test_a_view_stopped_during_a_resend_that_fails_is_closed(self):
        view = await TestTeardownRunsOnce._live()
        posting, gate = asyncio.Event(), asyncio.Event()
        resend = _make_interaction()

        async def fail(*args, **kwargs):
            posting.set()
            await gate.wait()
            raise discord.HTTPException(MagicMock(status=500, reason="x"), "boom")

        resend.response.send_message = AsyncMock(side_effect=fail)
        view.interaction = resend
        sending = asyncio.create_task(view.send())
        await asyncio.wait_for(posting.wait(), 1)
        view.stop()
        gate.set()
        with pytest.raises(discord.HTTPException):
            await sending

        # Stopped and still registered, it answered nothing on its message.
        assert view._torn_down()
        assert view.id not in view.state_store._active_views

    async def test_a_push_during_a_direct_on_timeout_is_refused(self):
        parked, release = asyncio.Event(), asyncio.Event()

        class Parked(RenderableLayoutView):
            async def _cleanup_attached_children(self):
                if not parked.is_set():
                    parked.set()
                    await release.wait()
                await super()._cleanup_attached_children()

        view = Parked(interaction=_make_interaction())
        await view.send()
        destroyed = TestTeardownRunsOnce._count_destroyed(view)
        timing_out = asyncio.create_task(view.on_timeout())
        await asyncio.wait_for(parked.wait(), 1)
        click = _make_interaction(message=MagicMock(id=view._message.id))

        with pytest.raises(RuntimeError, match="has closed"):
            await view.push(RenderableLayoutView, click)
        release.set()
        await timing_out
        assert destroyed == ["VIEW_DESTROYED"]

    async def test_a_timer_firing_mid_send_leaves_an_on_timeout_override_in_charge(self):
        class Keeps(RenderableLayoutView):
            timeout = 60

            async def on_timeout(self):
                pass

        view = Keeps(interaction=_make_interaction())
        view.add_item(ActionRow(StatefulButton(label="Go", custom_id="go")))
        record_message = view._update_message_state

        async def time_out_then_record(message):
            view._dispatch_timeout()
            await record_message(message)

        view._update_message_state = time_out_then_record

        assert await view.send() is not None
        await self._settle()
        # The override chose not to close it, as it may outside a send.
        assert not view._torn_down()
        view._message.edit.assert_not_awaited()

    async def test_a_close_cut_off_while_an_exit_waits_keeps_the_view_closing(self):
        # Reopened in between, a push took the message, and the waiting exit,
        # which returned True, found a view that had navigated away.
        parked = asyncio.Event()

        class Panel(RenderableLayoutView):
            async def _retire_registration(self):
                if not parked.is_set():
                    parked.set()
                    await asyncio.Event().wait()
                await super()._retire_registration()

        view = Panel(interaction=_make_interaction())
        await view.send()
        message = view._message
        first = asyncio.create_task(view.exit())
        await asyncio.wait_for(parked.wait(), 1)
        second = asyncio.create_task(view.exit(delete_message=True))
        await self._settle(5)
        first.cancel()
        # A click lands in the tick the first exit is cut off.
        click = _make_interaction(message=MagicMock(id=message.id))
        pushing = asyncio.create_task(view.push(RenderableLayoutView, click))
        results = await asyncio.gather(first, second, pushing, return_exceptions=True)

        assert isinstance(results[0], asyncio.CancelledError)
        assert results[1] is True
        assert isinstance(results[2], RuntimeError) and "closing" in str(results[2])
        assert view._torn_down()
        message.delete.assert_awaited_once()

    async def test_an_exit_left_with_a_cancelled_send_keeps_the_view_closing(self):
        parked = asyncio.Event()

        class Panel(RenderableLayoutView):
            async def _retire_registration(self):
                if not parked.is_set():
                    parked.set()
                    await asyncio.Event().wait()
                await super()._retire_registration()

        interaction = _make_interaction()
        posting, gate = self._post_slowly(interaction)
        view = Panel(interaction=interaction)
        sending = asyncio.create_task(view.send())
        await asyncio.wait_for(posting.wait(), 1)
        assert await view.exit(delete_message=True) is True
        gate.set()
        await asyncio.wait_for(parked.wait(), 1)
        message = view._message
        sending.cancel()
        # A click lands in the tick the send carrying the exit is cut off.
        click = _make_interaction(message=MagicMock(id=message.id))
        pushing = asyncio.create_task(view.push(RenderableLayoutView, click))
        results = await asyncio.gather(sending, pushing, return_exceptions=True)
        await self._settle()

        assert isinstance(results[1], RuntimeError) and "closing" in str(results[1])
        assert view._torn_down()
        message.delete.assert_awaited_once()

    @pytest.mark.parametrize("close", ["exit", "timeout"])
    async def test_a_close_freezes_after_the_views_own_edit_lands(self, close):
        # An edit already in flight landing after the freeze put live-looking
        # buttons back on the closed view's message.
        view = await TestTeardownRunsOnce._live()
        message = view._message
        order = []
        editing, release = asyncio.Event(), asyncio.Event()

        async def edit(**kwargs):
            if not editing.is_set():
                editing.set()
                await release.wait()
                order.append("render")
            else:
                order.append("freeze")
            return message

        message.edit = AsyncMock(side_effect=edit)
        view._last_tree_digest = None
        rendering = asyncio.create_task(view.refresh())
        await asyncio.wait_for(editing.wait(), 1)
        if close == "exit":
            closing = asyncio.create_task(view.exit())
        else:
            view.stop()
            closing = asyncio.create_task(view.on_timeout())
        await self._settle()
        release.set()
        await rendering
        await closing

        assert order == ["render", "freeze"]

    @pytest.mark.parametrize("ticks", [0, 1, 2, 3])
    async def test_a_cancelled_close_waiting_on_another_is_cancelled(self, ticks):
        # Cancelled in the ticks after the first close lets go. Before Python
        # 3.12, wait_for could swallow that cancellation and run the close anyway.
        parked, release = asyncio.Event(), asyncio.Event()

        class Panel(RenderableLayoutView):
            async def _retire_registration(self):
                if not release.is_set():
                    parked.set()
                    await release.wait()
                await super()._retire_registration()

        view = Panel(interaction=_make_interaction())
        await view.send()
        first = asyncio.create_task(view.exit())
        await asyncio.wait_for(parked.wait(), 1)
        second = asyncio.create_task(view.on_timeout())
        await self._settle(5)
        release.set()
        for _ in range(ticks):
            await asyncio.sleep(0)
        if second.done():
            await first
            return
        second.cancel()
        await first
        with pytest.raises(asyncio.CancelledError):
            await second


class TestStoppedWhileSent:
    """A view stopped while it is being sent is not left posted and registered.

    discord.py routes no clicks to a stopped view, so a send that went on to
    post it left a message whose buttons answered nothing, on a view still
    registered and counted by its instance limit.
    """

    async def test_stopped_in_on_load_posts_nothing(self):
        class Stops(RenderableLayoutView):
            async def on_load(self):
                self.stop()

        interaction = _make_interaction()
        view = Stops(interaction=interaction)

        assert await view.send() is None
        interaction.response.send_message.assert_not_awaited()
        assert view._torn_down()
        assert view.id not in view.state_store._active_views

    async def test_stopped_after_loading_is_posted_then_frozen(self):
        class Stops(RenderableLayoutView):
            async def seed_initial_state(self, state):
                self.stop()

        interaction = _make_interaction()
        view = Stops(interaction=interaction)
        view.add_item(ActionRow(StatefulButton(label="Go", custom_id="go")))

        # Posted, so the command is answered, then frozen as a timeout leaves it.
        assert await view.send() is None
        interaction.response.send_message.assert_awaited_once()
        view._message.edit.assert_awaited_once()
        assert view._torn_down()
        assert view.id not in view.state_store._active_views

    async def test_stopped_after_posting_is_frozen(self):
        view = RenderableLayoutView(interaction=_make_interaction())
        view.add_item(ActionRow(StatefulButton(label="Go", custom_id="go")))
        record_message = view._update_message_state

        async def stop_then_record(message):
            view.stop()
            await record_message(message)

        view._update_message_state = stop_then_record

        assert await view.send() is None
        view._message.edit.assert_awaited_once()
        assert view._torn_down()
        assert view.id not in view.state_store._active_views


class TestTimeoutWindowStillRenders:
    """A view inside its timeout window is intact, so its render has to ship.

    discord.py resolves the stopped future *before* it calls ``on_timeout``,
    so ``is_finished()`` reports True for the whole window even though the
    view still holds its subscription and its message is still editable. An
    override that dispatches instead of delegating to ``super()`` expects the
    resulting notification to rebuild and edit; guarding these seams on
    ``is_finished()`` dropped that final render with nothing logged.
    """

    def _wire(self, view):
        message = MagicMock()
        message.id = 4242
        message.edit = AsyncMock()
        message.delete = AsyncMock()
        view._message = message
        return message

    def _pull_view(self, rebuilds):
        class _Pull(RenderableLayoutView):
            subscribed_actions = {"CARDPULL_UPDATED"}

            def build_ui(self):
                self.clear_items()
                self.add_item(TextDisplay("closed"))
                rebuilds.append(1)

        return _Pull(interaction=_make_interaction(), user_id=1, guild_id=2, timeout=60)

    async def test_a_stopped_view_still_rebuilds_on_a_notification(self):
        rebuilds = []
        view = self._pull_view(rebuilds)
        await view.send()
        self._wire(view)
        rebuilds.clear()

        # What discord.py does immediately before calling on_timeout.
        view.stop()
        assert view.is_finished()

        await view._handle_state_notification(
            view.state_store.state, {"type": "CARDPULL_UPDATED", "payload": {}}
        )

        assert rebuilds == [1]

    async def test_a_dispatch_from_an_on_timeout_override_reaches_the_message(self):
        """The reported shape, driven through discord.py's own timeout path."""
        done = asyncio.Event()

        class _Pull(RenderableLayoutView):
            subscribed_actions = {"CARDPULL_UPDATED"}

            def build_ui(self):
                self.clear_items()
                self.add_item(TextDisplay("page " + str(getattr(self, "page", 0))))

            async def on_timeout(self):
                # No super() call, so the view keeps its subscription.
                self.page = 3
                await self.dispatch("CARDPULL_UPDATED", {"value": self.page})
                done.set()

        view = _Pull(interaction=_make_interaction(), user_id=1, guild_id=2, timeout=60)
        await view.send()
        message = self._wire(view)

        view._dispatch_timeout()
        await asyncio.wait_for(done.wait(), timeout=5)

        message.edit.assert_awaited()

    async def test_a_torn_down_view_still_skips_the_rebuild(self):
        """The guard this replaces was added for a real bug; it has to hold."""
        rebuilds = []
        view = self._pull_view(rebuilds)
        await view.send()
        self._wire(view)
        rebuilds.clear()

        await view.exit(delete_message=False)
        await view._handle_state_notification(
            view.state_store.state, {"type": "CARDPULL_UPDATED", "payload": {}}
        )

        assert rebuilds == []

    async def test_a_stopped_view_still_queues_a_rate_limit_retry(self):
        view = RenderableLayoutView(interaction=_make_interaction(), user_id=1, guild_id=2)
        await view.send()
        self._wire(view)

        view.stop()
        view._ratelimit_not_before = time.monotonic() + 30
        view._schedule_backoff_retry()

        assert view._deferred_refresh_task is not None
        view._deferred_refresh_task.cancel()

    async def test_a_torn_down_view_queues_no_rate_limit_retry(self):
        view = RenderableLayoutView(interaction=_make_interaction(), user_id=1, guild_id=2)
        await view.send()
        self._wire(view)

        await view.exit(delete_message=False)
        view._ratelimit_not_before = time.monotonic() + 30
        view._schedule_backoff_retry()

        assert view._deferred_refresh_task is None


class TestSendingAgainClosesTheOldMessage:
    """A view sent again moves to the new message and closes the one it left.

    The old message kept its live buttons, routed to the same view, so a click
    there acted on the panel shown somewhere else. The message it left now
    closes as the view's exit() would close it: exit_policy decides.
    """

    @staticmethod
    def _posted_as(interaction, view):
        """Make the message ``interaction`` posts carry ``view``'s components."""
        from discord.components import _component_factory

        interaction.original_response.return_value.components = [
            _component_factory(payload) for payload in view.to_components()
        ]

    @staticmethod
    def _panel(cls=RenderableLayoutView, **attrs):
        panel_cls = type("Panel", (cls,), dict(attrs))
        view = panel_cls(interaction=_make_interaction())
        view.add_item(ActionRow(StatefulButton(label="Go", custom_id="go")))
        return view

    async def test_the_message_left_is_frozen_and_the_view_stays_live(self):
        view = self._panel()
        await view.send()
        old = view._message
        again = _make_interaction()
        self._posted_as(again, view)
        view.interaction = again

        assert await view.send() is not None

        shipped = old.edit.await_args.kwargs["view"]
        buttons = [item for item in shipped.walk_children() if isinstance(item, Button)]
        assert buttons and all(button.disabled for button in buttons)
        assert shipped.is_finished()
        old.delete.assert_not_awaited()
        # The live panel's own buttons are untouched.
        live = [item for item in view.walk_children() if isinstance(item, Button)]
        assert live and not any(button.disabled for button in live)
        assert view._message is not old and not view._torn_down()

    async def test_the_message_left_is_deleted_under_a_delete_exit_policy(self):
        view = self._panel(exit_policy="delete")
        await view.send()
        old = view._message
        view.interaction = _make_interaction()

        assert await view.send() is not None
        old.delete.assert_awaited_once()
        old.edit.assert_not_awaited()

    async def test_a_v1_message_left_loses_its_buttons(self):
        from cascadeui.views.view import StatefulView

        view = StatefulView(interaction=_make_interaction())
        view.add_item(StatefulButton(label="Go", custom_id="go"))
        await view.send(content="panel")
        old = view._message
        view.interaction = _make_interaction()

        assert await view.send(content="panel") is not None
        old.edit.assert_awaited_once_with(view=None)

    async def test_a_send_again_that_fails_leaves_the_old_message_alone(self):
        view = self._panel()
        await view.send()
        old = view._message
        failing = _make_interaction()
        failing.response.send_message = AsyncMock(
            side_effect=discord.HTTPException(MagicMock(status=500, reason="x"), "boom")
        )
        view.interaction = failing

        with pytest.raises(discord.HTTPException):
            await view.send()
        old.edit.assert_not_awaited()
        old.delete.assert_not_awaited()
        assert view._message is old and not view._torn_down()

    async def test_discord_py_stops_routing_and_syncing_the_message_left(self):
        # Left tracked, the freeze's own gateway echo was fed back into the live
        # view's items and disabled the panel's buttons.
        from discord.ui.view import ViewStore

        view = self._panel()
        await view.send()
        old = view._message
        store = ViewStore(MagicMock())
        store.add_view(view, old.id)
        again = _make_interaction()
        again.original_response.return_value.id = 1234
        self._posted_as(again, view)
        view.interaction = again

        await view.send()

        assert not store.is_message_tracked(old.id)
        assert old.id not in store._views


class TestSendingAgainReleasesTheOldMessage:
    """The re-send close against discord.py's own view store.

    Messages here act as discord.py's do: an edit registers a live view with
    the store under that message, and the gateway feeds each edit of a
    tracked message back into its view.
    """

    @staticmethod
    def _message(message_id, store):
        message = MagicMock(id=message_id, channel=MagicMock(id=888))
        message.hold = None

        async def edit(**kwargs):
            if message.hold is not None:
                hold, message.hold = message.hold, None
                await hold.wait()
            view = kwargs.get("view")
            if view is not None and not view.is_finished() and view.is_dispatchable():
                store.add_view(view, message_id)
            if view is not None and store.is_message_tracked(message_id):
                store.update_from_message(message_id, view.to_components())
            return message

        message.edit = AsyncMock(side_effect=edit)
        message.delete = AsyncMock()
        return message

    async def _sent(self, store, message_id=999):
        interaction = _make_interaction()
        interaction.original_response = AsyncMock(return_value=self._message(message_id, store))
        view = RenderableLayoutView(interaction=interaction)
        view.add_item(ActionRow(StatefulButton(label="Go", custom_id="go")))
        await view.send()
        store.add_view(view, message_id)
        return view

    def _posting_to(self, view, store, message_id):
        interaction = _make_interaction()
        message = self._message(message_id, store)
        interaction.original_response = AsyncMock(return_value=message)

        def post(*args, **kwargs):
            interaction.response.is_done.return_value = True
            store.add_view(view, message_id)

        interaction.response.send_message = AsyncMock(side_effect=post)
        view.interaction = interaction
        return message

    async def test_an_edit_of_the_old_message_in_flight_does_not_route_it_back(self):
        # It registered the view on the old message again once it landed, and
        # the freeze's echo then disabled the live panel's buttons.
        from discord.ui.view import ViewStore

        store = ViewStore(MagicMock())
        view = await self._sent(store)
        old = view._message
        released = asyncio.Event()
        old.hold = released
        view.add_item(TextDisplay("changed"))
        rendering = asyncio.create_task(view.refresh())
        await until(lambda: old.hold is None)
        new = self._posting_to(view, store, 1001)
        sending = asyncio.create_task(view.send())
        for _ in range(50):
            if view._message is new:
                break
            await asyncio.sleep(0)
        released.set()
        await asyncio.wait_for(sending, 1)
        await rendering

        assert view._cache_key == 1001
        assert not store.is_message_tracked(999)
        assert 999 not in store._views
        buttons = [item for item in view.walk_children() if isinstance(item, Button)]
        assert buttons and not any(button.disabled for button in buttons)

    async def test_a_view_stopped_while_it_is_sent_again_still_releases_the_old_message(self):
        # stop() detaches the view from the store, so the release found none.
        from discord.ui.view import ViewStore

        store = ViewStore(MagicMock())
        view = await self._sent(store)
        self._posting_to(view, store, 1001)
        record_message = view._update_message_state

        async def stop_then_record(message):
            view.stop()
            await record_message(message)

        view._update_message_state = stop_then_record

        await view.send()

        assert not store.is_message_tracked(999)
        assert 999 not in store._views

    async def test_releasing_the_old_message_keeps_another_views_entries(self):
        from discord.ui.view import ViewStore

        store = ViewStore(MagicMock())
        view = await self._sent(store)
        other = RenderableLayoutView(interaction=_make_interaction())
        other.add_item(ActionRow(StatefulButton(label="Other", custom_id="other")))
        store.add_view(other, 999)
        self._posting_to(view, store, 1001)

        await view.send()

        entries = store._views.get(999, {})
        assert entries and all(item.view is other for item in entries.values())
        assert store.is_message_tracked(999)

    async def test_a_send_cancelled_while_an_old_edit_is_in_flight_releases_the_old_message(
        self,
    ):
        # The cancel cut the send while it waited for the edit, the edit then
        # landed and routed the old message to the view, and nothing released
        # it: clicks there acted on the panel shown on the new message.
        from discord.ui.view import ViewStore

        store = ViewStore(MagicMock())
        view = await self._sent(store)
        old = view._message
        released = asyncio.Event()
        old.hold = released
        view.add_item(TextDisplay("changed"))
        rendering = asyncio.create_task(view.refresh())
        await until(lambda: old.hold is None)
        self._posting_to(view, store, 1001)
        waiting = asyncio.Event()
        wait_for_own_edits = view._wait_for_own_edits

        async def mark_the_wait(*args, **kwargs):
            waiting.set()
            return await wait_for_own_edits(*args, **kwargs)

        view._wait_for_own_edits = mark_the_wait
        sending = asyncio.create_task(view.send())
        await asyncio.wait_for(waiting.wait(), 1)
        sending.cancel()
        released.set()
        with pytest.raises(asyncio.CancelledError):
            await sending
        await rendering

        assert view._cache_key == 1001
        assert not store.is_message_tracked(999)
        assert 999 not in store._views

    async def test_an_old_edit_landing_while_the_new_message_is_fetched_leaves_no_route(self):
        # Landing before the send had the new message, the edit keyed the view
        # to the old one, and nothing moved it back: after stop() the new
        # message's entries stayed in the store.
        from discord.ui.view import ViewStore

        store = ViewStore(MagicMock())
        view = await self._sent(store)
        old = view._message
        released = asyncio.Event()
        old.hold = released
        view.add_item(TextDisplay("changed"))
        rendering = asyncio.create_task(view.refresh())
        await until(lambda: old.hold is None)
        new = self._posting_to(view, store, 1001)
        posted = MagicMock(spec=discord.InteractionMessage)
        posted.id = 1001

        async def fetch_message(message_id):
            released.set()
            await until(lambda: getattr(view, "_cache_key", None) == 999)
            return new

        posted.channel.fetch_message = fetch_message
        view.interaction.original_response = AsyncMock(return_value=posted)

        await view.send()
        await rendering
        view.stop()

        assert 1001 not in store._views
        assert not store.is_message_tracked(1001)

    @pytest.mark.parametrize("cut", ["raise", "hold_ran_out"])
    async def test_a_send_whose_steps_are_cut_short_still_releases_the_old_message(
        self, cut, monkeypatch
    ):
        # Releasing the old message was the first thing closing it did, and a
        # step failing before it, or a cancel held past its limit, skipped it.
        from discord.ui.view import ViewStore

        import cascadeui.views.base as base

        monkeypatch.setattr(base, "_CANCEL_HOLD_SECONDS", 0.05)
        store = ViewStore(MagicMock())
        view = await self._sent(store)
        self._posting_to(view, store, 1001)
        record_message = view._update_message_state

        async def fail(message):
            await record_message(message)
            raise RuntimeError("the store is down")

        async def stuck(message):
            await asyncio.Event().wait()

        if cut == "raise":
            view._update_message_state = fail
            await view.send()
        else:
            inside = asyncio.Event()

            async def hold(action, state):
                inside.set()
                await asyncio.Event().wait()

            view.state_store.on("view_updated", hold)
            view._after_send = stuck
            sending = asyncio.create_task(view.send())
            try:
                await asyncio.wait_for(inside.wait(), 1)
                sending.cancel()
                done, _ = await asyncio.wait({sending}, timeout=2)
                assert sending in done
            finally:
                view.state_store.off("view_updated", hold)
                sending.cancel()

        assert not store.is_message_tracked(999)
        assert 999 not in store._views


class TestSendingAgainShowsWhatWasShown:
    async def test_the_message_left_is_frozen_as_it_was_shown(self):
        # Frozen from the new message, a public panel sent again for one user
        # showed that user's private render to everyone.
        class Balance(RenderableLayoutView):
            viewer = "everyone"

            async def on_load(self):
                self.clear_items()
                self.add_item(TextDisplay(f"balance shown to: {self.viewer}"))
                self.add_item(ActionRow(StatefulButton(label="Go", custom_id="go")))

        view = Balance(interaction=_make_interaction())
        await view.send()
        old = view._message
        view.viewer = "user 42 only"
        view.interaction = _make_interaction()

        await view.send(ephemeral=True)

        shipped = old.edit.await_args.kwargs["view"]
        texts = [item.content for item in shipped.walk_children() if isinstance(item, TextDisplay)]
        assert texts == ["balance shown to: everyone"]

    @staticmethod
    def _shown_on(message, since):
        return [
            [
                item.content
                for item in call.kwargs["view"].walk_children()
                if isinstance(item, TextDisplay)
            ]
            for call in message.edit.await_args_list[since:]
        ]

    @pytest.mark.parametrize("renderer", ["the send's own on_load()", "another task's reload()"])
    async def test_no_render_during_the_send_reaches_the_message_left(self, renderer):
        # Both edited the message being left with what the send rebuilt for
        # the new viewer, before the freeze put the old content back.
        registering = asyncio.Event()
        resume = asyncio.Event()

        class Balance(RenderableLayoutView):
            viewer = "everyone"
            refresh_in_load = False

            async def on_load(self):
                self.clear_items()
                self.add_item(TextDisplay(f"balance shown to: {self.viewer}"))
                self.add_item(ActionRow(StatefulButton(label="Go", custom_id="go")))
                if self.refresh_in_load:
                    await self.refresh()

            async def seed_initial_state(self, state):
                if self.viewer != "everyone":
                    registering.set()
                    await resume.wait()

        view = Balance(interaction=_make_interaction())
        await view.send()
        old = view._message
        since = old.edit.await_count
        view.viewer = "user 42 only"
        view.interaction = _make_interaction()

        if renderer == "the send's own on_load()":
            view.refresh_in_load = True
            resume.set()
            await view.send(ephemeral=True)
        else:
            sending = asyncio.create_task(view.send(ephemeral=True))
            await asyncio.wait_for(registering.wait(), 5)
            assert await view.reload() == "deferred"
            resume.set()
            await asyncio.wait_for(sending, 5)

        assert self._shown_on(old, since) == [["balance shown to: everyone"]]

    async def test_a_handle_on_the_old_message_is_dropped(self):
        # An embed refresh went through it and repainted the message left.
        from cascadeui.views.view import StatefulView

        view = StatefulView(interaction=_make_interaction())
        view.add_item(StatefulButton(label="Go", custom_id="go"))
        await view.send(content="panel")
        old = view._message
        view._webhook_message = old
        view.interaction = _make_interaction()

        await view.send(content="panel", ephemeral=True)

        assert view._webhook_message is None


class TestASendCancelledAfterItPostsFinishes:
    """A send cancelled once Discord accepted its message does the rest, then raises."""

    @staticmethod
    async def _cancel_in_view_updated(view):
        store = view.state_store
        inside = asyncio.Event()

        async def slow(action, state):
            inside.set()
            await asyncio.Event().wait()

        store.on("view_updated", slow)
        sending = asyncio.create_task(view.send())
        try:
            await asyncio.wait_for(inside.wait(), 1)
            sending.cancel()
            # Longer than the send's hold on a cancel (5s), so a regression
            # fails here instead of hanging the run.
            done, _ = await asyncio.wait({sending}, timeout=10)
            assert sending in done
            assert sending.cancelled()
        finally:
            store.off("view_updated", slow)

    async def test_a_close_recorded_during_the_send_does_not_outlast_the_hold(self, monkeypatch):
        # Carried out in the cancelled send once its hold had run out, the
        # close kept the caller waiting on its own edit, up to edit_timeout.
        import cascadeui.views.base as base_module

        monkeypatch.setattr(base_module, "_CANCEL_HOLD_SECONDS", 0.2)
        view = RenderableLayoutView(interaction=_make_interaction())
        view.add_item(ActionRow(StatefulButton(label="Go", custom_id="go")))
        await view.send()
        old = view._message
        release = asyncio.Event()

        async def stall(**kwargs):
            await release.wait()

        old.edit = AsyncMock(side_effect=stall)
        again = _make_interaction()
        again.original_response.return_value.id = 200
        again.original_response.return_value.edit = AsyncMock(side_effect=stall)
        view.interaction = again
        recording = asyncio.Event()

        async def parked(message):
            recording.set()
            await asyncio.Event().wait()

        view._update_message_state = parked
        sending = asyncio.create_task(view.send())
        try:
            await asyncio.wait_for(recording.wait(), 5)
            await view.exit()
            sending.cancel()
            # The hold runs out in the edit of the message left, and the close
            # recorded above edits the new message, which stalls as well.
            done, _ = await asyncio.wait({sending}, timeout=5)
            assert sending in done
            assert sending.cancelled()
        finally:
            release.set()
        await until(view._torn_down)

    async def test_a_persistent_panel_is_registered(self):
        # The cancel skipped the registration, so the live panel had no row
        # and did not come back after a restart.
        from cascadeui import PersistentLayoutView

        class Board(PersistentLayoutView):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.add_item(TextDisplay("board"))

        panel = Board(interaction=_make_interaction(), persistence_key="post-cancel")
        await self._cancel_in_view_updated(panel)

        assert "post-cancel" in panel.state_store.state["persistent_views"]
        assert not panel._torn_down()

    async def test_a_view_sent_with_a_parent_is_attached(self):
        parent = RenderableLayoutView(interaction=_make_interaction())
        await parent.send()
        child = RenderableLayoutView(interaction=_make_interaction(), parent=parent)
        await self._cancel_in_view_updated(child)

        assert child in parent._attached_children

    async def test_a_view_sent_again_closes_the_message_it_left(self):
        class Panel(RenderableLayoutView):
            async def on_load(self):
                self.clear_items()
                self.add_item(ActionRow(StatefulButton(label="Go", custom_id="go")))

        view = Panel(interaction=_make_interaction())
        await view.send()
        old = view._message
        view.interaction = _make_interaction()
        await self._cancel_in_view_updated(view)

        assert old.edit.await_count == 1

    async def test_a_cancel_while_the_posted_message_is_read_back_leaves_the_view_live(self):
        # The read sat inside the send's rollback, so the posted message was
        # left on screen with a view torn down behind it.
        interaction = _make_interaction()
        reading = asyncio.Event()
        posted = MagicMock()

        async def original_response():
            if not reading.is_set():
                reading.set()
                await asyncio.Event().wait()
            return posted

        interaction.original_response = original_response
        view = RenderableLayoutView(interaction=interaction)
        sending = asyncio.create_task(view.send())
        await asyncio.wait_for(reading.wait(), 1)
        sending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await sending

        assert not view._torn_down()
        assert view._message is posted

    async def test_a_cancel_during_the_refetch_still_finishes_the_send(self):
        # The cancel went on from the re-fetch, past everything the send does
        # afterwards, so the view kept its message and was never attached.
        parent = RenderableLayoutView(interaction=_make_interaction())
        await parent.send()
        interaction = _make_interaction()
        fetching = asyncio.Event()
        posted = MagicMock(spec=discord.InteractionMessage)

        async def fetch_message(message_id):
            fetching.set()
            await asyncio.Event().wait()

        posted.channel.fetch_message = fetch_message
        interaction.original_response = AsyncMock(return_value=posted)
        child = RenderableLayoutView(interaction=interaction, parent=parent)
        sending = asyncio.create_task(child.send())
        await asyncio.wait_for(fetching.wait(), 1)
        sending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await sending

        assert child._message is posted
        assert child in parent._attached_children

    @staticmethod
    def _board(key, retire_previous=True):
        from cascadeui import PersistentLayoutView

        class Board(PersistentLayoutView):
            retire_previous_on_send = retire_previous

            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.add_item(TextDisplay("board"))
                # A control, so closing the message the board left edits it.
                self.add_item(ActionRow(StatefulButton(label="Go", custom_id="board_go")))

        return Board(interaction=_make_interaction(), persistence_key=key)

    @staticmethod
    def _blocking(store, action_type):
        """Hook ``action_type`` to wait until cancelled; return the event it sets."""
        inside = asyncio.Event()

        async def wait(action, state):
            inside.set()
            await asyncio.Event().wait()

        store.on(action_type, wait)
        return inside, wait

    async def test_a_held_cancel_goes_through_once_the_hold_runs_out(self, monkeypatch, caplog):
        # Held with no limit, a step that never returned kept the cancel, and a
        # program ending with its bot still open never exited.
        import cascadeui.views.base as base

        monkeypatch.setattr(base, "_CANCEL_HOLD_SECONDS", 0.05)
        panel = self._board("held-too-long")
        store = panel.state_store
        inside, slow = self._blocking(store, "view_updated")
        _, stuck = self._blocking(store, "persistent_view_registered")
        sending = asyncio.create_task(panel.send())
        try:
            with caplog.at_level("WARNING", logger="cascadeui"):
                await asyncio.wait_for(inside.wait(), 1)
                sending.cancel()
                done, _ = await asyncio.wait({sending}, timeout=2)

            assert sending in done
            assert sending.cancelled()
            # The steps it cut are named, since nothing else says the panel
            # may be unregistered.
            assert any(
                "did not finish within" in r.getMessage()
                for r in caplog.records
                if r.name.startswith("cascadeui")
            )
        finally:
            store.off("view_updated", slow)
            store.off("persistent_view_registered", stuck)
            sending.cancel()

    @pytest.mark.skipif(sys.version_info < (3, 11), reason="asyncio.timeout() is 3.11+")
    async def test_a_timeout_around_the_send_still_reports_timeouterror(self, monkeypatch):
        # The hold's own cancel counted against the caller's timeout, which
        # then raised CancelledError instead of TimeoutError.
        import cascadeui.views.base as base

        monkeypatch.setattr(base, "_CANCEL_HOLD_SECONDS", 0.05)
        panel = self._board("timed-out")
        store = panel.state_store
        _, slow = self._blocking(store, "view_updated")
        _, stuck = self._blocking(store, "persistent_view_registered")
        outcome = []

        async def caller():
            try:
                async with asyncio.timeout(0.02):
                    await panel.send()
            except TimeoutError:
                outcome.append("timeout")
            outcome.append(asyncio.current_task().cancelling())

        calling = asyncio.create_task(caller())
        try:
            done, _ = await asyncio.wait({calling}, timeout=2)

            assert calling in done
            assert outcome == ["timeout", 0]
        finally:
            store.off("view_updated", slow)
            store.off("persistent_view_registered", stuck)
            calling.cancel()

    async def test_a_step_that_swallows_the_cancel_ending_the_hold_ends_the_send(self, monkeypatch):
        # A hook that caught the cancel the hold ran out on returned normally,
        # and the steps after it ran with nothing left to stop them.
        import cascadeui.views.base as base

        monkeypatch.setattr(base, "_CANCEL_HOLD_SECONDS", 0.05)
        panel = self._board("swallowed")
        await panel.send()
        old = panel._message

        async def never(*args, **kwargs):
            await asyncio.Event().wait()

        old.edit = AsyncMock(side_effect=never)
        old.delete = AsyncMock(side_effect=never)
        panel.interaction = _make_interaction()
        panel.interaction.original_response.return_value.id = 1001
        store = panel.state_store
        inside, slow = self._blocking(store, "view_updated")

        async def swallow(action, state):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                pass

        store.on("persistent_view_registered", swallow)
        sending = asyncio.create_task(panel.send())
        try:
            await asyncio.wait_for(inside.wait(), 1)
            sending.cancel()
            done, _ = await asyncio.wait({sending}, timeout=2)

            assert sending in done
        finally:
            store.off("view_updated", slow)
            store.off("persistent_view_registered", swallow)
            sending.cancel()

    @staticmethod
    def _resend_through(panel, gate):
        panel.interaction = _make_interaction()
        panel.interaction.original_response.return_value.id = 1001
        panel.state_store._add_middleware(gate)

    async def test_a_registration_cut_short_by_a_cancel_leaves_no_row_on_the_message_left(
        self, caplog
    ):
        # The row still named the message the send had closed, so a restart
        # restored the panel there with nothing to click; only a later exit()
        # removed it, and a panel is rarely exited.
        panel = self._board("cut-registration")
        await panel.send()
        store = panel.state_store
        entered = asyncio.Event()

        async def gate(action, state, next_fn):
            if action["type"] == "PERSISTENT_VIEW_REGISTERED":
                entered.set()
                await asyncio.Event().wait()
            return await next_fn(action, state)

        self._resend_through(panel, gate)
        try:
            with caplog.at_level("WARNING", logger="cascadeui"):
                sending = asyncio.create_task(panel.send())
                await asyncio.wait_for(entered.wait(), 1)
                sending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await sending
        finally:
            store._middleware.remove(gate)

        assert "cut-registration" not in store.state["persistent_views"]
        assert not panel._torn_down()
        warned = [r.getMessage() for r in caplog.records if r.name.startswith("cascadeui")]
        assert any(
            "CancelledError cut off registering" in m and "will not be reattached" in m
            for m in warned
        ), warned
        await panel.exit()

    async def test_a_panel_stopped_while_it_is_sent_again_leaves_no_row_on_the_message_it_left(
        self,
    ):
        # The stopped panel skipped its registration and both messages were
        # frozen, while the row kept naming the old one: a restart restored
        # the panel onto a frozen message.
        panel = self._board("stopped-resend")
        await panel.send()
        store = panel.state_store
        old = panel._message

        async def stop(action, state):
            panel.stop()

        panel.interaction = _make_interaction()
        panel.interaction.original_response.return_value.id = 1001
        store.on("view_updated", stop)
        try:
            await panel.send()
        finally:
            store.off("view_updated", stop)

        assert "stopped-resend" not in store.state["persistent_views"]
        # The message it left was closed, as a re-send closes it.
        assert old.edit.await_count == 1

    async def test_a_registration_cut_short_by_a_cancel_ends_within_the_hold(self, monkeypatch):
        # The row cleanup ran before the send's bound on a cancel had started,
        # so a middleware waiting on it kept the cancelled send, and a program
        # ending then, from ever finishing.
        import cascadeui.views.base as base

        monkeypatch.setattr(base, "_CANCEL_HOLD_SECONDS", 0.05)
        panel = self._board("cut-and-held")
        await panel.send()
        store = panel.state_store
        entered = asyncio.Event()

        async def gate(action, state, next_fn):
            if action["type"] in ("PERSISTENT_VIEW_REGISTERED", "PERSISTENT_VIEW_UNREGISTERED"):
                entered.set()
                await asyncio.Event().wait()
            return await next_fn(action, state)

        self._resend_through(panel, gate)
        sending = asyncio.create_task(panel.send())
        try:
            await asyncio.wait_for(entered.wait(), 1)
            sending.cancel()
            done, _ = await asyncio.wait({sending}, timeout=2)

            assert sending in done
        finally:
            store._middleware.remove(gate)
            sending.cancel()

    @pytest.mark.parametrize("ending", ["exit", "stop"])
    async def test_an_opted_out_panel_ended_while_sent_again_hands_the_key_back(self, ending):
        # The re-send removed the panel's row before its exit could hand it
        # back, so the earlier panel, still live under the key, had nothing to
        # restore it after a restart.
        earlier = self._board("handed-back", retire_previous=False)
        earlier.interaction.original_response.return_value.id = 2001
        await earlier.send()
        panel = type(earlier)(interaction=_make_interaction(), persistence_key="handed-back")
        panel.interaction.original_response.return_value.id = 2002
        await panel.send()
        store = panel.state_store
        assert store.state["persistent_views"]["handed-back"]["message_id"] == "2002"
        exiting = []

        async def end(action, state):
            if ending == "stop":
                panel.stop()
                return
            exiting.append(asyncio.ensure_future(panel.exit()))
            for _ in range(5):
                await asyncio.sleep(0)

        panel.interaction = _make_interaction()
        panel.interaction.original_response.return_value.id = 2003
        store.on("view_updated", end)
        try:
            await panel.send()
            for task in exiting:
                await asyncio.wait_for(task, 5)
        finally:
            store.off("view_updated", end)

        assert panel.is_finished()
        assert store.state["persistent_views"]["handed-back"]["message_id"] == "2001"
        await earlier.exit()

    async def test_a_panel_whose_new_message_is_gone_still_closes_the_message_it_left(self, caplog):
        # The row cleanup read the new message's id after a render had found
        # that message deleted and cleared it, and raised: the message the
        # panel left stayed open.
        panel = self._board("gone-resend")
        await panel.send()
        store = panel.state_store
        old = panel._message

        async def gone(action, state):
            panel._message.edit = AsyncMock(
                side_effect=discord.NotFound(MagicMock(status=404), "gone")
            )
            panel.add_item(TextDisplay("changed"))
            await panel.refresh()
            panel.stop()

        panel.interaction = _make_interaction()
        panel.interaction.original_response.return_value.id = 1001
        store.on("view_updated", gone)
        try:
            with caplog.at_level("ERROR", logger="cascadeui"):
                await panel.send()
        finally:
            store.off("view_updated", gone)

        assert panel._message is None  # the render found the new message gone
        assert old.edit.await_count == 1
        assert "gone-resend" not in store.state["persistent_views"]
        assert not [r for r in caplog.records if r.name.startswith("cascadeui")]

    @pytest.mark.parametrize("raised", ["before_the_commit", "after_the_commit"])
    async def test_a_re_registration_that_raises_says_where_the_panel_is_registered(
        self, raised, caplog
    ):
        # The row still named the message the send had just closed, so a
        # restart restored the panel there with nothing to click, and code
        # checking whether the panel was up found it; the error said the panel
        # would not be reattached either way.
        panel = self._board("raised-registration")
        await panel.send()
        store = panel.state_store

        async def gate(action, state, next_fn):
            if action["type"] == "PERSISTENT_VIEW_REGISTERED" and raised == "before_the_commit":
                raise RuntimeError("audit log is down")
            result = await next_fn(action, state)
            if action["type"] == "PERSISTENT_VIEW_REGISTERED":
                raise RuntimeError("audit log is down")
            return result

        self._resend_through(panel, gate)
        try:
            with caplog.at_level("ERROR", logger="cascadeui"):
                await panel.send()
        finally:
            store._middleware.remove(gate)

        errors = [r.getMessage() for r in caplog.records if r.name.startswith("cascadeui")]
        if raised == "before_the_commit":
            assert "raised-registration" not in store.state["persistent_views"]
            assert any("will not be reattached after a restart" in m for m in errors)
        else:
            entry = store.state["persistent_views"]["raised-registration"]
            assert entry["message_id"] == "1001"
            assert any("it is registered" in m for m in errors)

    async def test_a_panel_whose_re_registration_raises_moves_no_other_key(self):
        # Another key naming the panel's message (a key renamed in place) was
        # moved to the new message although the panel's own registration had
        # not reached it, so a restart restored the old key's row there.
        from cascadeui.state.actions import ActionCreators

        panel = self._board("renamed")
        await panel.send()
        store = panel.state_store
        old = str(panel._message.id)
        await store.dispatch(
            "PERSISTENT_VIEW_REGISTERED",
            ActionCreators.persistent_view_registered(
                "old-name", type(panel).__name__, old, str(panel._message.channel.id)
            ),
        )

        async def gate(action, state, next_fn):
            if (
                action["type"] == "PERSISTENT_VIEW_REGISTERED"
                and action["payload"]["persistence_key"] == "renamed"
            ):
                raise RuntimeError("audit log is down")
            return await next_fn(action, state)

        self._resend_through(panel, gate)
        try:
            await panel.send()
        finally:
            store._middleware.remove(gate)

        registry = store.state["persistent_views"]
        assert registry.get("old-name", {}).get("message_id") != "1001"
        # Nor may either row stay on the message the send closed: a restart
        # would restore a panel there with nothing to click.
        assert [key for key, entry in registry.items() if entry.get("message_id") == old] == []

    async def test_a_view_stopped_during_its_send_starts_no_refresh_timer(self):
        # A view stopped meanwhile, which its bot's close does to every view,
        # still got a timer that slept toward a hand-off it could never make,
        # and a program ending then printed a pending task.
        timers = []

        class Panel(RenderableLayoutView):
            auto_refresh_ephemeral = True

            def _schedule_ephemeral_refresh(self):
                timers.append(self)

        view = Panel(interaction=_make_interaction(), timeout=None)
        record_message = view._update_message_state

        async def stop_then_record(message):
            view.stop()
            await record_message(message)

        view._update_message_state = stop_then_record
        await view.send(ephemeral=True)
        for _ in range(5):
            await asyncio.sleep(0)

        assert timers == []

    async def test_a_view_stopped_while_it_is_sent_again_restarts_no_refresh_timer(self):
        # The rolled-back re-send put the old message's timer back for a view
        # already stopped, which its bot's close does to every view.
        timers = []

        class Panel(RenderableLayoutView):
            auto_refresh_ephemeral = True
            stop_while_loading = False

            def _schedule_ephemeral_refresh(self):
                timers.append(self)

            async def on_load(self):
                if self.stop_while_loading:
                    self.stop()

        view = Panel(interaction=_make_interaction(), timeout=None)
        await view.send(ephemeral=True)
        assert len(timers) == 1
        view.stop_while_loading = True
        view.interaction = _make_interaction()

        await view.send(ephemeral=True)

        assert len(timers) == 1

    async def test_a_cancel_cutting_the_close_of_the_message_left_still_freezes_it(self):
        """The cut close left the old message with live-looking controls that
        no longer routed to the view, so a click there failed."""
        view = RenderableLayoutView(interaction=_make_interaction())
        view.add_item(ActionRow(StatefulButton(label="Go", custom_id="go")))
        await view.send()
        old = view._message
        freezing = asyncio.Event()

        async def slow_edit(**kwargs):
            freezing.set()
            await asyncio.sleep(0.05)

        old.edit = AsyncMock(side_effect=slow_edit)
        again = _make_interaction()
        again.original_response.return_value.id = 200
        view.interaction = again
        sending = asyncio.create_task(view.send())
        await asyncio.wait_for(freezing.wait(), 5)
        sending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await sending
        await until(lambda: old.edit.await_count == 2)

        frozen = old.edit.call_args.kwargs["view"]
        buttons = [i for i in frozen.walk_children() if isinstance(i, discord.ui.Button)]
        assert buttons and all(b.disabled for b in buttons)
        assert view._message is not old and not view.is_finished()


class TestThePostedMessageComesWithTheResponse:
    """discord.py returns the posted message from ``send_message()``. The send
    read it back with a second request, where a failure rolled back a send
    that had already posted, leaving a message whose buttons answered nothing.

    The shared ``make_interaction()`` returns nothing from ``send_message()``,
    so the other send tests take the read-back; everything after it treats the
    message the same whichever way it came."""

    @staticmethod
    def _answering_with(posted):
        interaction = _make_interaction()
        answered = interaction.response.send_message.side_effect

        async def send_message(*args, **kwargs):
            answered(*args, **kwargs)
            return MagicMock(resource=posted)

        interaction.response.send_message = AsyncMock(side_effect=send_message)
        interaction.original_response = AsyncMock(
            side_effect=discord.HTTPException(MagicMock(status=503, reason="x"), "unavailable")
        )
        return interaction

    async def test_a_send_does_not_read_its_message_back(self):
        posted = MagicMock(spec=discord.InteractionMessage)
        posted.id = 1234
        fetched = MagicMock(id=1234, channel=MagicMock(id=888))
        posted.channel.fetch_message = AsyncMock(return_value=fetched)
        interaction = self._answering_with(posted)
        view = RenderableLayoutView(interaction=interaction)

        assert await view.send() is fetched
        interaction.original_response.assert_not_awaited()
        assert not view._torn_down()

    async def test_a_message_whose_channel_is_not_resolved_is_kept(self):
        # discord.py leaves the channel None for a channel type it cannot
        # build, and the re-fetch through it raised out of a send that had
        # posted, skipping the rest of the send.
        parent = RenderableLayoutView(interaction=_make_interaction())
        await parent.send()
        posted = MagicMock(spec=discord.InteractionMessage)
        posted.id = 1234
        posted.channel = None
        child = RenderableLayoutView(interaction=self._answering_with(posted), parent=parent)

        assert await child.send() is posted
        assert child._message is posted
        assert child in parent._attached_children

    async def test_an_ephemeral_send_keeps_the_message_the_response_carries(self):
        posted = MagicMock(spec=discord.InteractionMessage)
        posted.id = 1234
        interaction = self._answering_with(posted)
        view = RenderableLayoutView(interaction=interaction)

        assert await view.send(ephemeral=True) is posted
        interaction.original_response.assert_not_awaited()
        assert view._message is posted


class TestRolledBackSendsAndCutOffCloses:
    @staticmethod
    async def _settle(n=40):
        for _ in range(n):
            await asyncio.sleep(0)

    async def test_two_exits_cancelled_together_leave_the_view_live(self):
        # The cut-off holder left the view to the exit waiting for the turn,
        # which was cut off too, and nothing reset _closing: the panel
        # refused every click and navigation from then on.
        parked = asyncio.Event()

        class Child(RenderableLayoutView):
            async def _retire_registration(self):
                parked.set()
                await asyncio.Event().wait()

        parent = RenderableLayoutView(interaction=_make_interaction())
        await parent.send()
        child = Child(interaction=_make_interaction(), parent=parent)
        await child.send()
        first = asyncio.create_task(parent.exit())
        await asyncio.wait_for(parked.wait(), 1)
        second = asyncio.create_task(parent.exit())
        await self._settle()
        assert parent._close_waiting(), "the second exit is not waiting for the turn"
        first.cancel()
        second.cancel()
        for task in (first, second):
            with pytest.raises(asyncio.CancelledError):
                await task

        assert not parent.is_finished()
        assert not parent._closing, "the panel stayed closing with no close left to finish it"

    async def test_a_waiter_cut_off_before_a_resumed_close_starts_leaves_the_view_closing(self):
        # The holder was cut off with a close left to it, which resumes on a
        # task of its own. A waiter cut off in the same step, before that task
        # started, handed the view back, so its caller could navigate a view
        # the resumed close was about to tear down.
        from cascadeui.views.base import _CLOSE_RESUME_TASK_OWNER

        parked = asyncio.Event()
        seen = []

        class Child(RenderableLayoutView):
            async def _retire_registration(self):
                if not parked.is_set():
                    # From the holder's own task, so it is left with the holder.
                    await parent.exit()
                    parked.set()
                    await asyncio.Event().wait()

        async def waiting_exit():
            try:
                await parent.exit()
            except asyncio.CancelledError:
                seen.append(parent._closed())
                raise

        parent = RenderableLayoutView(interaction=_make_interaction())
        await parent.send()
        child = Child(interaction=_make_interaction(), parent=parent)
        await child.send()
        first = asyncio.create_task(parent.exit())
        await asyncio.wait_for(parked.wait(), 1)
        second = asyncio.create_task(waiting_exit())
        await self._settle()
        assert parent._close_waiting(), "the second exit is not waiting for the turn"
        first.cancel()
        second.cancel()
        for task in (first, second):
            with pytest.raises(asyncio.CancelledError):
                await task
        await asyncio.wait_for(parent.task_manager.wait_tasks(_CLOSE_RESUME_TASK_OWNER), 2)

        assert seen == [True], "the waiter handed back a view a resumed close still owed"
        assert parent.is_finished(), "the resumed close did not finish the exit"

    async def test_a_resumed_close_that_fails_leaves_the_view_live(self):
        # A resumed close counted as waiting for the turn while it held it, so
        # when its own teardown raised, nothing handed the view back: it stayed
        # closing, refusing every click and navigation, with no close left.
        from cascadeui.views.base import _CLOSE_RESUME_TASK_OWNER

        parked = asyncio.Event()

        class Child(RenderableLayoutView):
            async def _retire_registration(self):
                if not parked.is_set():
                    await parent.exit()
                    parked.set()
                    await asyncio.Event().wait()

        class Parent(RenderableLayoutView):
            async def _retire_registration(self):
                raise RuntimeError("the registration could not be retired")

        parent = Parent(interaction=_make_interaction())
        await parent.send()
        child = Child(interaction=_make_interaction(), parent=parent)
        await child.send()
        first = asyncio.create_task(parent.exit())
        await asyncio.wait_for(parked.wait(), 1)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        await asyncio.wait_for(parent.task_manager.wait_tasks(_CLOSE_RESUME_TASK_OWNER), 2)

        assert not parent.is_finished()
        assert not parent._closing, "the failed resumed close left the panel closing"

    async def test_a_delete_cut_off_after_a_timeout_is_made_again(self):
        # A view its timeout froze was exited with delete_message=True, and the
        # exit was cut off while it deleted the message. The delete had been
        # taken off the view before it was sent, so nothing resumed it, and a
        # later exit(delete_message=True) found the message closed and sent none.
        from cascadeui.views.base import _CLOSE_RESUME_TASK_OWNER

        in_delete, release = asyncio.Event(), asyncio.Event()
        deleted = []

        async def delete(*args, **kwargs):
            if not in_delete.is_set():
                in_delete.set()
                await release.wait()
            deleted.append(True)

        view = RenderableLayoutView(interaction=_make_interaction())
        await view.send()
        view._message.delete = AsyncMock(side_effect=delete)
        await view.on_timeout()
        closing = asyncio.create_task(view.exit(delete_message=True))
        try:
            await asyncio.wait_for(in_delete.wait(), 1)
            closing.cancel()
            with pytest.raises(asyncio.CancelledError):
                await closing
            await asyncio.wait_for(view.task_manager.wait_tasks(_CLOSE_RESUME_TASK_OWNER), 2)
        finally:
            release.set()

        assert deleted == [True], "the cut-off delete was never made again"

    async def test_a_settle_cut_off_in_its_retire_does_not_freeze_again(self):
        # A timeout froze the message, then settled an exit recorded while it
        # ran, and was cut off while retiring for that exit. The close that
        # took over froze the message a second time, though the timeout's
        # freeze had landed.
        from cascadeui.views.base import _CLOSE_RESUME_TASK_OWNER

        in_freeze, freeze_gate = asyncio.Event(), asyncio.Event()
        in_retire, retire_gate = asyncio.Event(), asyncio.Event()
        freezes = []

        class Panel(RenderableLayoutView):
            async def _retire_registration(self):
                if not in_retire.is_set():
                    in_retire.set()
                    await retire_gate.wait()
                await super()._retire_registration()

        view = Panel(interaction=_make_interaction())
        view.add_item(ActionRow(StatefulButton(label="go", custom_id="p:go")))
        await view.send()
        message = view._message

        async def edit(**kwargs):
            freezes.append(kwargs.get("view"))
            if len(freezes) == 1:
                in_freeze.set()
                await freeze_gate.wait()
            return message

        message.edit = AsyncMock(side_effect=edit)
        timing = asyncio.create_task(view.on_timeout())
        try:
            await asyncio.wait_for(in_freeze.wait(), 1)
            exiting = asyncio.create_task(view.exit())
            await self._settle()
            freeze_gate.set()
            await asyncio.wait_for(in_retire.wait(), 1)
            timing.cancel()
            with pytest.raises(asyncio.CancelledError):
                await timing
            retire_gate.set()
            await asyncio.wait_for(exiting, 5)
            await asyncio.wait_for(view.task_manager.wait_tasks(_CLOSE_RESUME_TASK_OWNER), 2)
        finally:
            freeze_gate.set()
            retire_gate.set()

        assert view.is_finished()
        assert len(freezes) == 1, f"the message was frozen again: {len(freezes)} edits"

    @pytest.mark.parametrize("refusal", ["veto", "instance_limit"])
    async def test_a_refused_send_leaves_a_view_attached_before_it(self, refusal):
        # The rollback closed every attached view, so refusing the parent
        # deleted a child sent before it.
        class Parent(RenderableLayoutView):
            instance_limit = 1
            instance_policy = "reject"

            async def on_pre_send(self, interaction):
                return refusal != "veto"

            async def on_instance_limit(self, error):
                pass

        if refusal == "instance_limit":
            await Parent(interaction=_make_interaction()).send()
        parent = Parent(interaction=_make_interaction())
        child = RenderableLayoutView(interaction=_make_interaction(), parent=parent)
        await child.send()
        assert child._attached_to is parent

        assert await parent.send() is None

        child._message.delete.assert_not_awaited()
        assert not child._torn_down()
        assert child._attached_to is None

    async def test_a_rolled_back_send_cancelled_while_its_children_close_is_torn_down(self):
        # The teardown ran after the children's cascade, so a cancel there
        # left a registered view with no message that locked its owner out.
        parked = asyncio.Event()

        class Companion(RenderableLayoutView):
            async def _retire_registration(self):
                parked.set()
                await asyncio.Event().wait()

        class Parent(RenderableLayoutView):
            instance_limit = 1
            instance_policy = "reject"

            async def on_load(self):
                await Companion(interaction=_make_interaction(), parent=self).send()

        interaction = _make_interaction()
        interaction.response.send_message = AsyncMock(
            side_effect=discord.HTTPException(MagicMock(status=500, reason="x"), "boom")
        )
        parent = Parent(interaction=interaction)
        sending = asyncio.create_task(parent.send())
        await asyncio.wait_for(parked.wait(), 1)
        sending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await sending

        assert parent._torn_down()
        assert parent.id not in parent.state_store._active_views
        again = Parent(interaction=_make_interaction())
        again.on_load = AsyncMock()
        assert await again.send() is not None

    async def test_a_send_refused_before_its_view_is_created_leaves_no_session(self):
        # The rollback's VIEW_DESTROYED found no view row and left the session
        # its SESSION_CREATED had made, with no member.
        store = get_store()

        async def refuse(action, state, next_fn):
            if action["type"] == "VIEW_CREATED":
                raise RuntimeError("refused")
            return await next_fn(action, state)

        store._add_middleware(refuse)
        try:
            view = RenderableLayoutView(interaction=_make_interaction())
            with pytest.raises(RuntimeError, match="refused"):
                await view.send()
        finally:
            store._remove_middleware(refuse)

        assert view.session_id not in store.state["sessions"]

    async def test_a_session_another_send_is_joining_is_kept(self):
        # A continuity class shares one session per user, so a send rolled back
        # while another is still registering leaves that session for it.
        class Shared(RenderableLayoutView):
            session_continuity = True

        store = get_store()
        waiting, gate = asyncio.Event(), asyncio.Event()
        refused = {}

        async def middleware(action, state, next_fn):
            if action["type"] == "VIEW_CREATED":
                if action["payload"]["view_id"] == refused.get("id"):
                    raise RuntimeError("refused")
                waiting.set()
                await gate.wait()
            return await next_fn(action, state)

        store._add_middleware(middleware)
        try:
            first = Shared(interaction=_make_interaction())
            second = Shared(interaction=_make_interaction())
            assert first.session_id == second.session_id
            refused["id"] = first.id
            joining = asyncio.create_task(second.send())
            await asyncio.wait_for(waiting.wait(), 1)
            with pytest.raises(RuntimeError, match="refused"):
                await first.send()
            gate.set()
            await joining
        finally:
            gate.set()
            store._remove_middleware(middleware)

        assert second.id in store.state["sessions"][second.session_id]["members"]

    async def test_a_send_cancelled_while_a_view_created_hook_runs_is_torn_down(self):
        # The exit of the batch that registers the view runs its hooks, and it
        # sat outside the rollback, so a timeout around send() landing there
        # left a registered view with no message that locked its owner out.
        inside = asyncio.Event()

        async def audit(action, state):
            inside.set()
            await asyncio.Event().wait()

        class Panel(RenderableLayoutView):
            instance_limit = 1
            instance_policy = "reject"

        panel = Panel(interaction=_make_interaction())
        panel.state_store.on("view_created", audit)
        sending = asyncio.create_task(panel.send())
        await asyncio.wait_for(inside.wait(), 1)
        sending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await sending
        panel.state_store.off("view_created", audit)

        assert panel._torn_down()
        assert panel.id not in panel.state_store._active_views
        assert await Panel(interaction=_make_interaction()).send() is not None

    async def test_a_direct_on_timeout_cut_off_while_its_children_close_leaves_the_view_live(self):
        # Stopped first, the cut-off left a view discord.py no longer routed
        # to, still registered and rendering.
        parked = asyncio.Event()

        class Child(RenderableLayoutView):
            async def _retire_registration(self):
                parked.set()
                await asyncio.Event().wait()

        view = RenderableLayoutView(interaction=_make_interaction())
        view.add_item(ActionRow(StatefulButton(label="Go", custom_id="go")))
        await view.send()
        await Child(interaction=_make_interaction(), parent=view).send()
        timing_out = asyncio.create_task(view.on_timeout())
        await asyncio.wait_for(parked.wait(), 1)
        timing_out.cancel()
        with pytest.raises(asyncio.CancelledError):
            await timing_out

        assert not view.is_finished()
        assert not view._closed()
        assert not view._torn_down()

    async def test_a_stopped_childs_close_cut_off_still_tears_it_down(self):
        parked = asyncio.Event()

        class Grandchild(RenderableLayoutView):
            async def _retire_registration(self):
                if not parked.is_set():
                    parked.set()
                    await asyncio.Event().wait()
                await super()._retire_registration()

        parent = RenderableLayoutView(interaction=_make_interaction())
        await parent.send()
        child = RenderableLayoutView(interaction=_make_interaction(), parent=parent)
        await child.send()
        await Grandchild(interaction=_make_interaction(), parent=child).send()
        child.stop()
        closing = asyncio.create_task(parent.exit())
        await asyncio.wait_for(parked.wait(), 1)
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing
        await self._settle()

        # Stopped, it could not take clicks again: its close carried on.
        assert child._torn_down()
        assert child.id not in child.state_store._active_views

    async def test_a_close_whose_destroy_was_cancelled_leaves_no_ghost(self):
        """A cancel while a middleware awaited VIEW_DESTROYED left the view
        unsubscribed but registered, and every later close skipped the
        teardown of a view that read as torn down: a permanent ghost."""
        from cascadeui.setup import setup_middleware

        parked, release = asyncio.Event(), asyncio.Event()

        async def audit(action, state, next_fn):
            if action["type"] == "VIEW_DESTROYED" and not release.is_set():
                parked.set()
                await release.wait()
            return await next_fn(action, state)

        await setup_middleware(audit)
        view = RenderableLayoutView(interaction=_make_interaction())
        await view.send()
        store = view.state_store

        closing = asyncio.create_task(view.exit())
        await asyncio.wait_for(parked.wait(), 1)
        closing.cancel()
        try:
            with pytest.raises(asyncio.CancelledError):
                await closing
        finally:
            release.set()
        await self._settle()

        assert view.id not in store._active_views
        assert view.id not in store.state["views"]
        assert view.session_id not in store.state["sessions"]

    @pytest.mark.parametrize("close", ["exit", "on_timeout"])
    async def test_a_close_waiting_on_a_push_leaves_the_removal_to_the_push(self, close):
        """The close found the source torn down but still registered and
        removed it, and the push then removed it again: every
        view_destroyed hook ran twice."""
        loading = asyncio.Event()

        class Detail(RenderableLayoutView):
            async def on_load(self):
                await loading.wait()

        view = RenderableLayoutView(interaction=_make_interaction())
        await view.send()
        store = view.state_store
        destroyed = []
        store.on(
            "VIEW_DESTROYED", lambda action, state: destroyed.append(action["payload"]["view_id"])
        )

        pushing = asyncio.create_task(view.push(Detail, interaction=_make_interaction()))
        await self._settle(10)
        closing = asyncio.create_task(getattr(view, close)())
        await self._settle(10)
        loading.set()
        await asyncio.wait_for(asyncio.gather(pushing, closing), 5)
        await self._settle()

        assert destroyed.count(view.id) == 1
        assert view.id not in store._active_views

    async def test_a_handed_on_view_whose_destroy_failed_is_removed_by_a_later_close(self):
        """A push's removal of the source failed (a middleware raised), and
        the retry a later close runs skipped every view that had handed its
        panel on, so the source stayed registered for good."""
        view = RenderableLayoutView(interaction=_make_interaction())
        await view.send()
        store = view.state_store
        failing = {view.id}

        async def flaky(action, state, next_fn):
            if action["type"] == "VIEW_DESTROYED":
                view_id = (action.get("payload") or {}).get("view_id")
                if view_id in failing:
                    failing.discard(view_id)
                    raise RuntimeError("analytics endpoint down")
            return await next_fn(action, state)

        store._add_middleware(flaky)
        try:
            await view.push(RenderableLayoutView, interaction=_make_interaction())
            await self._settle()
            assert not failing, "the push's removal of the source never ran"
            assert view.id in store._active_views

            await view.exit()
            await self._settle()
        finally:
            store._middleware.remove(flaky)

        assert view.id not in store._active_views, "the source stayed registered"
        assert view.id not in store.state["views"]
