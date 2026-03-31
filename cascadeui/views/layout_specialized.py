# // ========================================( Modules )======================================== // #


import inspect
from typing import Any, Callable, Dict, List, Optional, Union

import discord
from discord import Interaction
from discord.ui import ActionRow, Button, Container, TextDisplay

from ..components.base import StatefulButton, StatefulSelect
from .layout import StatefulLayoutView

# // ========================================( FormLayoutView )======================================== // #


class FormLayoutView(StatefulLayoutView):
    """A V2 layout view for collecting form data from users.

    The V2 equivalent of ``FormView``. Displays field status using
    ``TextDisplay`` inside a ``Container`` instead of embeds.

    Supports field types: "select", "boolean".
    String fields should use a Modal workflow (see TextInput / Modal in components.inputs).
    """

    def __init__(self, *args, title="Form", fields=None, on_submit=None, **kwargs):
        super().__init__(*args, **kwargs)

        self.title = title
        self.fields = fields or []
        self.on_submit = on_submit
        self.values = {}

        self._create_form_controls()
        self._rebuild_display()

    def _create_form_controls(self):
        """Create form controls based on field definitions."""
        for field in self.fields:
            field_type = field.get("type", "string")
            field_id = field.get("id")
            field_label = field.get("label", field_id)
            field_required = field.get("required", False)

            if field_type == "select":
                options = [
                    discord.SelectOption(
                        label=opt.get("label"),
                        value=opt.get("value"),
                        description=opt.get("description"),
                    )
                    for opt in field.get("options", [])
                ]

                select = StatefulSelect(
                    placeholder=field.get("placeholder", f"Select {field_label}..."),
                    options=options,
                    min_values=1 if field_required else 0,
                    max_values=1,
                    custom_id=f"form_{field_id}",
                )

                def make_select_callback(fid, sel):
                    async def callback(interaction):
                        self.values[fid] = sel.values[0]
                        await interaction.response.defer()
                        await self._update_form_display()

                    return callback

                select.callback = make_select_callback(field_id, select)
                self.add_item(ActionRow(select))

            elif field_type == "boolean":
                yes_button = StatefulButton(
                    label=f"{field_label}: Yes",
                    style=discord.ButtonStyle.success,
                    custom_id=f"form_{field_id}_yes",
                )

                no_button = StatefulButton(
                    label=f"{field_label}: No",
                    style=discord.ButtonStyle.danger,
                    custom_id=f"form_{field_id}_no",
                )

                def make_bool_callback(fid, value):
                    async def callback(interaction):
                        self.values[fid] = value
                        await interaction.response.defer()
                        await self._update_form_display()

                    return callback

                yes_button.callback = make_bool_callback(field_id, True)
                no_button.callback = make_bool_callback(field_id, False)

                self.add_item(ActionRow(yes_button, no_button))

        # Submit button
        submit_button = StatefulButton(
            label="Submit",
            style=discord.ButtonStyle.primary,
            custom_id="form_submit",
        )

        async def submit_callback(interaction):
            valid, errors = await self._validate_form()

            if valid:
                if self.on_submit:
                    await self.on_submit(interaction, self.values)
                    if not interaction.response.is_done():
                        await interaction.response.defer()
                else:
                    await interaction.response.send_message(
                        f"Form submitted with values: {self.values}", ephemeral=True
                    )
                # Skip if on_submit already called exit/push/replace
                if not self.is_finished():
                    await self.exit()
            else:
                if isinstance(errors, dict):
                    lines = []
                    for fid, field_errors in errors.items():
                        label = fid
                        for f in self.fields:
                            if f.get("id") == fid:
                                label = f.get("label", fid)
                                break
                        msgs = ", ".join(e.message for e in field_errors)
                        lines.append(f"**{label}:** {msgs}")
                    error_text = "\n".join(lines)
                else:
                    error_text = errors

                embed = discord.Embed(
                    title="Validation Failed",
                    description=error_text,
                    color=discord.Color.red(),
                )
                await interaction.response.send_message(embed=embed, ephemeral=True)

        submit_button.callback = submit_callback
        self.add_item(ActionRow(submit_button))

    def _rebuild_display(self):
        """Build the form status display as V2 components.

        Rebuilds the entire view: display container first, then interactive
        ActionRows. Uses add_item() exclusively to keep LayoutView's
        _total_children counter accurate.
        """
        lines = [f"**{self.title}**\n"]

        for field in self.fields:
            field_id = field.get("id")
            field_label = field.get("label", field_id)
            field_value = self.values.get(field_id, "Not set")
            required = " *" if field.get("required", False) else ""

            if field.get("type") == "boolean":
                field_value = (
                    "Yes" if field_value is True else "No" if field_value is False else "Not set"
                )

            lines.append(f"{field_label}{required}: {field_value}")

        # Snapshot interactive ActionRows before clearing
        action_rows = [c for c in self.children if isinstance(c, ActionRow)]

        self.clear_items()

        # Display container first
        container = Container(
            TextDisplay("\n".join(lines)),
            accent_colour=self.get_theme().get_style("primary_color"),
        )
        self.add_item(container)

        # Re-add interactive ActionRows
        for row in action_rows:
            self.add_item(row)

    async def _validate_form(self):
        """Validate form data using both required-field checks and field validators."""
        missing_fields = []
        for field in self.fields:
            if field.get("required", False):
                field_id = field.get("id")
                if field_id not in self.values or self.values[field_id] is None:
                    missing_fields.append(field.get("label", field_id))

        if missing_fields:
            return False, f"Please complete all required fields: {', '.join(missing_fields)}"

        has_validators = any(field.get("validators") for field in self.fields)
        if has_validators:
            from ..validation import validate_fields

            errors = await validate_fields(self.values, self.fields)
            if errors:
                return False, errors

        return True, ""

    async def _update_form_display(self):
        """Update the form display with current values."""
        self._rebuild_display()

        if self.message:
            await self.message.edit(view=self)

    async def update_from_state(self, state):
        """Update form display when state changes."""
        await self._update_form_display()


