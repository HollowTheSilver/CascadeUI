# // ========================================( Modules )======================================== // #


from discord import Color

from .core import Theme, register_theme, set_default_theme

# // ========================================( Script )======================================== // #


# Default theme
default_theme = Theme(
    "default",
    {
        "primary_color": Color.blue(),
        "secondary_color": Color.light_grey(),
        "success_color": Color.green(),
        "danger_color": Color.red(),
        "accent_colour": Color.blue(),
    },
)

# Dark theme
dark_theme = Theme(
    "dark",
    {
        "primary_color": Color.purple(),
        "secondary_color": Color.dark_grey(),
        "success_color": Color.blue(),
        "danger_color": Color.orange(),
        "accent_colour": Color.purple(),
        "header_emoji": "\U0001f319",
    },
)

# Light theme
light_theme = Theme(
    "light",
    {
        "primary_color": Color.gold(),
        "secondary_color": Color.light_grey(),
        "success_color": Color.green(),
        "danger_color": Color.orange(),
        "accent_colour": Color.gold(),
        "header_emoji": "\u2600\ufe0f",
    },
)

# Register built-in themes
register_theme(default_theme)
register_theme(dark_theme)
register_theme(light_theme)

# Set default theme
set_default_theme("default")
