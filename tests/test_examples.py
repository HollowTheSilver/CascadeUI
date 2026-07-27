"""Guards on the shipped example cogs.

The examples are reference material users copy verbatim, but nothing else in
the suite loads them. An example that raises on import, or whose view rejects
the arguments the cog beside it constructs, ships broken and is only caught by
running the bot.
"""

# // ========================================( Modules )======================================== // #


import importlib.util
import io
import pathlib
import sys

import discord
import pytest

# // ========================================( Constants )======================================== // #


EXAMPLES_DIR = pathlib.Path(__file__).resolve().parent.parent / "examples"
EXAMPLE_PATHS = sorted(EXAMPLES_DIR.glob("*.py"))
EXAMPLE_IDS = [p.stem for p in EXAMPLE_PATHS]


# // ========================================( Functions )======================================== // #


def load_example(path: pathlib.Path):
    """Import an example by path without placing it on ``sys.path``."""
    spec = importlib.util.spec_from_file_location(f"_example_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


def _png(name: str) -> discord.File:
    return discord.File(io.BytesIO(b"\x89PNG"), filename=name)


# // ========================================( Class )======================================== // #


class TestExamplesImport:
    """Every example module executes at import time."""

    def test_examples_directory_is_populated(self):
        # Guards the parametrize below: an empty glob would make every other
        # test in this file vacuously pass.
        assert len(EXAMPLE_PATHS) >= 18, f"found only {len(EXAMPLE_PATHS)} examples"

    @pytest.mark.parametrize("path", EXAMPLE_PATHS, ids=EXAMPLE_IDS)
    def test_example_imports(self, path):
        # Class-body validation (__init_subclass__) runs here, so a bad policy
        # value or an invalid nav_rebuild fails at import rather than at click.
        load_example(path)


# // ========================================( Class )======================================== // #


class TestAttachmentExampleViews:
    """``v2_attachments`` view constructors accept what its commands build.

    An import check cannot see this: the arguments are assembled inside the
    command callback, so a producer/consumer mismatch between the two halves
    of one file stays invisible until the command is invoked.
    """

    @pytest.fixture()
    def module(self):
        return load_example(EXAMPLES_DIR / "v2_attachments.py")

    def test_command_labels_feed_the_view(self, module):
        # The seam that actually broke: the command built newline-joined
        # strings while the view unpacked (name, role) pairs. Both halves were
        # valid on their own, so only running them together catches it.
        labels = module._section_labels("Invoker Name", "Bot Name")
        view = module._SectionView(
            user_id=1,
            guild_id=2,
            files=[_png("a.png"), _png("b.png")],
            labels=labels,
        )
        assert len(view.children) >= 1

    def test_section_view_takes_two_part_labels(self, module):
        # ``attach_section`` builds (name, role) pairs and the view unpacks
        # them into image_section's two text children. A single newline-joined
        # string raises "too many values to unpack".
        view = module._SectionView(
            user_id=1,
            guild_id=2,
            files=[_png("a.png"), _png("b.png")],
            labels=[("**Invoker**", "Invoker"), ("**Bot**", "Bot")],
        )
        texts = [
            item.content
            for item in view.walk_children()
            if isinstance(item, discord.ui.TextDisplay)
        ]
        assert "**Invoker**" in texts
        assert "Invoker" in texts

    def test_section_view_rejects_newline_joined_labels(self, module):
        # The pre-fix shape. Kept so a revert to newline-joined strings fails
        # here instead of at the slash command.
        with pytest.raises(ValueError):
            module._SectionView(
                user_id=1,
                guild_id=2,
                files=[_png("a.png"), _png("b.png")],
                labels=["**Invoker**\nInvoker", "**Bot**\nBot"],
            )

    def test_section_view_passes_placement_validation(self, module):
        from cascadeui.views._placement import validate_placement

        view = module._SectionView(
            user_id=1,
            guild_id=2,
            files=[_png("a.png"), _png("b.png")],
            labels=[("**Invoker**", "Invoker"), ("**Bot**", "Bot")],
        )
        validate_placement(view)
