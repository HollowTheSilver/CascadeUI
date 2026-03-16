# Theming

CascadeUI includes a theming system for consistent visual styling across views.

## Defining a Theme

A `Theme` is a named collection of style properties:

```python
from cascadeui import Theme

my_theme = Theme("corporate", {
    "primary_color": discord.Color.blue(),
    "header_emoji": ">>",
    "footer_text": "Acme Bot v2",
})
```

## Registering Themes

Register themes globally so they can be referenced by name:

```python
from cascadeui import register_theme, set_default_theme

register_theme(my_theme)
set_default_theme("corporate")  # All views use this unless overridden
```

### Built-in Themes

CascadeUI includes three built-in themes:

- `default` - neutral styling
- `dark` - dark color scheme
- `light` - light color scheme

## Per-View Theming

Override the default theme for a specific view:

```python
view = MyView(context=ctx, theme=my_theme)
```

Or look up a registered theme:

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

`get_theme()` checks for a per-view theme first, then falls back to the global default. This prevents cross-user interference when different users have different theme preferences.
