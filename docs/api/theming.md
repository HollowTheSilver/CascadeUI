# API: Theming

## `Theme`

```python
Theme(name, properties)
```

- `name` (str): Theme identifier
- `properties` (dict): Style properties (`primary_color`, `header_emoji`, `footer_text`, etc.)

### Methods

#### `apply_to_embed(embed)`

Applies the theme's color, footer, and styling to a `discord.Embed`.

---

## Functions

### `register_theme(theme)`

Registers a `Theme` in the global registry.

### `get_theme(name)`

Looks up a registered theme by name. Raises `KeyError` if not found.

### `set_default_theme(name)`

Sets the global default theme (used when views don't specify one).

### `get_default_theme()`

Returns the current default theme name.

---

## Built-in Themes

| Name | Description |
|------|-------------|
| `default` | Neutral styling |
| `dark` | Dark color scheme |
| `light` | Light color scheme |
