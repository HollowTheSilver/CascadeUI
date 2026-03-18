# // ========================================( Modules )======================================== // #


import asyncio
from typing import Any, Callable, Dict, List, Optional, Union

import discord
from discord import Embed, Interaction

from ..components.base import StatefulButton, StatefulSelect
from .base import StatefulView

# // ========================================( Classes )======================================== // #


class FormView(StatefulView):
    """A view for collecting form data from users.

    Supports field types: "select", "boolean".
    String fields should use a Modal workflow (see TextInput / Modal in components.inputs).

    Fields can include a ``validators`` list of callables for per-field validation.
    See ``cascadeui.validation`` for built-in validators.
    """

    def __init__(self, *args, title="Form", fields=None, on_submit=None, **kwargs):
        super().__init__(*args, **kwargs)

        self.title = title
        self.fields = fields or []
        self.on_submit = on_submit
        self.values = {}

        # Create form controls synchronously — no race condition
        self._create_form_controls()

    def _create_form_controls(self):
        """Create form controls based on field definitions."""
        current_row = 0

        for field in self.fields:
            if current_row > 4:
                break  # Discord max 5 rows (0-4)

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
                    row=current_row,
                )

                # Capture field_id and select per-iteration via default args
                def make_select_callback(fid, sel):
                    async def callback(interaction):
                        self.values[fid] = sel.values[0]
                        await interaction.response.defer()
                        await self._update_form_display()

                    return callback

                select.callback = make_select_callback(field_id, select)
                self.add_item(select)
                current_row += 1  # Select takes a full row

            elif field_type == "boolean":
                yes_button = StatefulButton(
                    label=f"{field_label}: Yes",
                    style=discord.ButtonStyle.success,
                    custom_id=f"form_{field_id}_yes",
                    row=current_row,
                )

                no_button = StatefulButton(
                    label=f"{field_label}: No",
                    style=discord.ButtonStyle.danger,
                    custom_id=f"form_{field_id}_no",
                    row=current_row,
                )

                # Capture field_id per-iteration via default arg
                def make_bool_callback(fid, value):
                    async def callback(interaction):
                        self.values[fid] = value
                        await interaction.response.defer()
                        await self._update_form_display()

                    return callback

                yes_button.callback = make_bool_callback(field_id, True)
                no_button.callback = make_bool_callback(field_id, False)

                self.add_item(yes_button)
                self.add_item(no_button)
                current_row += 1  # Boolean pair takes one row

            # "string" type is intentionally not supported inline — Discord does not
            # allow text input inside Views, only inside Modals. Use a Modal workflow
            # or the components.inputs.Modal class for string collection.

        # Add submit button on the next available row (or last row if full)
        submit_row = min(current_row, 4)
        submit_button = StatefulButton(
            label="Submit",
            style=discord.ButtonStyle.primary,
            custom_id="form_submit",
            row=submit_row,
        )

        async def submit_callback(interaction):
            valid, errors = await self._validate_form()

            if valid:
                if self.on_submit:
                    await self.on_submit(interaction, self.values)
                    # Ensure the interaction is acknowledged even if on_submit forgot
                    if not interaction.response.is_done():
                        await interaction.response.defer()
                else:
                    await interaction.response.send_message(
                        f"Form submitted with values: {self.values}", ephemeral=True
                    )
                # Clean up the view after successful submission
                await self.exit()
            else:
                # Build an error embed with per-field messages
                embed = discord.Embed(
                    title="Validation Failed",
                    color=discord.Color.red(),
                )

                if isinstance(errors, dict):
                    # Per-field validation errors
                    for field_id, field_errors in errors.items():
                        field_label = field_id
                        for f in self.fields:
                            if f.get("id") == field_id:
                                field_label = f.get("label", field_id)
                                break
                        error_messages = "\n".join(e.message for e in field_errors)
                        embed.add_field(
                            name=field_label,
                            value=error_messages,
                            inline=False,
                        )
                else:
                    # Legacy string error (missing required fields)
                    embed.description = errors

                await interaction.response.send_message(embed=embed, ephemeral=True)

        submit_button.callback = submit_callback
        self.add_item(submit_button)

    async def _validate_form(self):
        """Validate form data using both required-field checks and field validators."""
        # Check required fields first
        missing_fields = []
        for field in self.fields:
            if field.get("required", False):
                field_id = field.get("id")
                if field_id not in self.values or self.values[field_id] is None:
                    missing_fields.append(field.get("label", field_id))

        if missing_fields:
            return False, f"Please complete all required fields: {', '.join(missing_fields)}"

        # Run per-field validators if any field has them
        has_validators = any(field.get("validators") for field in self.fields)
        if has_validators:
            from ..validation import validate_fields

            errors = await validate_fields(self.values, self.fields)
            if errors:
                return False, errors

        return True, ""

    async def _update_form_display(self):
        """Update the form display with current values."""
        embed = discord.Embed(title=self.title)

        for field in self.fields:
            field_id = field.get("id")
            field_label = field.get("label", field_id)
            field_value = self.values.get(field_id, "Not set")

            if field.get("type") == "boolean":
                field_value = (
                    "Yes" if field_value is True else "No" if field_value is False else "Not set"
                )

            embed.add_field(
                name=f"{field_label} {'*' if field.get('required', False) else ''}",
                value=str(field_value),
                inline=False,
            )

        if self.message:
            await self.message.edit(embed=embed, view=self)

    async def update_from_state(self, state):
        """Update form display when state changes."""
        await self._update_form_display()