# // ========================================( PaginatedLayoutView )======================================== // #


class PaginatedLayoutView(StatefulLayoutView):
    """A V2 layout view for paginated content.

    The V2 equivalent of ``PaginatedView``. Pages are lists of V2 components
    (Container, TextDisplay, etc.) that replace the view's content on each
    page turn.

    Pages can be:
    - A list of V2 items (Container, TextDisplay, etc.)
    - A callable (sync or async) that returns a list of V2 items
    - A ``str`` wrapped in a Container + TextDisplay automatically

    Use the ``from_data`` classmethod to auto-paginate a list of items.
    """

    # Page count above which first/last and go-to-page buttons appear
    jump_threshold: int = 5

    def __init__(self, *args, pages=None, _per_page=None, _formatter=None, **kwargs):
        super().__init__(*args, **kwargs)

        self.pages = pages or []
        self.current_page = 0
        self._per_page: Optional[int] = _per_page
        self._formatter: Optional[Callable] = _formatter

        self._build_view()

    def _build_view(self):
        """Build the complete view: page content + navigation."""
        self._add_page_content()
        self._add_navigation_buttons()
        self._build_extra_items()

    def _resolve_page(self, page):
        """Resolve a page entry to a list of V2 items."""
        if isinstance(page, str):
            return [Container(TextDisplay(page))]
        if isinstance(page, list):
            return page
        if callable(page):
            result = page()
            if isinstance(result, list):
                return result
            return [result]
        return [page]

    def _add_page_content(self):
        """Add the current page's V2 components to the view."""
        if not self.pages:
            self.add_item(Container(TextDisplay("No pages.")))
            return

        items = self._resolve_page(self.pages[self.current_page])
        for item in items:
            self.add_item(item)

    def _add_navigation_buttons(self):
        """Add navigation buttons in an ActionRow."""
        total = len(self.pages)
        show_jump = total > self.jump_threshold
        at_first = self.current_page == 0
        at_last = self.current_page >= max(total - 1, 0)

        buttons = []

        if show_jump:
            first_btn = Button(
                label="\u23ee",
                style=discord.ButtonStyle.secondary,
                custom_id="paginated_first",
                disabled=at_first,
            )

            async def first_callback(interaction):
                self.current_page = 0
                await interaction.response.defer()
                await self._update_page()

            first_btn.callback = first_callback
            buttons.append(first_btn)

        prev_btn = Button(
            label="\u25c0",
            style=discord.ButtonStyle.secondary,
            custom_id="paginated_prev",
            disabled=at_first,
        )

        async def prev_callback(interaction):
            self.current_page = max(0, self.current_page - 1)
            await interaction.response.defer()
            await self._update_page()

        prev_btn.callback = prev_callback
        buttons.append(prev_btn)

        page_label = f"{self.current_page + 1}/{max(total, 1)}"
        if show_jump:
            goto_btn = Button(
                label=page_label,
                style=discord.ButtonStyle.primary,
                custom_id="paginated_goto",
            )

            async def goto_callback(interaction):
                await self._open_goto_modal(interaction)

            goto_btn.callback = goto_callback
            buttons.append(goto_btn)
        else:
            indicator = Button(
                label=f"Page {page_label}",
                style=discord.ButtonStyle.secondary,
                custom_id="paginated_indicator",
                disabled=True,
            )
            buttons.append(indicator)

        next_btn = Button(
            label="\u25b6",
            style=discord.ButtonStyle.secondary,
            custom_id="paginated_next",
            disabled=at_last,
        )

        async def next_callback(interaction):
            self.current_page = min(len(self.pages) - 1, self.current_page + 1)
            await interaction.response.defer()
            await self._update_page()

        next_btn.callback = next_callback
        buttons.append(next_btn)

        if show_jump:
            last_btn = Button(
                label="\u23ed",
                style=discord.ButtonStyle.secondary,
                custom_id="paginated_last",
                disabled=at_last,
            )

            async def last_callback(interaction):
                self.current_page = len(self.pages) - 1
                await interaction.response.defer()
                await self._update_page()

            last_btn.callback = last_callback
            buttons.append(last_btn)

        self.add_item(ActionRow(*buttons))

    async def _open_goto_modal(self, interaction: Interaction):
        """Open a modal for direct page number input."""
        total = len(self.pages)

        class _GotoModal(discord.ui.Modal, title="Go to Page"):
            page_input = discord.ui.TextInput(
                label=f"Page number (1\u2013{total})",
                placeholder=str(self.current_page + 1),
                min_length=1,
                max_length=len(str(total)),
                required=True,
            )

            def __init__(modal_self):
                super().__init__()
                modal_self._paginated_view = self

            async def on_submit(modal_self, modal_interaction: Interaction):
                value = modal_self.page_input.value.strip()
                try:
                    page_num = int(value)
                except ValueError:
                    await modal_interaction.response.send_message(
                        f"'{value}' is not a valid page number.", ephemeral=True
                    )
                    return

                page_num = max(1, min(page_num, total))
                modal_self._paginated_view.current_page = page_num - 1
                await modal_interaction.response.defer()
                await modal_self._paginated_view._update_page()

        await interaction.response.send_modal(_GotoModal())

    def _build_extra_items(self):
        """Hook for subclasses to add components below the navigation buttons.

        Called after navigation buttons during init and during every
        ``_update_page()`` call. Override this to add select menus, buttons,
        or other items. Use ``remove_item()`` to clear stale components first.
        """
        pass

    async def _update_page(self):
        """Rebuild the entire view for the current page and edit the message."""
        if not self.pages:
            return

        self.clear_items()
        self._add_page_content()
        self._add_navigation_buttons()
        self._build_extra_items()

        if self.message:
            await self.message.edit(view=self)

    @classmethod
    async def from_data(
        cls,
        items: list,
        per_page: int,
        formatter: Callable,
        **kwargs,
    ) -> "PaginatedLayoutView":
        """Create a PaginatedLayoutView by chunking items and applying a formatter.

        Parameters
        ----------
        items:
            The full list of items to paginate.
        per_page:
            Number of items per page.
        formatter:
            A sync or async callable that receives a list of items for one page
            and returns a list of V2 components, a single V2 component, or a str.
        **kwargs:
            Forwarded to the PaginatedLayoutView constructor.
        """
        chunks = [items[i : i + per_page] for i in range(0, len(items), per_page)]
        pages = []
        for chunk in chunks:
            if inspect.iscoroutinefunction(formatter):
                pages.append(await formatter(chunk))
            else:
                pages.append(formatter(chunk))
        view = cls(pages=pages, _per_page=per_page, _formatter=formatter, **kwargs)
        return view

    async def refresh_data(self, items: list):
        """Re-paginate with new data using the original per_page and formatter.

        Only works on views created via ``from_data()``.
        """
        if self._per_page is None or self._formatter is None:
            raise RuntimeError("refresh_data() requires a view created via from_data()")

        chunks = [items[i : i + self._per_page] for i in range(0, len(items), self._per_page)]
        pages = []
        for chunk in chunks:
            if inspect.iscoroutinefunction(self._formatter):
                pages.append(await self._formatter(chunk))
            else:
                pages.append(self._formatter(chunk))

        self.pages = pages or []
        if self.current_page >= len(self.pages):
            self.current_page = max(0, len(self.pages) - 1)

        await self._update_page()

    async def update_from_state(self, state):
        """Update pagination when state changes."""
        await self._update_page()
