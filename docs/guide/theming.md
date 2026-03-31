# Theming

CascadeUI includes a theming system for consistent visual styling across views. Themes define colors, button styles, and embed formatting that can be applied globally or per-view.

## Defining a Theme

A `Theme` is a named collection of style properties:

```python
from cascadeui import Theme

my_theme = Theme("corporate", {
    "primary_color": discord.Color.blue(),
    "secondary_color": discord.Color.light_grey(),
    "success_color": discord.Color.green(),
    "danger_color": discord.Color.red(),
    "header_emoji": ">>",
    "footer_text": "Acme Bot v2",
})
```

### Style Properties

Themes support the following style keys:

| Property | Type | Default | Description |
|----------|------|---------|-------------|
| `primary_color` | `discord.Color` | `Color.blue()` | Main embed color |
| `secondary_color` | `discord.Color` | `Color.light_grey()` | Secondary/muted color |
| `success_color` | `discord.Color` | `Color.green()` | Success state color |
| `danger_color` | `discord.Color` | `Color.red()` | Error/danger state color |
| `info_color` | `discord.Color` | varies | Informational color |
| `warning_color` | `discord.Color` | varies | Warning state color |
| `header_emoji` | `str` | `""` | Prepended to embed titles |
| `footer_text` | `str` | `""` | Default embed footer text |
| `accent_colour` | `discord.Color` | same as `primary_color` | V2 container accent color |
| `separator_spacing` | `str` | `"small"` | Default V2 separator spacing |
| `button_styles` | `dict` | see below | Maps button type names to `ButtonStyle` |

The `button_styles` dict maps logical names to discord.py button styles:

```python
# Default button_styles mapping
{
    "primary": discord.ButtonStyle.primary,
    "secondary": discord.ButtonStyle.secondary,
    "success": discord.ButtonStyle.success,
    "danger": discord.ButtonStyle.danger,
    "link": discord.ButtonStyle.link,
}
```

You can access any style property with `theme.get_style(key, default)`.

## Registering Themes

Register themes globally so they can be referenced by name:

```python
from cascadeui import register_theme, set_default_theme

register_theme(my_theme)
set_default_theme("corporate")  # All views use this unless overridden
```

### Built-in Themes

CascadeUI ships with three built-in themes, all auto-registered on import:

| Name | Primary Color | Header | Description |
|------|--------------|--------|-------------|
| `default` | Blue | *(none)* | Neutral styling with standard Discord colors |
| `dark` | Purple | Moon emoji | Dark color scheme with purple/teal tones |
| `light` | Gold | Sun emoji | Light color scheme with warm tones |

The `default` theme is active unless you call `set_default_theme()`.

## Per-View Theming

Override the default theme for a specific view:

```python
view = MyView(context=ctx, theme=my_theme)
```

Or look up a registered theme by name:

```python
from cascadeui import get_theme

dark = get_theme("dark")
view = MyView(context=ctx, theme=dark)
```

## Applying a Theme to Embeds

Use `theme.apply_to_embed()` to style an embed with the theme's colors and formatting:

```python
class MyView(StatefulView):
    async def build_embed(self):
        embed = discord.Embed(title="Dashboard")
        theme = self.get_theme()  # Falls back to default if no per-view theme
        theme.apply_to_embed(embed)
        return embed
```

`apply_to_embed()` does three things:

1. Sets the embed's color to `primary_color`
2. Prepends `header_emoji` to the embed title (if the theme defines one)
3. Sets the footer to `footer_text` (only if the embed doesn't already have a footer)

The method returns the embed, so you can chain it:

```python
embed = theme.apply_to_embed(discord.Embed(title="Stats"))
```

`get_theme()` checks for a per-view theme first, then falls back to the global default. This prevents cross-user interference when different users have different theme preferences.

## Creating Themed Buttons

Themes can create buttons with consistent styling:

```python
theme = self.get_theme()
button = theme.create_button(
    label="Confirm",
    button_type="success",       # Maps to theme's button_styles
    callback=self.on_confirm,
)
self.add_item(button)
```

The `button_type` parameter maps to the theme's `button_styles` dict. This lets you define semantic button types ("primary", "danger", "success") that resolve to different `ButtonStyle` values depending on the active theme.

## Theme-Aware Patterns

A common pattern is building embeds that use different theme colors for different states:

```python
class StatusView(StatefulView):
    async def build_embed(self, status):
        theme = self.get_theme()
        embed = discord.Embed(title="Server Status")

        if status == "online":
            embed.color = theme.get_style("success_color")
            embed.description = "All systems operational."
        elif status == "degraded":
            embed.color = theme.get_style("warning_color")
            embed.description = "Some services are slow."
        else:
            embed.color = theme.get_style("danger_color")
            embed.description = "Outage detected."

        theme.apply_to_embed(embed)  # Adds header/footer
        return embed
```

Using `get_style()` instead of hardcoded colors means the same view renders correctly under any theme.

## V2 Container Theming

For V2 views, themes can set accent colors on containers:

```python
from discord.ui import Container

class MyView(StatefulLayoutView):
    def _build_ui(self):
        theme = self.get_theme()
        container = Container(...)
        theme.apply_to_container(container)  # Sets accent_colour from theme
        self.add_item(container)
```

`apply_to_container()` reads the theme's `accent_colour` style and applies it to the container. Returns the container for chaining.

The `card()` helper accepts a `color` parameter directly, which is the simpler approach when you don't need theme-driven colors:

```python
from cascadeui import card

# Direct color — most common
self.add_item(card("## Title", color=discord.Color.green()))

# Theme-driven color
theme = self.get_theme()
self.add_item(card("## Title", color=theme.get_style("accent_colour")))
```

All three built-in themes include `accent_colour`: `default` (blue), `dark` (purple), `light` (gold).
