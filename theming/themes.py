
# // ========================================( Modules )======================================== // #


from discord import Color
from .core import Theme, register_theme, set_current_theme


# // ========================================( Script )======================================== // #


# Default theme
default_theme = Theme("default", {
    "primary_color": Color.blue(),
    "secondary_color": Color.light_grey(),
    "success_color": Color.green(),
    "danger_color": Color.red(),
    "info_color": Color.blurple(),
    "warning_color": Color.gold(),

    "header_emoji": "",
    "footer_text": "Powered by CascadeUI"
})

# Dark theme
dark_theme = Theme("dark", {
    "primary_color": Color.dark_blue(),
    "secondary_color": Color.dark_grey(),
    "success_color": Color.dark_green(),
    "danger_color": Color.dark_red(),
    "info_color": Color.dark_blue(),
    "warning_color": Color.gold(),

    "header_emoji": "🌙",
    "footer_text": "Powered by CascadeUI (Dark Theme)"
})

# Light theme
light_theme = Theme("light", {
    "primary_color": Color.blue(),
    "secondary_color": Color.light_grey(),
    "success_color": Color.green(),
    "danger_color": Color.red(),
    "info_color": Color.blue(),
    "warning_color": Color.gold(),

    "header_emoji": "☀️",
    "footer_text": "Powered by CascadeUI (Light Theme)"
})

# Register built-in themes
register_theme(default_theme)
register_theme(dark_theme)
register_theme(light_theme)

# Set default theme
set_current_theme("default")
