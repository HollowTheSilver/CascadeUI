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
| `info_color` | `discord.Color` | varies by theme |
| `warning_color` | `discord.Color` | varies by theme |
| `header_emoji` | `str` | `""` |
| `footer_text` | `str` | `""` |
| `accent_colour` | `discord.Color` | same as `primary_color` |
| `separator_spacing` | `str` | `"small"` |
| `button_styles` | `dict` | Maps `"primary"`, `"secondary"`, `"success"`, `"danger"`, `"link"` to matching `ButtonStyle` |

Additional properties can be set freely via the `styles` dict.

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

#### `create_button(label, button_type="primary", **kwargs)`

Creates a `StatefulButton` styled according to the theme's `button_styles` mapping.

- `label` (str): Button label
- `button_type` (str): Key into `button_styles` (e.g., `"primary"`, `"danger"`)
- `**kwargs`: Passed through to `StatefulButton` (e.g., `callback`, `custom_id`, `row`)

**Returns:** A `StatefulButton` instance.

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

---

## Built-in Themes

All three are auto-registered on import. `"default"` is set as the default theme.

| Name | Primary Color | Accent Colour | Header Emoji | Footer Text |
|------|--------------|---------------|--------------|-------------|
| `default` | Blue | Blue | *(none)* | "Powered by CascadeUI" |
| `dark` | Purple | Purple | Moon | "Powered by CascadeUI (Dark Theme)" |
| `light` | Gold | Gold | Sun | "Powered by CascadeUI (Light Theme)" |
