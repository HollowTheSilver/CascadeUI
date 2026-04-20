# // ========================================( Modules )======================================== // #


from typing import Any, Dict, Optional

import discord
from discord import Color

# // ========================================( Classes )======================================== // #


class Theme:
    """Defines styling for UI components."""

    def __init__(self, name: str, styles: Dict[str, Any] = None) -> None:
        self.name = name
        self.styles = styles or {}

        # Set default styles if not provided
        if "primary_color" not in self.styles:
            self.styles["primary_color"] = Color.blue()
        if "secondary_color" not in self.styles:
            self.styles["secondary_color"] = Color.light_grey()
        if "success_color" not in self.styles:
            self.styles["success_color"] = Color.green()
        if "danger_color" not in self.styles:
            self.styles["danger_color"] = Color.red()

        # V2 Container styling -- defaults derived from V1 colors
        if "accent_colour" not in self.styles:
            self.styles["accent_colour"] = self.styles.get("primary_color")
        if "separator_spacing" not in self.styles:
            self.styles["separator_spacing"] = "small"

    def get_style(self, key: str, default: Any = None) -> Any:
        """Get a style value from the theme."""
        return self.styles.get(key, default)

    def apply_to_embed(self, embed: discord.Embed) -> discord.Embed:
        """Apply theme styling to an embed."""
        embed.color = self.get_style("primary_color")

        # Apply other embed styling as needed
        header_emoji = self.get_style("header_emoji")
        if header_emoji and embed.title:
            embed.title = f"{header_emoji} {embed.title}"

        footer_text = self.get_style("footer_text")
        if footer_text and not embed.footer:
            embed.set_footer(text=footer_text)

        return embed

    def apply_to_container(self, container) -> Any:
        """Apply theme accent colour to a V2 Container.

        Sets the Container's ``accent_colour`` from the theme's
        ``accent_colour`` style. Returns the container for chaining.
        """
        accent = self.get_style("accent_colour")
        if accent is not None:
            container.accent_colour = accent
        return container


# Global theme registry
_themes: Dict[str, Theme] = {}
_default_theme: Optional[Theme] = None


def register_theme(theme: Theme) -> None:
    """Register a theme in the global registry."""
    _themes[theme.name] = theme


def get_theme(name: str) -> Optional[Theme]:
    """Get a theme from the registry."""
    return _themes.get(name)


def set_default_theme(name: str) -> bool:
    """Set the default theme used when no per-view theme is specified."""
    global _default_theme
    if name in _themes:
        _default_theme = _themes[name]
        return True
    return False


def get_default_theme() -> Optional[Theme]:
    """Get the default theme."""
    return _default_theme
