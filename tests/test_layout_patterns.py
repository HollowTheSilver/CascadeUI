"""Tests for TabLayoutView and WizardLayoutView (V2 patterns)."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from discord.ui import ActionRow, Container, LayoutView, TextDisplay
from helpers import make_interaction as _make_interaction

from cascadeui.components.base import StatefulButton
from cascadeui.views.layout import StatefulLayoutView
from cascadeui.views.patterns import MenuLayoutView, MenuView, TabLayoutView, WizardLayoutView
from cascadeui.views.view import StatefulView


async def _noop(interaction):
    pass


# // ========================================( TabLayoutView )======================================== // #


class TestTabLayoutViewInit:
    """Basic init and tab switching tests."""

    def test_is_subclass_of_stateful_layout_view(self):
        assert issubclass(TabLayoutView, StatefulLayoutView)

    def test_is_subclass_of_layout_view(self):
        assert issubclass(TabLayoutView, LayoutView)

    def test_init_with_tabs(self):
        async def builder_a():
            return [Container(TextDisplay("Tab A"))]

        async def builder_b():
            return [Container(TextDisplay("Tab B"))]

        interaction = _make_interaction()
        view = TabLayoutView(
            interaction=interaction,
            tabs={"Tab A": builder_a, "Tab B": builder_b},
        )

        assert view.active_tab == "Tab A"
        assert view._active_tab == 0

    def test_has_tab_buttons(self):
        async def builder():
            return [Container(TextDisplay("content"))]

        interaction = _make_interaction()
        view = TabLayoutView(
            interaction=interaction,
            tabs={"First": builder, "Second": builder},
        )

        # Should have an ActionRow with tab buttons
        action_rows = [c for c in view.children if isinstance(c, ActionRow)]
        assert len(action_rows) >= 1

        # Check tab button custom_ids
        tab_row = action_rows[0]
        custom_ids = [getattr(c, "custom_id", None) for c in tab_row.children]
        assert "tab_0" in custom_ids
        assert "tab_1" in custom_ids

    def test_active_tab_property(self):
        async def builder():
            return [Container(TextDisplay("x"))]

        interaction = _make_interaction()
        view = TabLayoutView(
            interaction=interaction,
            tabs={"Alpha": builder, "Beta": builder, "Gamma": builder},
        )

        assert view.active_tab == "Alpha"
        view._active_tab = 2
        assert view.active_tab == "Gamma"


class TestTabLayoutViewRefresh:
    """Tab switching refresh tests."""

    async def test_refresh_tabs_updates_content(self):
        async def builder_a():
            return [Container(TextDisplay("Content A"))]

        async def builder_b():
            return [Container(TextDisplay("Content B"))]

        interaction = _make_interaction()
        view = TabLayoutView(
            interaction=interaction,
            tabs={"A": builder_a, "B": builder_b},
        )
        view._message = MagicMock()
        view._message.edit = AsyncMock()

        view._active_tab = 1
        await view._refresh_tabs()

        view._message.edit.assert_called_once()


# // ========================================( WizardLayoutView )======================================== // #


class TestWizardLayoutViewInit:
    """Basic init tests."""

    def test_is_subclass_of_stateful_layout_view(self):
        assert issubclass(WizardLayoutView, StatefulLayoutView)

    def test_is_subclass_of_layout_view(self):
        assert issubclass(WizardLayoutView, LayoutView)

    def test_init_with_steps(self):
        async def builder():
            return [Container(TextDisplay("Step content"))]

        interaction = _make_interaction()
        view = WizardLayoutView(
            interaction=interaction,
            steps=[
                {"name": "Step 1", "builder": builder},
                {"name": "Step 2", "builder": builder},
            ],
        )

        assert view.current_step == 0
        assert view.step_count == 2

    def test_has_nav_buttons(self):
        async def builder():
            return [Container(TextDisplay("x"))]

        interaction = _make_interaction()
        view = WizardLayoutView(
            interaction=interaction,
            steps=[{"name": "S1", "builder": builder}],
        )

        # Find nav ActionRow with wizard buttons
        all_custom_ids = []
        for child in view.children:
            if isinstance(child, ActionRow):
                for item in child.children:
                    cid = getattr(item, "custom_id", None)
                    if cid:
                        all_custom_ids.append(cid)

        # Back and Next carry the step they were drawn for.
        assert "wizard_back:0" in all_custom_ids
        assert "wizard_indicator" in all_custom_ids
        assert "wizard_next:0" in all_custom_ids

    def test_single_step_shows_finish(self):
        async def builder():
            return [Container(TextDisplay("only step"))]

        interaction = _make_interaction()
        view = WizardLayoutView(
            interaction=interaction,
            steps=[{"name": "Only", "builder": builder}],
        )

        assert view._next_btn.custom_id == "wizard_next:0"
        assert view._next_btn.label == "Finish"


class TestWizardLayoutViewNavigation:
    """Step navigation tests."""

    async def test_go_next_advances_step(self):
        async def builder():
            return [Container(TextDisplay("step"))]

        interaction = _make_interaction()
        view = WizardLayoutView(
            interaction=interaction,
            steps=[
                {"name": "Step 1", "builder": builder},
                {"name": "Step 2", "builder": builder},
                {"name": "Step 3", "builder": builder},
            ],
        )
        view._message = MagicMock()
        view._message.edit = AsyncMock()

        nav_interaction = _make_interaction()
        await view._go_next(nav_interaction)

        assert view._current_step == 1

    async def test_go_back_decrements_step(self):
        async def builder():
            return [Container(TextDisplay("step"))]

        interaction = _make_interaction()
        view = WizardLayoutView(
            interaction=interaction,
            steps=[
                {"name": "Step 1", "builder": builder},
                {"name": "Step 2", "builder": builder},
            ],
        )
        view._message = MagicMock()
        view._message.edit = AsyncMock()

        # Go to step 2
        nav_interaction = _make_interaction()
        await view._go_next(nav_interaction)
        assert view._current_step == 1

        # Go back to step 1
        back_interaction = _make_interaction()
        await view._go_back(back_interaction)
        assert view._current_step == 0

    async def test_go_back_at_first_step_noop(self):
        async def builder():
            return [Container(TextDisplay("step"))]

        interaction = _make_interaction()
        view = WizardLayoutView(
            interaction=interaction,
            steps=[{"name": "Only", "builder": builder}],
        )

        nav_interaction = _make_interaction()
        await view._go_back(nav_interaction)

        assert view._current_step == 0

    async def test_finish_calls_on_finish(self):
        async def builder():
            return [Container(TextDisplay("step"))]

        finish_called = []

        class FinishWizard(WizardLayoutView):
            async def on_finish(self, interaction):
                finish_called.append(True)
                if not interaction.response.is_done():
                    await interaction.response.defer()

        interaction = _make_interaction()
        view = FinishWizard(
            interaction=interaction,
            steps=[{"name": "Only", "builder": builder}],
        )

        nav_interaction = _make_interaction()
        await view._go_next(nav_interaction)

        assert len(finish_called) == 1

    async def test_validator_blocks_next(self):
        async def builder():
            return [Container(TextDisplay("step"))]

        async def failing_validator():
            return False, "Validation failed"

        interaction = _make_interaction()
        view = WizardLayoutView(
            interaction=interaction,
            steps=[
                {"name": "Step 1", "builder": builder, "validator": failing_validator},
                {"name": "Step 2", "builder": builder},
            ],
        )

        nav_interaction = _make_interaction()
        await view._go_next(nav_interaction)

        # Should not advance
        assert view._current_step == 0
        nav_interaction.response.send_message.assert_called_once_with(
            "Validation failed", ephemeral=True
        )


class TestCompositeHostSeams:
    """The V2 composites probe host render seams by name, not by type.

    The component layer cannot import views without a cycle, so
    ``_rerender_host`` duck-types each seam. That makes the seam names a
    cross-package contract: renaming one silently stops composites from
    re-rendering inside that host.
    """

    def test_probed_host_seams_still_exist(self):
        """A rename check only. The behavior these seams drive is covered
        end to end by ``test_collapsible_toggle_rebuilds_tab`` in
        ``tests/test_tab_patterns.py``; this catches the cheaper failure of
        one of the three names disappearing, which that test would report
        as a confusing render failure rather than a missing attribute.
        """
        from cascadeui.views.layout import StatefulLayoutView
        from cascadeui.views.patterns.tabs import TabLayoutView

        assert hasattr(TabLayoutView, "_refresh_tabs")
        assert hasattr(StatefulLayoutView, "reload")
        assert hasattr(StatefulLayoutView, "refresh")

    def test_probe_order_prefers_build_ui_then_tabs_then_reload(self):
        """The seam order is load-bearing: a TabLayoutView has no build_ui
        and its bare reload() would refresh a stale tree, so _refresh_tabs
        has to be probed before reload.
        """
        import inspect

        from cascadeui.components.patterns import v2

        src = inspect.getsource(v2._rerender_host)
        positions = [src.index(name) for name in ("build_ui", "_refresh_tabs", "reload")]
        assert positions == sorted(positions)


class TestGotoModalHookIsGuarded:
    """The goto-modal fires ``on_page_changed`` after moving the cursor.

    ``_safe_defer`` has already acked by that point, so a raising override
    would leave the user looking at a page that silently never turned: the
    cursor advances and ``_update_page`` never runs. The three sibling
    call sites in the same file were already guarded.
    """

    async def test_a_raising_hook_still_turns_the_page(self):
        import discord

        from cascadeui import PaginatedView

        class Raising(PaginatedView):
            async def on_page_changed(self, page):
                raise RuntimeError("hook failed")

        pages = [discord.Embed(title=str(i)) for i in range(10)]
        view = Raising(pages=pages, interaction=_make_interaction())
        view._message = MagicMock()
        view._message.edit = AsyncMock()
        opener = _make_interaction()
        goto = next(c for c in view.children if getattr(c, "custom_id", None) == "paginated_goto")
        await goto.callback(opener)
        modal = opener.response.send_modal.call_args.args[0]
        modal.page_input._value = "3"

        await modal.on_submit(_make_interaction())

        assert view.current_page == 2
        view._message.edit.assert_awaited()


class TestAutoExitButton:
    """``auto_exit_button`` adds the library's Exit button to a pattern and
    keeps it through every re-render, as ``MenuView`` already did."""

    @staticmethod
    def _exits(view):
        from cascadeui.components.base import StatefulButton

        return [
            b for b in view.walk_children() if isinstance(b, StatefulButton) and b.label == "Exit"
        ]

    @staticmethod
    async def _paginated_v2(flag):
        from cascadeui.views.patterns import PaginatedLayoutView

        class _View(PaginatedLayoutView):
            auto_exit_button = flag

        view = await _View.from_data(
            list(range(20)),
            per_page=5,
            formatter=lambda chunk: [TextDisplay(str(chunk))],
            interaction=_make_interaction(),
        )
        return view, lambda: view.set_page(1)

    @staticmethod
    async def _paginated_v1(flag):
        import discord

        from cascadeui.views.patterns import PaginatedView

        class _View(PaginatedView):
            auto_exit_button = flag

        pages = [discord.Embed(title=str(i)) for i in range(4)]
        view = _View(pages=pages, interaction=_make_interaction())
        return view, lambda: view.set_page(1)

    @staticmethod
    async def _tabs_v2(flag):
        class _View(TabLayoutView):
            auto_exit_button = flag

        async def builder():
            return [Container(TextDisplay("tab"))]

        view = _View(interaction=_make_interaction(), tabs={"A": builder, "B": builder})
        await view.on_load()
        return view, lambda: view.switch_tab("B")

    @staticmethod
    async def _tabs_v1(flag):
        import discord

        from cascadeui.views.patterns import TabView

        class _View(TabView):
            auto_exit_button = flag

        async def builder():
            return discord.Embed(title="tab")

        view = _View(interaction=_make_interaction(), tabs={"A": builder, "B": builder})
        return view, lambda: view.switch_tab("B")

    @staticmethod
    async def _wizard_v2(flag):
        class _View(WizardLayoutView):
            auto_exit_button = flag

        async def builder():
            return [Container(TextDisplay("step"))]

        view = _View(
            interaction=_make_interaction(),
            steps=[{"name": "A", "builder": builder}, {"name": "B", "builder": builder}],
        )
        await view.on_load()
        return view, lambda: view._go_next(_make_interaction())

    @staticmethod
    async def _wizard_v1(flag):
        import discord

        from cascadeui.views.patterns import WizardView

        class _View(WizardView):
            auto_exit_button = flag

        async def builder():
            return discord.Embed(title="step")

        view = _View(
            interaction=_make_interaction(),
            steps=[{"name": "A", "builder": builder}, {"name": "B", "builder": builder}],
        )
        return view, lambda: view._go_next(_make_interaction())

    @staticmethod
    async def _form(cls_name, flag):
        from cascadeui.views import patterns

        base = getattr(patterns, cls_name)

        class _View(base):
            auto_exit_button = flag

        view = _View(
            interaction=_make_interaction(),
            fields=[{"id": "n", "type": "text", "label": "Name"}],
        )
        view.refresh = AsyncMock()
        return view, view._update_form_display

    FACTORIES = [
        "_paginated_v2",
        "_paginated_v1",
        "_tabs_v2",
        "_tabs_v1",
        "_wizard_v2",
        "_wizard_v1",
        "form:FormLayoutView",
        "form:FormView",
    ]

    async def _build(self, name, flag):
        if name.startswith("form:"):
            return await self._form(name.split(":")[1], flag)
        return await getattr(self, name)(flag)

    @pytest.mark.parametrize("name", FACTORIES)
    async def test_the_exit_survives_a_re_render(self, name):
        view, re_render = await self._build(name, True)
        assert len(self._exits(view)) == 1

        result = re_render()
        if hasattr(result, "__await__"):
            await result

        assert len(self._exits(view)) == 1

    @pytest.mark.parametrize("name", FACTORIES)
    async def test_off_by_default(self, name):
        view, _ = await self._build(name, False)

        assert self._exits(view) == []

    def test_a_non_bool_value_is_refused_at_class_definition(self):
        from cascadeui.views.patterns import FormLayoutView, PaginatedLayoutView, TabView

        for base in (PaginatedLayoutView, TabView, WizardLayoutView, FormLayoutView):
            with pytest.raises(ValueError, match="auto_exit_button must be a bool"):
                type("_Bad", (base,), {"auto_exit_button": 1})

    EXIT_IDS = {
        "_paginated_v2": "paginated_exit",
        "_paginated_v1": "paginated_exit",
        "_tabs_v2": "tab_exit",
        "_tabs_v1": "tab_exit",
        "_wizard_v2": "wizard_exit",
        "_wizard_v1": "wizard_exit",
        "form:FormLayoutView": "form_exit",
        "form:FormView": "form_exit",
    }

    @pytest.mark.parametrize("name", FACTORIES)
    async def test_the_exit_has_a_fixed_id(self, name):
        """A generated id changes every run, so on a persistent panel the
        Exit button stopped answering after a restart."""
        view, _ = await self._build(name, True)

        assert [b.custom_id for b in self._exits(view)] == [self.EXIT_IDS[name]]

    @staticmethod
    def _menu(cls, **attrs):
        page_base = StatefulLayoutView if issubclass(cls, StatefulLayoutView) else StatefulView
        _Page = type("_Page", (page_base,), {})
        menu_cls = type("_Menu", (cls,), attrs)
        return menu_cls(interaction=_make_interaction(), categories=[{"label": "A", "view": _Page}])

    @pytest.mark.parametrize("cls", [MenuView, MenuLayoutView])
    def test_the_menu_exit_has_a_fixed_id(self, cls):
        view = self._menu(cls)

        assert [b.custom_id for b in self._exits(view)] == ["menu_exit"]

    @staticmethod
    async def _render(re_render):
        result = re_render()
        if hasattr(result, "__await__"):
            await result

    @pytest.mark.parametrize("name", FACTORIES)
    async def test_an_instance_override_adds_the_exit_at_the_next_render(self, name):
        """The button was added while the view was built, so a value set on
        the instance afterwards was accepted and did nothing."""
        view, re_render = await self._build(name, False)

        view.set_class_attribute("auto_exit_button", True)
        await self._render(re_render)
        await self._render(re_render)

        assert len(self._exits(view)) == 1

    @pytest.mark.parametrize("name", FACTORIES)
    async def test_an_instance_override_removes_the_exit_at_the_next_render(self, name):
        view, re_render = await self._build(name, True)

        view.set_class_attribute("auto_exit_button", False)
        await self._render(re_render)

        assert self._exits(view) == []

    @staticmethod
    def _fills_row_4(self):
        for i in range(5):
            self.add_item(StatefulButton(label=f"Extra {i}", row=4, callback=_noop))

    def test_a_full_row_4_names_the_attribute(self):
        """discord.py's own error names only the row, not the setting that put
        a sixth button on it."""
        import discord

        from cascadeui.views.patterns import PaginatedView, TabView

        async def builder():
            return discord.Embed(title="tab")

        builds = [
            lambda: type(
                "_P",
                (PaginatedView,),
                {"auto_exit_button": True, "_build_extra_items": self._fills_row_4},
            )(pages=[discord.Embed(title="1")], interaction=_make_interaction()),
            lambda: type(
                "_T",
                (TabView,),
                {"auto_exit_button": True, "_build_extra_items": self._fills_row_4},
            )(interaction=_make_interaction(), tabs={"A": builder}),
            lambda: self._menu(MenuView, _build_extra_items=self._fills_row_4),
        ]
        for build in builds:
            with pytest.raises(ValueError, match="auto_exit_button puts an Exit button on row 4"):
                build()

    @pytest.mark.parametrize("name", FACTORIES[:6])
    async def test_a_callers_button_with_the_exit_id_is_kept(self, name, monkeypatch):
        """The Exit was found by its id alone, so with the setting off a
        caller's own button that used the same id was removed at the first
        render."""
        exit_id = self.EXIT_IDS[name]

        def extras(view):
            quit_button = StatefulButton(label="Quit", custom_id=exit_id, callback=_noop)
            view.add_item(ActionRow(quit_button))

        base = {
            "_paginated_v2": "PaginatedLayoutView",
            "_tabs_v2": "TabLayoutView",
            "_wizard_v2": "WizardLayoutView",
        }.get(name)
        if base is not None:
            from cascadeui.views import patterns

            monkeypatch.setattr(getattr(patterns, base), "_build_extra_items", extras)
            view, re_render = await self._build(name, False)
        else:
            view, re_render = await self._build(name, False)
            view.add_item(StatefulButton(label="Quit", custom_id=exit_id, row=3, callback=_noop))

        await self._render(re_render)
        await self._render(re_render)

        quits = [b for b in view.walk_children() if getattr(b, "label", None) == "Quit"]
        assert [b.custom_id for b in quits] == [exit_id]


class TestInstanceOverridesApply:
    """An attribute set with ``set_class_attribute()`` after construction takes
    effect from the next render. Each was read only while the view was built,
    so the override was accepted and the first message showed the class value."""

    @staticmethod
    def _buttons(view):
        import discord

        walk = view.walk_children() if hasattr(view, "walk_children") else view.children
        return [c for c in walk if isinstance(c, discord.ui.Button)]

    @staticmethod
    def _text(view):
        return " ".join(c.content for c in view.walk_children() if isinstance(c, TextDisplay))

    @pytest.mark.parametrize("version", ["v1", "v2"])
    async def test_a_paginated_nav_label_shows_in_the_first_message(self, version):
        import discord

        from cascadeui.views.patterns import PaginatedLayoutView, PaginatedView

        if version == "v2":
            view = await PaginatedLayoutView.from_data(
                list(range(6)),
                per_page=1,
                formatter=lambda chunk: [TextDisplay(str(chunk))],
                interaction=_make_interaction(),
            )
        else:
            view = PaginatedView(
                pages=[discord.Embed(title=str(i)) for i in range(6)],
                interaction=_make_interaction(),
            )
        view.set_class_attribute("next_button_label", "Onward")
        view.set_class_attribute("jump_threshold", 100)

        await view.send()

        labels = [b.label for b in self._buttons(view)]
        assert "Onward" in labels
        assert len(labels) == 3  # prev, indicator, next: no jump buttons under 100 pages

    async def test_a_paginated_layout_flag_shows_in_the_first_message(self):
        from cascadeui.views.patterns import PaginatedLayoutView

        view = await PaginatedLayoutView.from_data(
            list(range(3)),
            per_page=1,
            formatter=lambda chunk: [TextDisplay(str(chunk))],
            interaction=_make_interaction(),
        )
        view.set_class_attribute("nav_inside_container", True)

        await view.send()

        assert [type(c) for c in view.children] == [Container]

    async def test_the_wizards_back_label_shows_in_the_first_message(self):
        async def builder():
            return [Container(TextDisplay("step"))]

        view = WizardLayoutView(
            interaction=_make_interaction(),
            steps=[{"name": "A", "builder": builder}, {"name": "B", "builder": builder}],
        )
        view.set_class_attribute("back_button_label", "Previous")

        await view.send()

        assert "Previous" in [b.label for b in self._buttons(view)]

    @pytest.mark.parametrize("cls_name", ["FormLayoutView", "FormView"])
    async def test_the_forms_edit_label_shows_in_the_first_message(self, cls_name):
        from cascadeui.views import patterns

        view = getattr(patterns, cls_name)(
            interaction=_make_interaction(),
            fields=[{"id": "n", "type": "text", "label": "Name"}],
        )
        view.set_class_attribute("text_edit_button_label", "Type it")

        await view.send()

        assert "Type it" in [b.label for b in self._buttons(view)]

    async def test_the_tab_row_layout_shows_in_the_first_message(self):
        async def builder():
            return [Container(TextDisplay("tab"))]

        view = TabLayoutView(
            interaction=_make_interaction(), tabs={f"T{i}": builder for i in range(7)}
        )
        view.set_class_attribute("tab_overflow_policy", (2, 5))

        await view.send()

        rows = [len(c.children) for c in view.children if isinstance(c, ActionRow)]
        assert rows[:2] == [2, 5]

    async def test_a_v1_tab_row_layout_shows_in_the_first_message(self):
        import discord

        from cascadeui.views.patterns import TabView

        async def builder():
            return discord.Embed(title="tab")

        view = TabView(interaction=_make_interaction(), tabs={f"T{i}": builder for i in range(7)})
        view.set_class_attribute("tab_overflow_policy", (2, 5))

        await view.send()

        rows = [b.row for b in self._buttons(view) if (b.custom_id or "").startswith("tab_")]
        assert [rows.count(0), rows.count(1)] == [2, 5]

    def test_an_invalid_tab_row_layout_is_refused(self):
        """The class body refused it; the instance override skipped that check."""
        view = TabLayoutView(interaction=_make_interaction(), tabs={"A": _noop})

        with pytest.raises(ValueError, match="not a valid preset"):
            view.set_class_attribute("tab_overflow_policy", "select")

    @pytest.mark.parametrize("cls", [MenuLayoutView, MenuView])
    async def test_the_menu_style_shows_in_the_first_message(self, cls):
        import discord

        # A menu pushes only within its own version.
        screen = type(
            "_Screen", (StatefulLayoutView if cls is MenuLayoutView else StatefulView,), {}
        )
        view = cls(interaction=_make_interaction(), categories=[{"label": "Go", "view": screen}])
        view.set_class_attribute("menu_style", discord.ButtonStyle.danger)

        await view.send()

        assert discord.ButtonStyle.danger in [b.style for b in self._buttons(view)]

    @pytest.mark.parametrize("cls", [MenuLayoutView, MenuView])
    async def test_a_menu_without_its_exit_sends_without_it(self, cls):
        screen = type(
            "_Screen", (StatefulLayoutView if cls is MenuLayoutView else StatefulView,), {}
        )
        view = cls(interaction=_make_interaction(), categories=[{"label": "Go", "view": screen}])
        view.set_class_attribute("auto_exit_button", False)

        await view.send()

        assert "menu_exit" not in [b.custom_id for b in self._buttons(view)]

    async def test_a_role_panel_title_shows_in_the_first_message(self):
        from cascadeui import RoleCategory, RolesLayoutView

        class _Panel(RolesLayoutView):
            categories = [RoleCategory(name="OverrideTitleProbe", roles={"A": 1})]

        view = _Panel(interaction=_make_interaction())
        view.set_class_attribute("title", "Pick your roles")

        await view.send()

        assert "Pick your roles" in self._text(view)

    def test_undo_turned_on_for_one_instance_is_tracked(self):
        view = StatefulView(interaction=_make_interaction())

        view.set_class_attribute("enable_undo", True)
        view.set_class_attribute("undo_limit", 7)

        assert view.state_store._undo_enabled_views[view.id] == 7
        view.set_class_attribute("enable_undo", False)
        assert view.id not in view.state_store._undo_enabled_views

    def test_a_subscription_override_reaches_the_store(self):
        class _Watcher(StatefulView):
            subscribed_actions = {"A"}

        view = _Watcher(interaction=_make_interaction())

        view.set_class_attribute("subscribed_actions", {"B"})

        assert view.state_store.subscribers[view.id][1] == {"B"}

    async def test_an_override_on_an_exited_view_leaves_the_store_alone(self):
        """A subscription override on an exited view raised ``KeyError`` (its
        subscriber was gone), and an undo override tracked a dead view again."""

        class _Watcher(StatefulView):
            subscribed_actions = {"A"}

        view = _Watcher(interaction=_make_interaction())
        await view.send()
        await view.exit()

        view.set_class_attribute("subscribed_actions", {"B"})
        view.set_class_attribute("enable_undo", True)

        assert view.id not in view.state_store._undo_enabled_views

    @pytest.mark.parametrize("given", ["no_content", "explicit_embed"])
    async def test_a_v1_tab_exit_override_shows_in_the_first_message(self, given):
        """With content of the caller's own, the first message went out before
        the controls were matched to the override, so it had no Exit."""
        import discord

        from cascadeui.views.patterns import TabView

        async def builder():
            return discord.Embed(title="tab")

        view = TabView(interaction=_make_interaction(), tabs={"A": builder, "B": builder})
        view.set_class_attribute("auto_exit_button", True)
        kwargs = {"embed": discord.Embed(title="welcome")} if given == "explicit_embed" else {}

        await view.send(**kwargs)

        assert "tab_exit" in [b.custom_id for b in self._buttons(view)]


class TestOverridingABuildSeamStillSends:
    """A subclass that replaces a pattern's build step without calling
    ``super()`` never reached the step that records what the tree was built
    from, and its first ``send()`` raised ``AttributeError``."""

    @staticmethod
    async def _sent(view):
        await view.send()
        return view

    async def test_a_menu_whose_build_ui_skips_super(self):
        class _Menu(MenuLayoutView):
            def build_ui(self):
                self.clear_items()
                self.add_item(TextDisplay("custom"))

        view = await self._sent(
            _Menu(
                interaction=_make_interaction(),
                categories=[{"label": "A", "view": StatefulLayoutView}],
            )
        )

        assert [c.content for c in view.children if isinstance(c, TextDisplay)] == ["custom"]

    async def test_a_role_panel_whose_build_ui_skips_super(self):
        from cascadeui import RoleCategory, RolesLayoutView

        class _Panel(RolesLayoutView):
            categories = [RoleCategory(name="SkipSuperProbe", roles={"A": 1})]

            def build_ui(self):
                self.clear_items()
                self.add_item(TextDisplay("custom"))

        view = await self._sent(_Panel(interaction=_make_interaction()))

        assert [c.content for c in view.children if isinstance(c, TextDisplay)] == ["custom"]

    async def test_a_v1_form_whose_build_skips_the_controls(self):
        from cascadeui.views.patterns import FormView

        class _Form(FormView):
            def _build_form(self):
                pass

        view = await self._sent(
            _Form(
                interaction=_make_interaction(),
                fields=[{"id": "n", "type": "text", "label": "Name"}],
            )
        )

        assert view.children == []

    async def test_a_v2_form_whose_display_is_its_own(self):
        from cascadeui.views.patterns import FormLayoutView

        class _Form(FormLayoutView):
            def _rebuild_display(self):
                self.clear_items()
                self.add_item(TextDisplay("custom"))

        view = await self._sent(
            _Form(
                interaction=_make_interaction(),
                fields=[{"id": "n", "type": "text", "label": "Name"}],
            )
        )

        assert [c.content for c in view.children if isinstance(c, TextDisplay)] == ["custom"]


class TestInstanceOverrideEdges:
    async def test_a_v2_indicator_label_applies_on_reload(self):
        """The indicator's label and format were left out of what the nav is
        built from, so a reload kept the old label until the next page turn."""
        from cascadeui.views.patterns import PaginatedLayoutView

        view = await PaginatedLayoutView.from_data(
            list(range(60)),
            per_page=1,
            formatter=lambda chunk: [TextDisplay(str(chunk))],
            interaction=_make_interaction(),
        )
        await view.send()

        view.set_class_attribute("indicator_button_label", "Pick a page")
        await view.reload()

        assert view._indicator_btn.label == "Pick a page"

    async def test_a_v1_tab_layout_the_rows_cannot_hold_is_refused_where_it_is_set(self):
        """The re-lay raised discord.py's row error inside a later click,
        naming neither the attribute nor the fix, and left a tab off the view."""
        import discord

        from cascadeui.views.patterns import TabView

        class _Tabs(TabView):
            def _build_extra_items(self):
                for i in range(5):
                    self.add_item(StatefulButton(label=f"Extra {i}", row=4, callback=_noop))

        async def builder():
            return discord.Embed(title="tab")

        view = _Tabs(interaction=_make_interaction(), tabs={f"T{i}": builder for i in range(5)})
        await view.send()

        with pytest.raises(ValueError, match=r"tab_overflow_policy \(1, 1, 1, 1, 1\) puts a tab"):
            view.set_class_attribute("tab_overflow_policy", (1, 1, 1, 1, 1))

        assert view.tab_overflow_policy == "fill"
        tabs = [c.label for c in view.children if (c.custom_id or "").startswith("tab_")]
        assert tabs == ["T0", "T1", "T2", "T3", "T4"]
        await view.switch_tab("T4")


class TestOverridesSurviveARebuild:
    """``pop()`` and the Continue button rebuild a view from its constructor
    kwargs, which cannot carry ``set_class_attribute()`` overrides, so the
    rebuilt view read its class values again."""

    async def test_a_label_override_is_still_shown_after_back(self):
        import discord
        from helpers import RenderableLayoutView

        from cascadeui.views.patterns import PaginatedLayoutView

        pages = await PaginatedLayoutView.from_data(
            list(range(30)),
            per_page=5,
            formatter=lambda chunk: [TextDisplay(str(chunk))],
            interaction=_make_interaction(),
        )
        pages.set_class_attribute("next_button_label", "Suivant")
        await pages.send()

        child = await pages.push(RenderableLayoutView, _make_interaction())
        back = await child.pop(_make_interaction())

        labels = [c.label for c in back.walk_children() if isinstance(c, discord.ui.Button)]
        assert "Suivant" in labels

    async def test_a_limit_override_holds_on_the_root_after_back(self):
        from helpers import RenderableLayoutView

        class Root(RenderableLayoutView):
            pass

        root = Root(interaction=_make_interaction())
        root.set_class_attribute("instance_limit", 1)
        root.set_class_attribute("participant_limit", 2)
        await root.send()

        child = await root.push(RenderableLayoutView, _make_interaction())
        back = await child.pop(_make_interaction())
        again = await back.push(RenderableLayoutView, _make_interaction())

        assert (back.instance_limit, back.participant_limit) == (1, 2)
        assert again._root_instance_limit() == 1

    async def test_the_continue_rebuild_keeps_the_overrides(self):
        from helpers import RenderableLayoutView

        class Panel(RenderableLayoutView):
            timeout = None

        panel = Panel(interaction=_make_interaction())
        panel.set_class_attribute("participant_limit", 2)
        panel.set_class_attribute("exit_policy", "delete")
        await panel.send(ephemeral=True)

        await panel._reopen_ephemeral(_make_interaction())

        new = panel.current_view
        assert new is not panel
        assert (new.participant_limit, new.exit_policy) == (2, "delete")
