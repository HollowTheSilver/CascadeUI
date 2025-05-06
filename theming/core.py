
# // ========================================( Modules )======================================== // #


from typing import Dict, Any, Optional, Union
import discord
from discord import Color, ButtonStyle


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
                "link": ButtonStyle.link
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

    def create_button(self,
                      label: str,
                      button_type: str = "primary",
                      **kwargs) -> discord.ui.Button:
        """Create a themed button."""
        from ..components.base import StatefulButton

        # Get button style from theme
        style_map = self.get_style("button_styles", {})
        style = style_map.get(button_type, ButtonStyle.secondary)

        return StatefulButton(label=label, style=style, **kwargs)


# Global theme registry
_themes = {}
_current_theme = None


def register_theme(theme: Theme) -> None:
    """Register a theme in the global registry."""
    _themes[theme.name] = theme


def get_theme(name: str) -> Optional[Theme]:
    """Get a theme from the registry."""
    return _themes.get(name)


def set_current_theme(name: str) -> bool:
    """Set the current theme by name."""
    global _current_theme
    if name in _themes:
        _current_theme = _themes[name]
        return True
    return False


def get_current_theme() -> Optional[Theme]:
    """Get the current theme."""
    return _current_theme
