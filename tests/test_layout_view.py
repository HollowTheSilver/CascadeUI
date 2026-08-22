"""Tests for StatefulLayoutView (V2 base class)."""

import asyncio
import inspect
import io
import logging
import time
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import discord
import pytest
from discord.ui import ActionRow, Button, Container
from discord.ui import File as UIFile
from discord.ui import LayoutView, MediaGallery, Section, Separator, TextDisplay, Thumbnail
from helpers import RenderableLayoutView
from helpers import make_interaction as _make_interaction

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
        with pytest.raises(RuntimeError, match=r"reload\(\) called from inside"):
            await view.reload()
        # The failed run released the lock; the view is not wedged.
        assert not view._reload_lock.locked()


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

    async def test_gated_reload_during_a_live_deferred_task_is_not_latched(self):
        """A reload gated while the single deferred task is already past its
        own dispatch must leave a task to serve it. The gate declines to queue
        a second task while one is alive; without the exiting task requeueing
        a successor, the slot clears and the pending reload is latched with
        nothing left to run it."""
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
        await view.reload(force=True)
        # The gate declined to queue: the live task still owns the slot.
        # (The DEFERRED disposition itself is asserted in its own test.)
        assert view._reload_pending is True
        assert view._deferred_refresh_task is deferred

        park.set()
        await deferred

        # The exiting task queued a successor for the coalesced reload
        # instead of clearing the slot over a still-pending fetch.
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

    async def test_deferred_refresh_noop_on_finished_view(self):
        view = self._make_view(self._build_simple, refresh_cooldown_ms=50)
        self._prime(view)
        view.on_state_changed = AsyncMock()
        view.stop()  # mark view as finished

        await view._deferred_refresh(0.01)

        view.on_state_changed.assert_not_awaited()

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
        await asyncio.sleep(0.35)
        assert view._message.edit.await_count == 2

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

    async def test_modal_interaction_falls_through(self):
        """Modal submissions have a message, but the response cannot
        carry a component edit -- fast path refuses and defers to the
        channel endpoint.
        """
        view = self._make_view(self._build_simple)
        self._prime(view)
        interaction = self._make_acting_interaction()
        interaction.type = discord.InteractionType.modal_submit

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


class TestEphemeralActingRefresh:
    """Ephemeral acting views edit through the webhook without pre-deferring.

    The edit ships through ``self._message.edit()`` -- the
    ``InteractionMessage`` / ``WebhookMessage`` whose ``.edit()`` routes
    through the webhook on the original send's token, independent of the
    click's ack -- so it lands without first waiting on a deferred-update
    round-trip. ``refresh()`` does not defer; the click is acknowledged
    after the callback by the post-callback defer in ``_scheduled_task``
    (or by the auto-defer timer when the edit is slow). The edit-as-ack
    fast path stays in force for non-ephemeral views, where the edit is
    fast enough to double as the ack.
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

    async def test_acting_ephemeral_edits_through_webhook_without_predefer(self):
        """No pre-defer in refresh(): skip the edit-as-ack fast path and ship
        the edit straight through the webhook handle (``self._message``). The
        click's ack is delegated to _scheduled_task's post-callback defer.
        """
        view = self._make_ephemeral_view()
        interaction = self._make_acting_interaction()

        token = _CURRENT_INTERACTION.set(interaction)
        try:
            await view.refresh()
        finally:
            _CURRENT_INTERACTION.reset(token)

        # refresh() does not ack -- no deferred update fires here.
        interaction.response.defer.assert_not_called()
        # The edit-as-ack fast path is reserved for non-ephemeral views.
        interaction.response.edit_message.assert_not_called()
        # The edit shipped through the webhook handle.
        view._message.edit.assert_awaited_once_with(view=view)

    async def test_non_acting_ephemeral_edits_without_a_defer(self):
        """Background ephemeral refreshes (no bound interaction) edit straight
        through the webhook handle -- no deferred ack fires because there is
        nothing to acknowledge.
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

        await asyncio.sleep(0.35)

        assert view._message.edit.await_count == 2  # the retry shipped it

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

        await asyncio.sleep(0.35)

        # The retry shipped, and it shipped the ARMED tree (no rebuild).
        assert view._message.edit.await_count == 2
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

    async def test_no_retry_is_queued_for_a_finished_view(self):
        view = self._make_view()
        view._message.edit = AsyncMock(side_effect=_FakeRateLimit(retry_after=0.2))
        view.stop()

        await view.refresh()

        assert view._deferred_refresh_task is None


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
        """The seed hook was the one member this family's sweep missed.

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
        # No baseline was recorded, so the next refresh ships unconditionally
        # rather than skipping an edit against a digest that never computed.
        assert view._last_tree_digest is None
        assert "post-send setup raised" in caplog.text

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

        shipped = await source._apply_navigation_edit(destination, interaction, None)

        # refresh() swallows the transport failure so a repaint can retry, but
        # navigation tears the source down on the strength of the edit, so a
        # swallowed failure has to read as "not shipped" here.
        assert shipped is False

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
