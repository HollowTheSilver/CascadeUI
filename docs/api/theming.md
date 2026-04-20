# API: Theming

## `Theme`

```python
Theme(name, styles=None)
```

- `name` (str): Theme identifier
- `styles` (dict, optional): Style properties. Missing keys are filled with defaults.

### Default Style Properties

| Property | Type | Default |
|----------|------|---------|
| `primary_color` | `discord.Color` | `Color.blue()` |
| `secondary_color` | `discord.Color` | `Color.light_grey()` |
| `success_color` | `discord.Color` | `Color.green()` |
| `danger_color` | `discord.Color` | `Color.red()` |
| `accent_colour` | `discord.Color` | same as `primary_color` |
| `separator_spacing` | `str` | `"small"` |

Additional properties can be set freely via the `styles` dict (e.g.
`header_emoji`, `footer_text`, or custom keys).

### Instance Attributes

- `name` (str): Theme identifier
- `styles` (dict): Full stylesheet dict

### Methods

#### `get_style(key, default=None)`

Returns the value of a style property, or `default` if not set.

#### `apply_to_embed(embed)`

Applies the theme to a `discord.Embed` (V1):

1. Sets `embed.color` to `primary_color`
2. Prepends `header_emoji` to `embed.title` (if defined and title exists)
3. Sets `embed.set_footer(text=footer_text)` if the embed has no footer and `footer_text` is defined

**Returns:** The modified embed.

#### `apply_to_container(container)`

Applies the theme to a V2 `Container`:

1. Sets `container.accent_colour` to the theme's `accent_colour` style

**Returns:** The modified container.

---

## Functions

### `register_theme(theme)`

Registers a `Theme` in the global registry by name.

### `get_theme(name)`

Looks up a registered theme by name. Returns `None` if not found.

### `set_default_theme(name)`

Sets the global default theme. Returns `True` if the theme exists, `False` otherwise.

### `get_default_theme()`

Returns the current default `Theme` instance, or `None` if no default is set.

### `get_current_theme()`

Returns the `Theme` active in the current execution context. Inside a view's
`build_ui()` method, returns the view's theme. Outside a view context, returns
`None`.

Builder functions like `card()` and `stats_card()` call this internally as a
fallback when no explicit `color=` is passed.

---

## Built-in Themes

All three are auto-registered on import. `"default"` is set as the default theme.

| Name | Primary Color | Accent Colour | Header Emoji |
|------|--------------|---------------|--------------|
| `default` | Blue | Blue | *(none)* |
| `dark` | Purple | Purple | Moon |
| `light` | Gold | Gold | Sun |
