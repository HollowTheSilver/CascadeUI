# // ========================================( Modules )======================================== // #


from typing import Any, Dict, Optional, Union

import discord
from discord import ButtonStyle, Color

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

        # Button style mappings
        if "button_styles" not in self.styles:
            self.styles["button_styles"] = {
                "primary": ButtonStyle.primary,
                "secondary": ButtonStyle.secondary,
                "success": ButtonStyle.success,
                "danger": ButtonStyle.danger,
                "link": ButtonStyle.link,
            }

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

    def create_button(
        self, label: str, button_type: str = "primary", **kwargs
    ) -> discord.ui.Button:
        """Create a themed button."""
        from ..components.base import StatefulButton

        # Get button style from theme
        style_map = self.get_style("button_styles", {})
        style = style_map.get(button_type, ButtonStyle.secondary)

        return StatefulButton(label=label, style=style, **kwargs)


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
