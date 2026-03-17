"""Tests for component creation and callback wrapping."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from cascadeui.components.base import StatefulButton, StatefulSelect, StatefulComponent
from cascadeui.components.composition import CompositeComponent, register_component, get_component


class TestStatefulComponent:
    def test_button_stores_original_callback(self):
        async def my_cb(interaction):
            pass

        btn = StatefulButton(label="Test", callback=my_cb)
        assert btn.original_callback is my_cb

    def test_button_without_callback(self):
        btn = StatefulButton(label="No CB")
        assert btn.original_callback is None

    def test_select_stores_original_callback(self):
        async def my_cb(interaction):
            pass

        sel = StatefulSelect(options=[discord.SelectOption(label="A", value="a")], callback=my_cb)
        assert sel.original_callback is my_cb

    def test_button_passes_style_through(self):
        btn = StatefulButton(label="Danger", style=discord.ButtonStyle.danger)
        assert btn.style == discord.ButtonStyle.danger


class TestCompositeComponent:
    def test_add_and_retrieve_components(self):
        comp = CompositeComponent()
        btn = StatefulButton(label="Child")
        comp.add_component(btn)
        assert btn in comp.components

    def test_register_and_get_component(self):
        register_component("test_comp", CompositeComponent)
        cls = get_component("test_comp")
        assert cls is CompositeComponent

    def test_get_unknown_component_returns_none(self):
        result = get_component("nonexistent_component_xyz")
        assert result is None
