"""Tests for emoji_grid() / EmojiGrid — axis matrix, cell API, validation."""

import discord
import pytest

from cascadeui.components.patterns import EmojiGrid, emoji_grid

# // ========================================( Construction )======================================== // #


def test_is_textdisplay_subclass():
    """EmojiGrid must be a live TextDisplay so it plugs into card() directly."""
    grid = emoji_grid(3, 3)
    assert isinstance(grid, discord.ui.TextDisplay)
    assert isinstance(grid, EmojiGrid)
    assert grid.content == grid._render()


def test_public_shape_attrs():
    """rows/cols exposed as read-only public attributes for introspection."""
    grid = emoji_grid(4, 7)
    assert grid.rows == 4
    assert grid.cols == 7


# // ========================================( Axis matrix )======================================== // #


def test_no_labels():
    """rows and cols alone produce a pure cell grid with no headers."""
    grid = emoji_grid(2, 3, fill="-")
    assert grid.content == "- - -\n- - -"


def test_row_labels_only():
    """Row labels prefix each row; no header row is emitted."""
    grid = emoji_grid(2, 2, fill=".", row_labels="alpha")
    assert grid.content == "\U0001f1e6 . .\n\U0001f1e7 . ."


def test_col_labels_only_header_is_flush():
    """Col labels without row labels emit a header flush with data rows.

    With no row-label strip there is no intersection cell, so the header
    row must not prepend a corner -- it would leave the header one cell
    wider than the data rows below.
    """
    grid = emoji_grid(2, 2, fill=".", col_labels="numeric")
    assert grid.content == "1\ufe0f\u20e3 2\ufe0f\u20e3\n. .\n. ."


def test_both_labels():
    """Both axes produce corner + col header and row prefixes on each row."""
    grid = emoji_grid(2, 2, fill="\u2b1b", row_labels="alpha", col_labels="numeric")
    expected = (
        "\u2b1b 1\ufe0f\u20e3 2\ufe0f\u20e3\n"
        "\U0001f1e6 \u2b1b \u2b1b\n"
        "\U0001f1e7 \u2b1b \u2b1b"
    )
    assert grid.content == expected


# // ========================================( Validation )======================================== // #


def test_dimension_floor():
    with pytest.raises(ValueError, match="rows and cols must be >= 1"):
        emoji_grid(0, 5)
    with pytest.raises(ValueError, match="rows and cols must be >= 1"):
        emoji_grid(5, 0)


def test_alpha_preset_cap():
    with pytest.raises(ValueError, match="alpha preset supports up to 26"):
        emoji_grid(27, 5, row_labels="alpha")


def test_numeric_preset_cap():
    with pytest.raises(ValueError, match="numeric preset supports up to 10"):
        emoji_grid(5, 11, col_labels="numeric")


def test_custom_sequence_length_mismatch():
    with pytest.raises(ValueError, match="row_labels length 2 does not match"):
        emoji_grid(3, 3, row_labels=["a", "b"])


def test_corner_without_both_labels_raises():
    """Explicitly passing corner= requires both row_labels and col_labels."""
    # Neither axis labeled.
    with pytest.raises(ValueError, match="corner is only rendered when both"):
        emoji_grid(3, 3, corner="\U0001f3f4")
    # Only col labels -- header is flush with data rows, no corner slot.
    with pytest.raises(ValueError, match="corner is only rendered when both"):
        emoji_grid(3, 3, col_labels="numeric", corner="\U0001f3f4")
    # Only row labels -- no header row, nothing for the corner to anchor.
    with pytest.raises(ValueError, match="corner is only rendered when both"):
        emoji_grid(3, 3, row_labels="alpha", corner="\U0001f3f4")


def test_content_length_cap():
    """Oversized grids must fail at construction, not at Discord send."""
    with pytest.raises(ValueError, match="exceeds 4000-character"):
        emoji_grid(60, 60, fill="\U0001f7e6\U0001f7e6")


# // ========================================( Cell API )======================================== // #


def test_int_key_assignment_and_get():
    grid = emoji_grid(3, 3, fill=".")
    grid[4] = "X"
    assert grid[4] == "X"
    # Row 1, col 1 should now be X in the rendered content.
    assert grid.content.split("\n")[1] == ". X ."


def test_tuple_key_assignment():
    grid = emoji_grid(3, 3, fill=".")
    grid[(2, 0)] = "O"
    assert grid[(2, 0)] == "O"
    assert grid[6] == "O"  # Flat equivalence.


def test_iterable_bulk_assignment_single_rebuild():
    """Bulk assign accepts any iterable of keys, mixed int and tuple."""
    grid = emoji_grid(3, 3, fill=".")
    grid[[0, (1, 1), 8]] = "#"
    assert grid[0] == "#"
    assert grid[(1, 1)] == "#"
    assert grid[8] == "#"
    # Non-targeted cells remain fill.
    assert grid[1] == "."


def test_fill_rect_inclusive_bounds():
    grid = emoji_grid(4, 4, fill=".")
    grid.fill_rect((1, 1), (2, 2), "#")
    # Inclusive corners: (1,1) (1,2) (2,1) (2,2) all '#'; rest '.'.
    for idx in (5, 6, 9, 10):
        assert grid[idx] == "#"
    for idx in (0, 1, 2, 3, 4, 7, 8, 11, 12, 13, 14, 15):
        assert grid[idx] == "."


def test_clear_resets_to_fill():
    grid = emoji_grid(3, 3, fill=".")
    grid[[0, 1, 2]] = "#"
    grid.clear()
    assert all(grid[i] == "." for i in range(9))


def test_out_of_range_int_key_raises():
    grid = emoji_grid(3, 3)
    with pytest.raises(IndexError, match="out of range"):
        grid[9] = "X"


def test_out_of_range_tuple_key_raises():
    grid = emoji_grid(3, 3)
    with pytest.raises(IndexError, match="out of range"):
        grid[(3, 0)] = "X"


def test_non_string_value_raises():
    grid = emoji_grid(3, 3)
    with pytest.raises(TypeError, match="cell value must be str"):
        grid[0] = 42
