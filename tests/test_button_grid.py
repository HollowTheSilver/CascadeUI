"""Tests for button_grid() — ActionRow packing and Discord 5x5 limit."""

import discord
import pytest

from cascadeui.components.base import StatefulButton
from cascadeui.components.patterns import button_grid


def _make_button(r, c):
    async def cb(interaction):
        pass

    return StatefulButton(label=f"{r},{c}", callback=cb)


# // ========================================( Shape )======================================== // #


def test_square_grid_shape():
    rows = button_grid(3, 3, _make_button)
    assert len(rows) == 3
    assert all(isinstance(row, discord.ui.ActionRow) for row in rows)
    assert all(len(row.children) == 3 for row in rows)


def test_rectangular_grid_3x5():
    rows = button_grid(3, 5, _make_button)
    assert len(rows) == 3
    assert all(len(row.children) == 5 for row in rows)


def test_max_5x5_accepted():
    rows = button_grid(5, 5, _make_button)
    assert len(rows) == 5
    assert sum(len(r.children) for r in rows) == 25


def test_factory_called_with_row_col_coordinates():
    """cell_factory(row, col) is the public contract, not a flat index."""
    seen = []

    def track(r, c):
        seen.append((r, c))
        return _make_button(r, c)

    button_grid(2, 3, track)
    assert seen == [(0, 0), (0, 1), (0, 2), (1, 0), (1, 1), (1, 2)]


# // ========================================( Validation )======================================== // #


def test_exceeds_5_rows():
    with pytest.raises(ValueError, match="5x5 component limit"):
        button_grid(6, 5, _make_button)


def test_exceeds_5_cols():
    with pytest.raises(ValueError, match="5x5 component limit"):
        button_grid(5, 6, _make_button)


def test_non_button_factory_return():
    with pytest.raises(TypeError, match="must return discord.ui.Button"):
        button_grid(3, 3, lambda r, c: "not a button")


def test_zero_dimension_rejected():
    with pytest.raises(ValueError, match="5x5 component limit"):
        button_grid(0, 3, _make_button)
