# Theming

CascadeUI includes a theming system for consistent visual styling across views.
Themes define colors and embed formatting that can be applied globally or
per-view.

## Defining a Theme

A `Theme` is a named collection of style properties:

```python
from cascadeui import Theme

my_theme = Theme("corporate", {
    "primary_color": discord.Color.blue(),
    "secondary_color": discord.Color.light_grey(),
    "success_color": discord.Color.green(),
    "danger_color": discord.Color.red(),
    "accent_colour": discord.Color.dark_blue(),
    "header_emoji": ">>",
})
```

### Style Properties

Themes support the following built-in style keys:

| Property | Type | Default | Description |
|----------|------|---------|-------------|
| `primary_color` | `discord.Color` | `Color.blue()` | Main embed color |
| `secondary_color` | `discord.Color` | `Color.light_grey()` | Secondary/muted color |
| `success_color` | `discord.Color` | `Color.green()` | Success state color |
| `danger_color` | `discord.Color` | `Color.red()` | Error/danger state color |
| `accent_colour` | `discord.Color` | same as `primary_color` | V2 container accent color |
| `separator_spacing` | `str` | `"small"` | Default V2 separator spacing |
| `header_emoji` | `str` | *(none)* | Prepended to embed titles |
| `footer_text` | `str` | *(none)* | Default embed footer text |

Themes also accept custom keys. Access any property with
`theme.get_style(key, default)`.

## Registering Themes

Register themes globally so they can be referenced by name:

```python
from cascadeui import register_theme, set_default_theme

register_theme(my_theme)
set_default_theme("corporate")  # All views use this unless overridden
```

### Built-in Themes

CascadeUI ships with three built-in themes, all auto-registered on import:

| Name | Primary Color | Accent | Header |
|------|--------------|--------|--------|
| `default` | Blue | Blue | *(none)* |
| `dark` | Purple | Purple | Moon emoji |
| `light` | Gold | Gold | Sun emoji |

The `default` theme is active unless you call `set_default_theme()`.

## Per-View Theming

### Constructor Argument

Pass a theme when creating a view:

```python
view = MyView(user_id=ctx.author.id, theme=my_theme)
```

### Class-Level Attribute

Set the theme on the class body for all instances:

```python
from cascadeui import StatefulLayoutView, dark_theme

class DarkDashboard(StatefulLayoutView):
    theme = dark_theme

    def build_ui(self):
        # card() automatically picks up dark_theme's purple accent
        self.add_item(card("## Dashboard", "Welcome back!"))
```

The class attribute is validated at definition time -- `theme = "dark"` raises
`TypeError` immediately, catching the mistake before any user clicks a button.

A `theme=` constructor argument overrides the class attribute.

### Accessing the Theme

```python
theme = self.get_theme()  # Per-view > global default > bare fallback
```

## Automatic Theme Propagation

Inside `build_ui()`, the view's theme is set on a context variable
automatically. Builder functions like `card()` and `stats_card()` read this
context as a fallback when no explicit `color=` is passed:

```python
class ThemedView(StatefulLayoutView):
    theme = dark_theme  # Purple accent

    def build_ui(self):
        self.clear_items()
        # Both cards get purple accent automatically -- no color= needed
        self.add_item(card("## Section One", "Content here"))
        self.add_item(card("## Section Two", "More content"))
```

Explicit `color=` always wins:

```python
# This card is green regardless of the view's theme
self.add_item(card("## Override", color=discord.Color.green()))
```

Outside a view context (e.g. top-level code), `card()` produces containers
with no accent color.

### Reading the Theme Context

For custom builder functions or helpers that need to read the active theme:

```python
from cascadeui import get_current_theme

def my_custom_card(title, content):
    theme = get_current_theme()
    color = theme.get_style("accent_colour") if theme else None
    return Container(TextDisplay(f"## {title}"), TextDisplay(content), accent_colour=color)
```

## Applying a Theme to Embeds

Use `theme.apply_to_embed()` to style a V1 embed:

```python
class MyView(StatefulView):
    def build_embed(self):
        embed = discord.Embed(title="Dashboard")
        theme = self.get_theme()
        theme.apply_to_embed(embed)
        return embed
```

`apply_to_embed()` sets the embed color to `primary_color` and optionally
prepends `header_emoji` to the title and sets `footer_text` as the footer.

## V2 Container Theming

For V2 views, the `card()` helper is the primary way to create themed
containers. With automatic theme propagation, most views need no manual color
handling at all:

```python
class MyView(StatefulLayoutView):
    theme = my_theme

    def build_ui(self):
        self.clear_items()
        # card() reads the theme's accent_colour automatically
        self.add_item(card("## Server Info", key_value({"Members": 42})))
```

For cases where you need to apply a theme to a container directly:

```python
theme = self.get_theme()
container = Container(...)
theme.apply_to_container(container)  # Sets accent_colour from theme
```

## Theme-Aware Patterns

Use `get_style()` to read semantic colors for state-driven styling:

```python
class StatusView(StatefulView):
    def build_embed(self, status):
        theme = self.get_theme()
        embed = discord.Embed(title="Server Status")

        # apply_to_embed sets primary_color, header_emoji, footer_text.
        # Call it first, then override the color with the semantic value.
        theme.apply_to_embed(embed)

        if status == "online":
            embed.color = theme.get_style("success_color")
            embed.description = "All systems operational."
        else:
            embed.color = theme.get_style("danger_color")
            embed.description = "Outage detected."

        return embed
```

Using `get_style()` instead of hardcoded colors means the same view renders
correctly under any theme.