class PaginatedView(StatefulView):
    """A view for paginated content.

    Pages can be:
    - ``discord.Embed`` objects
    - ``str`` for plain text content
    - ``dict`` with keys ``"embed"`` and/or ``"content"`` for mixed content

    When the page count exceeds ``jump_threshold``, first/last jump buttons
    and a go-to-page modal button are shown automatically.

    Use the ``from_data`` classmethod to auto-paginate a list of items.
    """

    # Page count above which first/last and go-to-page buttons appear
    jump_threshold: int = 5

    def __init__(self, *args, pages=None, **kwargs):
        super().__init__(*args, **kwargs)

        self.pages = pages or []
        self.current_page = 0

        self._add_navigation_buttons()

    @classmethod
    async def from_data(
        cls,
        items: list,
        per_page: int,
        formatter: Callable,
        **kwargs,
    ) -> "PaginatedView":
        """Create a PaginatedView by chunking items and applying a formatter.

        Parameters
        ----------
        items:
            The full list of items to paginate.
        per_page:
            Number of items per page.
        formatter:
            A sync or async callable that receives a list of items for one page
            and returns an ``Embed``, ``str``, or page dict.
        **kwargs:
            Forwarded to the PaginatedView constructor (context, timeout, etc.).
        """
        chunks = [items[i : i + per_page] for i in range(0, len(items), per_page)]
        pages = []
        for chunk in chunks:
            if asyncio.iscoroutinefunction(formatter):
                pages.append(await formatter(chunk))
            else:
                pages.append(formatter(chunk))
        return cls(pages=pages, **kwargs)

    def _add_navigation_buttons(self):
        """Add navigation buttons to the view.

        When page count exceeds ``jump_threshold``, adds first/last jump
        buttons and a go-to-page button. All navigation buttons are placed
        on row 0 (max 5 buttons).
        """
        total = len(self.pages)
        show_jump = total > self.jump_threshold

        # First button (conditional)
        if show_jump:
            first_btn = StatefulButton(
                label="\u23ee",
                style=discord.ButtonStyle.secondary,
                custom_id="paginated_first",
                disabled=True,
                row=0,
            )

            async def first_callback(interaction):
                self.current_page = 0
                await interaction.response.defer()
                await self._update_page()

            first_btn.callback = first_callback
            self.add_item(first_btn)

        # Previous button (always)
        prev_btn = StatefulButton(
            label="\u25c0",
            style=discord.ButtonStyle.secondary,
            custom_id="paginated_prev",
            disabled=True,
            row=0,
        )

        async def prev_callback(interaction):
            self.current_page = max(0, self.current_page - 1)
            await interaction.response.defer()
            await self._update_page()

        prev_btn.callback = prev_callback
        self.add_item(prev_btn)

        # Page indicator OR go-to-page button
        page_label = f"1/{max(total, 1)}"
        if show_jump:
            goto_btn = StatefulButton(
                label=page_label,
                style=discord.ButtonStyle.primary,
                custom_id="paginated_goto",
                row=0,
            )

            async def goto_callback(interaction):
                await self._open_goto_modal(interaction)

            goto_btn.callback = goto_callback
            self.add_item(goto_btn)
        else:
            indicator = discord.ui.Button(
                label=f"Page {page_label}",
                style=discord.ButtonStyle.secondary,
                custom_id="paginated_indicator",
                disabled=True,
                row=0,
            )
            self.add_item(indicator)

        # Next button (always)
        next_btn = StatefulButton(
            label="\u25b6",
            style=discord.ButtonStyle.secondary,
            custom_id="paginated_next",
            disabled=total <= 1,
            row=0,
        )

        async def next_callback(interaction):
            self.current_page = min(len(self.pages) - 1, self.current_page + 1)
            await interaction.response.defer()
            await self._update_page()

        next_btn.callback = next_callback
        self.add_item(next_btn)

        # Last button (conditional)
        if show_jump:
            last_btn = StatefulButton(
                label="\u23ed",
                style=discord.ButtonStyle.secondary,
                custom_id="paginated_last",
                disabled=total <= 1,
                row=0,
            )

            async def last_callback(interaction):
                self.current_page = len(self.pages) - 1
                await interaction.response.defer()
                await self._update_page()

            last_btn.callback = last_callback
            self.add_item(last_btn)

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

    def _extract_page(self, page) -> dict:
        """Extract embed/content kwargs from a page entry.

        Only includes keys that are actually present — omitted keys won't
        be sent to the API, preserving existing message fields.
        """
        result = {}
        if isinstance(page, dict):
            if page.get("embed") is not None:
                result["embed"] = page["embed"]
            if page.get("content") is not None:
                result["content"] = page["content"]
        elif isinstance(page, discord.Embed):
            result["embed"] = page
        elif isinstance(page, str):
            result["content"] = page
        return result

    async def send(self, content=None, *, embed=None, embeds=None, ephemeral=False):
        """Send the view, using the first page as the initial content if not specified."""
        if self.pages and embed is None and content is None:
            page_kwargs = self._extract_page(self.pages[0])
            embed = page_kwargs.get("embed")
            content = page_kwargs.get("content")
        return await super().send(content=content, embed=embed, embeds=embeds, ephemeral=ephemeral)

    async def _update_page(self):
        """Update the current page display."""
        if not self.pages:
            return

        page_kwargs = self._extract_page(self.pages[self.current_page])
        total = len(self.pages)
        at_first = self.current_page == 0
        at_last = self.current_page >= total - 1

        for item in self.children:
            cid = getattr(item, "custom_id", None)
            if cid == "paginated_prev":
                item.disabled = at_first
            elif cid == "paginated_next":
                item.disabled = at_last
            elif cid == "paginated_first":
                item.disabled = at_first
            elif cid == "paginated_last":
                item.disabled = at_last
            elif cid == "paginated_indicator":
                item.label = f"Page {self.current_page + 1}/{total}"
            elif cid == "paginated_goto":
                item.label = f"{self.current_page + 1}/{total}"

        if self.message:
            await self.message.edit(**page_kwargs, view=self)

    async def update_from_state(self, state):
        """Update pagination when state changes."""
        await self._update_page()
