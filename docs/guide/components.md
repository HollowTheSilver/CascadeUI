# Components

CascadeUI components extend discord.py's built-in UI components with state
dispatching, validation, and composition helpers. They fall into three tiers
based on how they interact with the state store -- see
[Core Concepts -- Component Tiers](concepts.md#component-tiers) for the
tier-by-tier overview, and
[Core Concepts -- Extension Strategies](concepts.md#extension-strategies) for
the subclass / builder / wrapper / pattern taxonomy that governs how each
section below is built.

---

## Interactive Components

These fire `COMPONENT_INTERACTION` actions on every click or selection.

### StatefulButton

Extends `discord.ui.Button` with automatic state dispatching:

```python
from cascadeui import StatefulButton

button = StatefulButton(
    label="Click Me",
    style=discord.ButtonStyle.primary,
    callback=my_handler,
)
```

In V2 views, buttons must be wrapped in `ActionRow`:

```python
from discord.ui import ActionRow

self.add_item(ActionRow(
    StatefulButton(label="Save", callback=self.save),
    StatefulButton(label="Cancel", callback=self.cancel),
))
```

Convenience subclasses: `PrimaryButton`, `SecondaryButton`, `SuccessButton`,
`DangerButton`, `LinkButton`, `ToggleButton`.

#### `owner_only=True` per-button host gate

Pairs with view-level `owner_only=False` to express open-view + host-only-button
flows (lobby Start/Disband, ticket Close, poll End). When set, the button
callback fires only when `interaction.user.id == view.user_id`; mismatches
route through `view.on_unauthorized(interaction)` without invoking the
callback.

```python
from discord.ui import ActionRow

from cascadeui import StatefulLayoutView, StatefulButton

class LobbyView(StatefulLayoutView):
    owner_only = False  # everyone in the channel can see the lobby

    def build_ui(self):
        self.clear_items()
        self.add_item(ActionRow(
            StatefulButton(label="Join", callback=self.join),
            StatefulButton(
                label="Start",
                callback=self.start,
                owner_only=True,  # only the lobby host
            ),
        ))
```

Anonymous views (no `view.user_id`) skip the gate entirely so background
or system-driven flows still work. Defaults to `False`, so omitting the
kwarg leaves the standard callback contract unchanged.

### StatefulSelect

Extends `discord.ui.Select` with state integration:

```python
from cascadeui import StatefulSelect

select = StatefulSelect(
    placeholder="Pick one...",
    options=[
        discord.SelectOption(label="Option A", value="a"),
        discord.SelectOption(label="Option B", value="b"),
    ],
    callback=my_handler,
)
```

Specialized variants: `Dropdown` (alias), `RoleSelect`, `ChannelSelect`,
`UserSelect`, `MentionableSelect`.

#### `set_selected(value)` / `get_selected()`

Programmatic state reflection for selects:

```python
# Set which options are marked as default
select.set_selected("a")             # Single value
select.set_selected(["a", "b"])      # Multiple values (max_values > 1)
select.set_selected(None)            # Clear all selections

# Read current selections
selected = select.get_selected()     # Returns list[str]
```

Values that don't match any existing option are silently ignored, so
state-driven rebuilds survive config migrations.

#### Two-Parameter Callbacks

`StatefulSelect` callbacks may accept an optional second parameter `values`:

```python
async def on_select(interaction, values):
    selected = values[0]  # list[str] of selected option values
    ...
```

Detection happens at creation time via `inspect.signature`. Single-parameter
callbacks still work unchanged.

#### Pre-populated defaults (specialized selects)

`RoleSelect`, `UserSelect`, `ChannelSelect`, and `MentionableSelect`
each accept a `default_values=` constructor kwarg that pre-marks
entries as selected when the message first renders. CascadeUI accepts
a permissive input shape -- raw `int` IDs, Discord objects (Role,
Member, User, GuildChannel), or pre-built `discord.SelectDefaultValue`
instances -- and wraps each entry with the right type for the select
class.

```python
from cascadeui import RoleSelect, UserSelect, MentionableSelect

# Raw int IDs (auto-typed as 'role')
moderator_select = RoleSelect(default_values=[123456789, 987654321])

# Discord.Member objects (auto-typed as 'user')
admin_select = UserSelect(default_values=[ctx.guild.owner])

# MentionableSelect requires typed objects -- bare ints rejected
mention_select = MentionableSelect(
    default_values=[
        ctx.guild.get_role(role_id),       # auto-typed as 'role'
        ctx.guild.get_member(user_id),     # auto-typed as 'user'
    ],
)
```

Update defaults after construction with `set_default_values(values)`,
which accepts the same input shape and replaces the existing list:

```python
select.set_default_values([new_id_1, new_id_2])
select.set_default_values([])     # clear all defaults
select.set_default_values(None)   # clear all defaults
```

`MentionableSelect` cannot infer type from a bare `int` (could be
either user or role), so raw IDs are rejected with a `TypeError`
naming the supported input shapes. Callers who only have IDs
construct `discord.SelectDefaultValue(id=..., type='user')` (or
`'role'`) explicitly and pass that.

### Passing Context to Callbacks

Use closures or `functools.partial` to pass extra data:

```python
def _make_move(self, cell: int):
    async def callback(interaction):
        self.board[cell] = self.current_mark
        self.build_ui()
        await self.refresh()
    return callback

# Each button gets a unique callback with a different `cell`
for i in range(9):
    button = StatefulButton(label=self.board[i], callback=self._make_move(i))
```

---

## Modal Inputs

These live inside `Modal` dialogs. They have `custom_id` attributes but
`is_dispatchable = False` -- values are collected by the Modal on submit.
All five share the same contract:

- `custom_id` derived from label via `TextInput._slug()`
- Optional `validators` list auto-collected by `Modal`
- Value write-back: `.value` or `.values` populated after submit
- Each renders as a `discord.ui.Label` wrapping the inner input. The
  label string moves to `Label.text`; an optional `description=` kwarg
  populates `Label.description` for a secondary helper line beneath
  the title. The Modal's child tree carries the `ui.Label` wrappers
  directly; CascadeUI unwraps them internally during submit
  collection.

### TextInput

Wraps `discord.ui.TextInput`:

```python
from cascadeui import TextInput

name = TextInput(label="Name", placeholder="Enter your name")
bio = TextInput(label="Bio", style=discord.TextStyle.long)
```

After submit: `name.value` → `str`.

Validators are attached directly:

```python
from cascadeui import TextInput, min_length, regex

name = TextInput(
    label="Username",
    description="Lowercase letters, numbers, and underscores only.",
    validators=[
        min_length(3),
        regex(r"^[a-zA-Z0-9_]+$", "Alphanumeric only"),
    ],
)
```

The `description=` kwarg renders as `Label.description` -- a secondary
helper line beneath the field title. Available on all five wrapped
input types (TextInput, Checkbox, CheckboxGroup, RadioGroup,
FileUpload).

### Checkbox

Wraps `discord.ui.Checkbox` -- a single boolean toggle:

```python
from cascadeui import Checkbox

agree = Checkbox(label="I agree to the terms", default=False)
```

After submit: `agree.value` → `bool`.

### CheckboxGroup

Wraps `discord.ui.CheckboxGroup` -- multi-select with labeled options:

```python
from cascadeui import CheckboxGroup

roles = CheckboxGroup(
    label="Preferred Roles",
    options=[
        {"label": "Tank", "value": "tank"},
        {"label": "DPS", "value": "dps"},
        {"label": "Support", "value": "support", "default": True},
    ],
    min_values=1,
    max_values=3,
)
```

Options accept dict shorthand (shown above) or native
`discord.CheckboxGroupOption` instances. After submit: `roles.values` →
`list[str]`.

### RadioGroup

Wraps `discord.ui.RadioGroup` -- single-select with labeled options:

```python
from cascadeui import RadioGroup

difficulty = RadioGroup(
    label="Difficulty",
    options=[
        {"label": "Easy", "value": "easy"},
        {"label": "Normal", "value": "normal", "default": True},
        {"label": "Hard", "value": "hard"},
    ],
)
```

Same dict shorthand as `CheckboxGroup`. After submit: `difficulty.value` →
`str`.

### FileUpload

Wraps `discord.ui.FileUpload`:

```python
from cascadeui import FileUpload

upload = FileUpload(label="Avatar", max_values=1)
```

After submit: `upload.values` → `list[discord.Attachment]`.

!!! warning "Ephemeral attachment URLs"
    `discord.Attachment` objects contain CDN URLs that expire. Read attachment
    data in the modal callback -- do not store attachments in the state store.

### Modal

`Modal` collects all wrapped input types and handles submission:

```python
from cascadeui import Modal, TextInput, Checkbox

name = TextInput(label="Name")
agree = Checkbox(label="Agree to terms")

async def handle(interaction, values):
    print(name.value, agree.value)
    await interaction.response.send_message("Done!", ephemeral=True)

modal = Modal(title="Registration", inputs=[name, agree], callback=handle)
await self.open_modal(interaction, modal)
```

Inside a CascadeUI view callback, use `self.open_modal()` instead of
`interaction.response.send_modal()`. It handles the case where auto-defer
has already consumed the response slot. See
[Opening Modals from Callbacks](views.md#opening-modals-from-callbacks).

After submit, each input's `.value` / `.values` is populated. `modal.values_by_input` provides a dict keyed by input instance.

Validators from all inputs are auto-collected. If any fail, the modal
responds with error messages and blocks submission.

Pass `view_id=self.id` to dispatch a `MODAL_SUBMITTED` action for state
tracking.

---

## Custom Emoji

CascadeUI accepts the same emoji forms as discord.py everywhere a
component takes an `emoji=` argument. The type alias `EmojiInput`
(exported from `cascadeui.components.types`) is
`Optional[Union[str, discord.Emoji, discord.PartialEmoji]]` and matches
the union accepted by `discord.ui.Button` and `discord.SelectOption`.

### Three string forms

| Form | Example | Source |
|---|---|---|
| Unicode | `"⚙️"` or its Python escape form | Standard Unicode emoji |
| Custom static | `"<:fire:1234567890123456789>"` | Guild-owned or application-owned static emoji |
| Custom animated | `"<a:dance:1234567890123456789>"` | Guild-owned or application-owned animated emoji |

discord.py parses all three at the component boundary via
`PartialEmoji.from_str`. A live `discord.Emoji` instance (returned by
`bot.get_emoji` or `bot.fetch_application_emoji`) and a
`discord.PartialEmoji` are accepted directly; `str(emoji_obj)` produces
the matching `<:name:id>` form when a plain string is required.

### Where emoji are accepted

| Surface | Slot |
|---|---|
| `StatefulButton(emoji=...)` | inherited from `discord.ui.Button` |
| `StatefulSelect` option dicts | `"emoji"` key |
| `action_section`, `toggle_section`, `link_section`, `button_row`, `cycle_button`, `toggle_button` | `emoji=` kwarg |
| `confirm_section` | `confirm_emoji=` / `cancel_emoji=` |
| `choice_row` | per option via `Choice(emoji=...)`; the `{label: value}` dict form has no emoji slot |
| `MenuView` / `MenuLayoutView` category dicts | `"emoji"` key |
| `RoleCategory(icon=...)` | rendered as a markdown prefix in the category header |
| `RolesLayoutView` / `PersistentRolesLayoutView` | `format_button_emoji` classmethod returns `EmojiInput` |
| `LeaderboardLayoutView.podium_emojis` | rank → string used in markdown |
| `WizardLayoutView` | `back_button_emoji`, `next_button_emoji`, `finish_button_emoji` |
| `PaginatedLayoutView` | `first_button_emoji` through `last_button_emoji` |
| `FormLayoutView` | `text_edit_button_emoji` |
| `with_loading_state(loading_emoji=...)` | wrapper kwarg |
| Refresh handoff (any view) | `refresh_button_emoji` |

### Application-owned emojis

Custom guild emojis only render where the bot is currently a member of
the source guild. For bots deployed across many independent guilds,
guild-owned assets stop rendering once the bot leaves the guild that
owns them. discord.py supports **application-owned emojis**: emoji
uploaded directly to the bot's application, which render everywhere
the bot operates and never expire on guild membership changes.

```python
# One-time setup. Run once, capture the IDs in config.
import discord

bot = discord.Client(intents=discord.Intents.default())

@bot.event
async def on_ready():
    with open("fire.png", "rb") as f:
        emoji = await bot.create_application_emoji(name="fire", image=f.read())
    print(f"Created: {emoji} (id={emoji.id})")
    await bot.close()

bot.run("TOKEN")
```

Reference the captured ID in any `emoji=` slot:

```python
FIRE_EMOJI = "<:fire:1234567890123456789>"  # captured from the setup run

self.add_item(action_section(
    "Activate trial",
    label="Start",
    emoji=FIRE_EMOJI,
    callback=self._start,
))
```

`Client.fetch_application_emojis()` lists existing application emojis
and `fetch_application_emoji(emoji_id)` retrieves one by ID. Both
return `discord.Emoji` instances; the rendering pipeline treats them
identically to guild emojis.

### Pitfalls

- **Missing angle brackets.** Typing `":fire:1234567890123456789"` instead of
  `"<:fire:1234567890123456789>"` parses as a unicode emoji name and renders
  as literal text.
- **Short emoji IDs fall through to unicode.** discord.py's parser
  requires 13 to 20 digits for the ID portion of a custom emoji string.
  Real Discord snowflakes are 17 to 19 digits. Anything shorter is
  treated as a unicode emoji name and renders as literal text -- this
  catches shortened placeholder values copied without substitution.
- **Wrong emoji ID.** Discord renders unknown IDs as a placeholder
  glyph; no exception is raised. Verify IDs match a real emoji in the
  source guild or application.
- **Bot not in source guild.** A guild-owned custom emoji string only
  renders when the bot is currently a member of the guild that owns
  the emoji. Application emojis avoid this entirely.
- **Animated flag.** Static custom emoji use `<:name:id>` with no
  leading `a`. Animated use `<a:name:id>`. Mismatching the flag
  against the actual asset shows a static frame.

---

## V2 Builder Functions

Convenience functions for building V2 component trees. All return standard
discord.py components.

### `card(*children, color=None, spoiler=False)`

Creates a `Container`. Strings are auto-wrapped in `TextDisplay`. Pass
`spoiler=True` to hide the whole container behind a spoiler overlay:

```python
from cascadeui import card, divider
from discord.ui import TextDisplay

self.add_item(card(
    "## My Card",
    TextDisplay("Card content."),
    divider(),
    TextDisplay("-# Footer"),
    color=discord.Color.blurple(),
))
```

### `key_value(data)`

Converts a dict to a formatted `TextDisplay`:

```python
from cascadeui import key_value

self.add_item(key_value({"Status": "Online", "Users": "42"}))
# Renders: **Status:** Online\n**Users:** 42
```

### `action_section(text, *, label, callback, ...)`

A `Section` with text and a button accessory. Pass `disabled=True` to render
the button greyed out and non-interactive:

```python
from cascadeui import action_section

self.add_item(action_section(
    "Click to refresh the dashboard",
    label="Refresh", callback=self.refresh_data, emoji="🔄",
))
```

### `toggle_section(text, *, active, callback, labels=("Enabled", "Disabled"), emoji=None)`

A `Section` with a green/red toggle button. `labels` sets the (active, inactive)
button text -- pass `("On", "Off")` to relabel -- and `emoji` adds a button emoji.
Pass `disabled=True` to render the button greyed out and non-interactive:

```python
from cascadeui import toggle_section

self.add_item(toggle_section(
    "**Dark Mode**\nEnable dark theme",
    active=self.dark_mode, callback=self.toggle_dark,
))
```

### `image_section(text, *, url, description=None, spoiler=False)`

A `Section` with a `Thumbnail` image. `url` accepts a remote URL string,
the `attachment://name.ext` form, or a `discord.File` instance whose
`.uri` is read internally. `description` sets the thumbnail's alt text (up to
256 chars); `spoiler=True` hides it behind a spoiler. See
[Local file attachments](#local-file-attachments).

### `link_section(text, *, label, url, emoji=None)`

A `Section` with a link-style button accessory. Completes the `*_section`
family for the three Section accessory shapes: action (StatefulButton), image
(Thumbnail), and link. Link buttons open a URL directly -- no callback runs
and no interaction fires.

```python
from cascadeui import link_section

self.add_item(link_section(
    "Full documentation is on GitHub Pages.",
    label="Open Docs",
    url="https://hollowthesilver.github.io/CascadeUI/",
))
```

### `confirm_section(text, *, on_confirm, on_cancel, ...)`

Returns a `[TextDisplay, ActionRow]` list rather than a single component so
the caller can splat it into `card(...)`. The paired success/danger buttons
run the supplied callbacks:

```python
from cascadeui import card, confirm_section

self.add_item(card(
    "## Delete Server Data",
    *confirm_section(
        "This cannot be undone.",
        on_confirm=self._do_delete,
        on_cancel=self._do_cancel,
        confirm_label="Delete",
    ),
    color=discord.Color.red(),
))
```

Defaults: confirm button is green with a check emoji, cancel is red with a
cross emoji. Override any of `confirm_label`, `cancel_label`,
`confirm_emoji`, `cancel_emoji` to customize.

### `alert(message, *, level="info")`

A colored status container:

| Level | Color |
|-------|-------|
| `"success"` | Green |
| `"warning"` | Gold |
| `"error"` | Red |
| `"info"` | Blue |

### `stats_card(title, stats, *, color=None, footer=None)`

Thin composition of `card(title, key_value(stats), ...)`. The title is
rendered as a second-level heading automatically (pre-format with `##` for
finer control), a small separator sits between the heading and the stats,
and an optional `footer` line renders in Discord's subtext style:

```python
from cascadeui import stats_card

self.add_item(stats_card(
    "Server Overview",
    {"Members": 42, "Channels": 12, "Roles": 5},
    color=discord.Color.green(),
    footer="Updated just now",
))
```

When `color` is omitted, the active theme's `accent_colour` is used
automatically inside a view's `build_ui()`.

### `progress_bar(value, max_value, *, width=20, ...)`

Text-based progress bar returned as a `TextDisplay`. V2 equivalent of the V1
`ProgressBar` composite. Renders `[████████████░░░░░░░░] 60%` by default with
Unicode block glyphs. `value` is clamped to `[0, max_value]` so callers do
not need to guard against overshoots.

```python
from cascadeui import progress_bar

self.add_item(progress_bar(7, 10, width=10))  # [███████░░░] 70%
```

Override `filled` / `empty` for alternative glyphs, or set
`show_percent=False` to drop the trailing percentage.

### `divider()` and `gap(large=False)`

`divider()` creates a thin line separator. `gap()` creates spacing without a
visible line.

### `gallery(*media, descriptions=None)`

A `MediaGallery` from one or more image references. Each reference is
either a URL string, the `attachment://name.ext` form, or a `discord.File`
instance whose `.uri` is read internally. See
[Local file attachments](#local-file-attachments) for the send-time pairing.

### `file_attachment(url, *, spoiler=False)`

A `File` component for inline attachment display. Accepts either a remote
URL, the `attachment://name.ext` form, or a `discord.File` instance:

```python
from cascadeui import card, file_attachment

card(
    "## Quarterly Report",
    file_attachment("attachment://q1_2026.pdf"),
    "Released April 15.",
)
```

Use `file_attachment` for downloadable files; use `gallery` for inline image previews.

### `button_row(buttons, *, style=..., emoji=None)`

Builds an `ActionRow` from a `{label: callback}` mapping. Dict insertion
order determines button order, so every button in the row shares one style
and emoji:

```python
from cascadeui import button_row

self.add_item(button_row(
    {
        "Save": self._save,
        "Reset": self._reset,
        "Cancel": self._cancel,
    },
    style=discord.ButtonStyle.primary,
))
```

Raises `ValueError` if the mapping is empty or exceeds Discord's
5-buttons-per-row limit. For per-button customization, build the `ActionRow`
by hand.

### `choice_row(options, *, on_select, selected=None, multi=False, ...)`

A "pick one" (or "pick any") control. Where `button_row` gives N buttons
each with its own callback and no notion of a current selection,
`choice_row` gives N options that share one `on_select` and tracks which is
active: the active option renders highlighted, and in single-select it is
also disabled so re-picking it is a no-op. Pass a `{label: value}` dict for
the common case, or a list of `Choice` when an option needs an emoji or a
dropdown description:

```python
from cascadeui import choice_row

# inside your StatefulLayoutView.build_ui():
self.add_item(choice_row(
    {"Easy": Difficulty.EASY, "Normal": Difficulty.NORMAL, "Hard": Difficulty.HARD},
    selected=self.difficulty,
    on_select=self._set_difficulty,   # async (interaction, value) -> None
))
```

When the option count outgrows a button row (more than `button_threshold`,
default 5), `choice_row` renders a dropdown instead -- up to Discord's
25-option select limit (exported as `MAX_SELECT_OPTIONS`), past which it
raises. Discord requires select option values to be strings; the builder maps
to and from that form, so `on_select` always receives the real Python value,
never a stringified index.

Set `multi=True` to let several options be active at once. `selected`
becomes a set, the buttons become toggles (no disabled state, since an
active option must be clickable to turn it off), and `on_select` receives
the full list of selected values. Two `choice_row` controls in one view need
distinct `custom_id=` values so their components do not collide.

!!! warning "The host owns the selection -- rebuild after `on_select`"
    `choice_row` is stateless: it reads `selected` at build time and renders
    the active option(s) from it. Your `on_select` callback must store the new
    value and rebuild the row -- `build_ui()` then `refresh()`, or a state
    dispatch that triggers `on_state_changed`. Without a rebuild, the control
    snaps back to the build-time selection on the next click.

!!! tip
    Reach for a raw `StatefulSelect` when you need custom `min_values` /
    `max_values`, or a select type other than text (role, user, channel).
    `choice_row` covers the common "pick from a fixed set of values" shape.

### `cycle_button(*, values, on_change, ...)`

A button that cycles through a fixed list of values. The button tracks its
own index on the returned instance (`button._cycle_index`); clicking
advances to the next value (wrapping) and updates the label before the
`on_change` callback runs:

```python
from cascadeui import cycle_button

async def _preset_changed(interaction, value):
    self.preset = value
    self.build_ui()
    await self.refresh()

self.add_item(ActionRow(cycle_button(
    values=["Low", "Medium", "High"],
    on_change=self._preset_changed,
    emoji="⚙️",
)))
```

The callback receives the *new* value (post-advance). Optional `labels=`
customizes the display strings, `start=` picks the initial index.

### `toggle_button(*, active, on_toggle, ...)`

Standalone boolean toggle button. Distinct from `toggle_section`, which
wraps the same button shape in a Section with display text on the left.
Use `toggle_button` when the button stands alone in an `ActionRow`:

```python
from cascadeui import toggle_button

async def _dark_mode(interaction, active):
    self.dark = active
    self.build_ui()
    await self.refresh()

self.add_item(ActionRow(toggle_button(
    active=self.dark,
    on_toggle=_dark_mode,
    labels=("Dark", "Light"),
)))
```

The button flips its own state (`button._toggle_active`) and calls
`on_toggle(interaction, new_state)` with the post-flip value. Style and
label swap automatically between the active/inactive pair.

### `tab_nav(tabs, *, active=None, ...)`

Lighter alternative to `TabLayoutView` for views that want tab-style
navigation without the full Tab pattern's lifecycle (async builders,
`on_tab_switched`, refresh contract). Each tab is just a button the view
handles in its own callback:

```python
from cascadeui import tab_nav

self.add_item(tab_nav(
    {
        "Stats": self._show_stats,
        "Settings": self._show_settings,
        "Help": self._show_help,
    },
    active="Stats",
))
```

The tab matching `active` renders with `active_style` (primary by default);
all others render with `inactive_style` (secondary). If `active` is
omitted, the first tab is marked active. Capped at Discord's 5-per-row
limit -- use `TabLayoutView` for views that need more tabs.

---

## Local file attachments

V2 builders that take a media reference (`gallery`, `image_section`,
`file_attachment`) accept three input shapes:

- A remote URL (`"https://cdn.example.com/img.png"`)
- The `attachment://<filename>` reference scheme, when the file travels
  with the message as a `discord.File`
- A `discord.File` instance directly -- the builder reads its `.uri`
  property and emits the same `attachment://<filename>` reference

The reference and the bytes are independent: the builder emits the
reference into the component tree; the `discord.File` carries the bytes
to Discord.

!!! warning "Both halves must travel together"

    An `attachment://<filename>` reference inside a builder is meaningless
    on its own. The matching `discord.File` must reach the same payload
    via `view.send(files=[...])` (initial send) or
    `view.refresh(attachments=[...])` (in-place edit), or Discord renders
    the reference as an unresolved placeholder.

### Initial send

`StatefulView.send()` and `StatefulLayoutView.send()` accept `file=`
(singular) and `files=` (sequence) for the initial upload:

```python
import discord
from cascadeui import StatefulLayoutView, card, gallery

class GalleryView(StatefulLayoutView):
    def __init__(self, *, photo_uri: str, **kwargs):
        super().__init__(**kwargs)
        self.add_item(card("## Gallery", gallery(photo_uri)))

photo = discord.File("assets/photo.png")
view = GalleryView(context=ctx, photo_uri=photo.uri)
await view.send(files=[photo])
```

Mixing `file=` and `files=` raises `TypeError` from discord.py at the send
boundary. If the send fails before the bytes reach Discord, the library
closes the supplied file handles as part of its rollback so callers do
not leak file pointers.

### Fetching remote assets

For attachments sourced from a URL (avatars, CDN-hosted images, attachment
proxy URLs), `cascadeui.fetch_as_file()` absorbs the standard
`aiohttp` GET + `BytesIO` + `discord.File` construction:

```python
from cascadeui import fetch_as_file

photo = await fetch_as_file(
    ctx.author.display_avatar.url,
    "avatar.png",
)
await view.send(files=[photo])
```

Pass a shared `session=` for any command that fetches multiple URLs so
the requests share one TCP pool:

```python
import aiohttp

async with aiohttp.ClientSession() as session:
    photo_a = await fetch_as_file(url_a, "a.png", session=session)
    photo_b = await fetch_as_file(url_b, "b.png", session=session)
await view.send(files=[photo_a, photo_b])
```

`fetch_as_file` forwards `spoiler=` and `description=` to the
`discord.File` constructor, so attachment metadata travels through the
helper without an extra wrapper. See `examples/v2_attachments.py` for a
runnable four-command walkthrough.

### Mid-session attachment swaps

`view.refresh()` forwards arbitrary kwargs to the underlying
`message.edit()` call, so a swap goes through the standard `attachments=`
parameter -- a replacement list, not additive. Pair it with `build_ui()`
so the component tree rebuilds against the new reference at the same
time the new bytes ship:

```python
class GalleryView(StatefulLayoutView):
    def __init__(self, *, photo_uri: str, **kwargs):
        super().__init__(**kwargs)
        self._photo_uri = photo_uri
        self.build_ui()

    def build_ui(self):
        self.clear_items()
        self.add_item(card("## Gallery", gallery(self._photo_uri)))

    async def swap_photo(self, new_photo: discord.File):
        self._photo_uri = new_photo.uri
        self.build_ui()
        await self.refresh(attachments=[new_photo])
```

`build_ui()` clears the tree and re-adds the rebuilt card; `refresh()`
ships the new component bytes plus the replacement attachments in a
single edit. `attachments=` replaces the message's complete attachment
list -- previously-attached files not present in the new list are
removed. Use an empty list (`attachments=[]`) to clear all attachments
without uploading new ones.

### Persistent views

`PersistentLayoutView` and `PersistentView` survive bot restarts.
`attachment://` references already present in the persisted component
tree resolve from Discord's stored copy of the original upload -- the
bytes survive as long as the message itself does, so no re-upload is
required at restart.

References introduced *after* restart (inside `on_restore` or any later
rebuild) need a matching `discord.File` passed through
`refresh(attachments=[...])`; otherwise Discord renders the reference as
unresolved.

---

## Composing V2 layouts

A V2 view is a tree, and most layout questions reduce to one thing: what
nests inside what. The allowed shape is small enough to hold in your head,
and only two nestings are ever illegal.

### The nesting tree

```text
LayoutView (the view)
  - Container  (a card)         the main grouping block
      - TextDisplay             markdown text
      - Section                 text + one accessory (button or thumbnail)
      - ActionRow               buttons OR one select
      - MediaGallery            1-10 images
      - File                    a downloadable file
      - Separator               a divider or gap
  - ActionRow                   (also valid at the top level)
  - Section                     (also valid at the top level)
  - MediaGallery / File / TextDisplay / Separator
```

The two surprising illegal nestings are: a `Container` cannot hold another
`Container`, and a `Section` cannot hold another `Section`. The rest follow
type logic -- a bare `Button` or `Select` needs an `ActionRow`, and a
`Thumbnail` belongs only as a `Section` accessory.

### What goes where

- **Top level (the view):** Containers (cards), ActionRows, Sections,
  MediaGalleries, Files, TextDisplays, Separators. A bare `Button`,
  `Select`, or `Thumbnail` is *not* valid at the top level -- it must live
  inside an `ActionRow` (buttons/selects) or a `Section` (thumbnail).
- **Inside a card (`Container`):** everything the top level accepts except
  another card. Text, sections, action rows (which may wrap a select),
  galleries, and dividers all nest inside a card.
- **Inside an `ActionRow`:** up to 5 buttons, *or* exactly one select --
  not both.
- **Inside a `Section`:** one to three `TextDisplay` children plus one
  accessory (a button or a thumbnail).

### Selects and choice_rows belong inside cards

A card accepts ActionRows, and a `choice_row` *is* an ActionRow (buttons, or
a single select), so a dropdown belongs *inside* the card it relates to, not
stranded as a separate row below it. A raw `StatefulSelect` drops in the same
way once wrapped in an `ActionRow` -- `choice_row` does that wrapping for you:

```python
self.add_item(card(
    "## Filters",
    key_value(self._summary()),
    choice_row(LEAGUES, selected=self.league, on_select=self._pick),  # inside the card
))
```

### Reveal a region inside a card

Because `Collapsible.render()` and `PaginatedRegion.controls()` return raw
components rather than a pre-wrapped card, you choose where they land. Splat
them into a `card()` and the revealed region expands *inside* the card:

```python
def build_ui(self):
    self.clear_items()
    self.add_item(card(
        "## Filters",
        key_value(self._summary()),
        *self.league_picker.render(self),   # trigger + reveal, inside the card
    ))
```

The single caveat is the Container-in-Container rule: a `reveal` callable
must not return a `card` when you splat it into a card. choice_rows,
sections, and text are all fine -- each is either an ActionRow or a
Container-legal child.

The rules below spell out every constraint and how CascadeUI's pre-flight
validator enforces them.

## V2 Placement Rules

Discord's V2 component system has strict rules about which component types
can nest inside which. discord.py only enforces a small subset of those rules
at construction time -- the rest are enforced by Discord's API server when
`send()` runs, returning HTTP 400 with terse error text far from the
construction site. CascadeUI's V2 builders sidestep this entire problem
because each builder hardcodes a known-safe shape, and an opt-out send-time
validator catches violations in any tree built outside the builders.

### What discord.py enforces at construction

- V1 `View` rejects V2 items (`TextDisplay`, `Container`, `Section`, etc).
- `Section` allows at most 3 children.
- `Section` requires an `accessory=` kwarg.
- `ActionRow` width budget = 5 (Button = 1 unit, Select = 5 units).
- `add_item` rejects non-Item, non-str types.

Everything else passes construction silently. Discord rejects the message
when you call `send()`.

### What Discord enforces at send time

| Composition | discord.py | Discord API |
|---|---|---|
| `Section` accessory must be `Button` or `Thumbnail` | accepts | rejects |
| `Section` children must all be `TextDisplay` | accepts | rejects |
| `Container` children must be `ActionRow`, `TextDisplay`, `Section`, `MediaGallery`, `File`, or `Separator` | accepts | rejects |
| Containers cannot nest | accepts | rejects |
| Sections cannot nest | accepts | rejects |
| Standalone `Button` / `Select` / `Thumbnail` at LayoutView top level | accepts | rejects |
| `ActionRow` children must be `Button` or `Select` | accepts | rejects |
| `Thumbnail` outside a `Section` accessory slot | accepts | rejects |
| `Label` / `RadioGroup` / `CheckboxGroup` / `Checkbox` / `FileUpload` at any LayoutView position | accepts | rejects (Modal-only) |
| `Container` must hold at least 1 child (empty) | accepts | rejects |
| `Section` must hold 1-3 children (empty) | accepts (empty) | rejects |
| `ActionRow` must hold at least 1 child (empty) | accepts | rejects |
| `MediaGallery` must hold 1-10 items | accepts | rejects |
| Two components share a `custom_id` | accepts | rejects (code 50035) |

### Builders are guardrails

Routing through CascadeUI's V2 builders sidesteps every rule in the table
above. Each builder hardcodes a Discord-API-valid shape:

- `card()` always produces a top-level `Container` with valid children.
- `image_section()` always produces `Section(TextDisplay, accessory=Thumbnail)`.
- `action_section()` / `toggle_section()` / `link_section()` always produce
  `Section(TextDisplay, accessory=Button-variant)`.
- `button_row()` always produces `ActionRow(Button, Button, ...)` capped at 5.
- `choice_row()` always produces one `ActionRow` (buttons or a `StatefulSelect`),
  capped at 5 buttons / 25 options.
- `gallery()` / `file_attachment()` always produce a properly-wrapped media component.

A user who composes views entirely from builders cannot construct a tree
Discord rejects (size limits aside). The escape hatch is dropping to raw
`discord.ui` primitives -- where the validator below picks up the slack.

### Pre-flight validation

Every V2 view (`StatefulLayoutView` and subclasses) gets a placement
validator that runs before every Discord round-trip. Three seams call
it: the initial send (`_send_pipeline`), every state-driven `refresh()`
after the render-hash short-circuit, and the in-place edits emitted by
`push()` / `pop()` navigation. Mid-session shape changes (Wizard step
swaps, Form section toggles, Tab body rebuilds) get caught at the
seam instead of surfacing as terse HTTP 400 from `message.edit()`.

When the assembled component tree contains a composition Discord would
400 on, the validator raises a clear `ValueError` naming the violation
node, the path through the tree, and the suggested fix:

```
ValueError: Invalid V2 placement: Container cannot be a child of Container.
  Path: MyDashboard -> Container[0] -> Container[0]
  Discord rejects this composition with HTTP 400.
  Fix: Containers cannot nest. Move the inner Container's children up to
       the outer Container, or split into a separate top-level Container.
```

The path uses bracket indices on each segment so the offending node is
unambiguous when the same type appears multiple times at the same depth.

Skipped refreshes (the render-hash digest matches the previous send)
bypass validation entirely -- nothing changed, the previous send
already validated the tree. The cost on every other refresh is one
linear walk over the component tree, microseconds for typical views.

### Duplicate custom_ids

Discord also rejects a message whose component tree contains two
components with the same `custom_id` (HTTP 400, code 50035). This is a
uniqueness rule, not a placement one, so it runs for **every** view (V1
`StatefulView` as well as V2) and is not governed by `validate_placement`.
The same three seams walk the tree and raise a directed `ValueError`
naming the repeated id:

```
ValueError: Duplicate component custom_id: 'choice' appears more than once in ConfigView.
  Discord rejects this message with HTTP 400 (code 50035: component custom id cannot be duplicated).
  Fix: Give each component a distinct custom_id. A builder used more than once
       (choice_row, PaginatedRegion, Collapsible) needs a distinct custom_id=/key= per call.
```

Auto-generated ids never collide here: CascadeUI position-anchors them
before send. Only caller-supplied ids repeat, most often a builder called
twice with its default (`choice_row`, `PaginatedRegion`, `Collapsible`).
`Modal` applies the same rule to its inputs at construction: two inputs
whose labels derive the same `custom_id` raise immediately rather than
silently overwriting each other at submit.

### Opting out

Set `validate_placement = False` on the view class for a documented escape
hatch:

```python
class MyDashboard(StatefulLayoutView):
    validate_placement = False  # tree uses a composition Discord accepts
                                # that the built-in validator rejects
```

The recommended use case is narrow: the validator's matrix lags a Discord
or discord.py update. Any other reason to opt out signals an actual
placement bug. Prefer fixing the tree. `validate_placement = False`
disables only the placement matrix; the duplicate-`custom_id` check is a
separate, always-on guard and still runs.

---

## Grid Helpers

Two helpers for building grid-based UIs:

### `emoji_grid(rows, cols, *, fill="⬛", row_labels=None, col_labels=None, corner=None, cell_sep=" ")`

Returns an `EmojiGrid` -- a live `TextDisplay` subclass that rewrites its
content on every mutation:

```python
from cascadeui import emoji_grid

grid = emoji_grid(10, 10, fill="🟦", row_labels="alpha", col_labels="numeric")
grid[(2, 3)] = "🔥"       # Set single cell
grid.fill_rect((0, 0), (2, 2), "⬜")  # Fill rectangle
grid.clear()               # Reset all cells to fill
```

#### Retained Mode vs Immediate Mode

`EmojiGrid` supports two usage patterns:

**Retained mode** -- mutate cells in place; content auto-rewrites. The grid
object holds its own state and each mutation immediately updates the rendered
string. Ideal for persistent grids where the board evolves incrementally:

```python
# Battleship pattern: mutate cells, grid auto-renders
grid[(row, col)] = hit_emoji
self.build_ui()  # grid.content is already updated
```

**Immediate mode** -- rebuild from external state each render. The grid is
reconstructed from scratch on every `build_ui()` call, using external state
as the source of truth:

```python
# Dashboard pattern: rebuild from state
grid = emoji_grid(5, 5, fill="⬛")
for pos, value in self.state_data.items():
    grid[pos] = value
```

Both are valid patterns. Use retained mode when the grid IS the state; use
immediate mode when external state drives the rendering.

#### Axis Labels

| `row_labels` | `col_labels` | Result |
|---|---|---|
| `None` | `None` | No labels |
| `"alpha"` | `None` | A-Z row labels only |
| `None` | `"numeric"` | 0-9 column header only |
| `"alpha"` | `"numeric"` | Both, with corner character |

Presets: `"alpha"` (regional indicators, max 26) and `"numeric"` (keycap
emoji, max 10). Custom `Sequence[str]` also accepted.

#### Mutation API

| Operation | Example |
|-----------|---------|
| Single cell | `grid[(r, c)] = "🔥"` |
| Multiple cells | `grid[[(0,0), (1,1)]] = "⭐"` |
| Rectangle fill | `grid.fill_rect((0,0), (2,2), "⬜")` |
| Row by index | `grid[0] = "🟥"` |
| Clear all | `grid.clear()` |

### `button_grid(rows, cols, cell_factory)`

Packs buttons into `ActionRow` components:

```python
from cascadeui import button_grid

rows = button_grid(3, 3, lambda r, c: StatefulButton(
    label=self.board[r][c],
    callback=self._make_move(r, c),
))
for row in rows:
    self.add_item(row)
```

Discord caps at 5 rows × 5 buttons. Both dimensions must be 1-5.

---

## Behavioral Wrappers

Modify component behavior without changing the component:

!!! danger "Wrappers consume the interaction response"
    All three wrappers attempt to use `interaction.response` internally (with
    an `is_done()` fallback for auto-defer compatibility). Wrapped callbacks
    should use `self.respond(interaction, ...)` for any replies -- it handles
    the response/followup routing automatically.

### `with_loading_state(button)`

Disables the button and changes its label to "Loading..." while the callback
runs.

### `with_confirmation(button, *, message, confirmed_message, cancelled_message)`

Shows a confirmation prompt before executing the callback.

### `with_cooldown(button, *, seconds, scope="user")`

Per-user (default), per-guild, or global cooldown.

---

## V2 Composite Components

A composite component holds state across interactions and lives *inside* a view, sitting between the two simpler shapes: a builder returns a one-shot tree and forgets it, a view owns the whole message.

### `PaginatedRegion`

Pages one slice of a `StatefulLayoutView`'s tree while the host renders everything else. Reach for `PaginatedLayoutView` when the page list *is* the message; reach for `PaginatedRegion` when one section of a multi-section view needs its own page index while the surrounding layout -- a header, other cards, a second list -- stays put.

!!! note "Why `PaginatedLayoutView` takes a `formatter` but `PaginatedRegion` does not"
    A `PaginatedLayoutView` owns the whole message, so it needs a `formatter` to render each page -- there is no other render hook to call. A `PaginatedRegion` lives inside a host view whose `build_ui()` or `on_load()` already renders everything, so it only slices the item list and the host renders the slice alongside the rest of the layout.

```python
from cascadeui import StatefulLayoutView, PaginatedRegion, action_section, card

class TaskListView(StatefulLayoutView):
    def __init__(self, tasks, **kwargs):
        super().__init__(**kwargs)
        self.tasks = tasks
        self.pager = PaginatedRegion(per_page=6)

    def build_ui(self):
        self.clear_items()
        self.pager.items = self.tasks
        rows = [action_section(t.title, label="Open", callback=self._open(t))
                for t in self.pager.page_items]
        self.add_item(card("## Tasks", *rows, *self.pager.controls(self)))
```

`controls(self)` captures the host and returns the nav row (empty on a single page). A page click re-runs the host's render path and re-slices. Each region keeps its own page index, so two regions can share a view if you give them distinct `key` values. Customization mirrors `PaginatedLayoutView` -- subclass and override the `{first,prev,indicator,next,last}_button_{label,emoji,style}` class attributes, `jump_threshold`, or the `on_page_changed` hook. See the API reference for the full surface.

### `Collapsible`

A trigger button that toggles an inline region of revealed content -- the disclosure/expander pattern. Use it when a button should reveal more controls (a `choice_row`, a select, a form) in place and hide them again, instead of pushing a new view or sending a follow-up. Pass a `reveal` callable that returns the content; `Collapsible` owns the toggle state and drives the host's render path on each click.

```python
from cascadeui import Collapsible, choice_row

class FilterView(StatefulLayoutView):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.league_picker = Collapsible(
            label="Edit Leagues",
            expanded_label="Done",
            reveal=lambda: choice_row(
                LEAGUES, selected=self.league, on_select=self._pick_league
            ),
        )

    def build_ui(self):
        self.clear_items()
        self.add_item(card("## Filters", key_value(self._summary())))
        for item in self.league_picker.render(self):
            self.add_item(item)

    async def _pick_league(self, interaction, value):
        self.league = value
        self.league_picker.collapse()   # collapse after the pick
        self.build_ui()
        await self.refresh()
```

`render(self)` returns the trigger alone while collapsed, or the trigger plus the `reveal()` content while expanded (order via `trigger_first`). The collapse policy is the caller's -- `collapse()` after a revealed action finishes, or leave it open for multi-step use. The trigger relabels and restyles between states via `expanded_label` / `expanded_style` / `expanded_emoji`, and two collapsibles in one view need distinct `key` values.

`expand()` / `collapse()` set the state programmatically and `expanded` reads it; the `on_toggle(expanded)` hook fires after every open or close for async prefetch or logging.

### In-card disclosure with `summary`

By default the trigger is a button on its own row. When the disclosure belongs *inside* a card -- the Edit button sitting beside a line of summary text rather than in a row of its own -- pass a `summary` callable (zero-argument, synchronous, read on every render like `reveal`). The trigger then renders as an `action_section`: a Section carrying the summary text with the trigger button as its accessory, so the whole disclosure splats into one `card(...)`.

```python
self.representation = Collapsible(
    label="Edit",
    expanded_label="Done",
    emoji="✏️",
    summary=lambda: f"Flagged beside your name: **{self.represented_name}**.",
    reveal=lambda: ActionRow(self._represented_select()),
    key="representation",
)

def build_ui(self):
    self.clear_items()
    self.add_item(card("### 🏳️ Representation", *self.representation.render(self)))
```

When `summary` returns an empty value (data not loaded yet), the trigger falls back to the bare button rather than rendering an empty Section.

---

## V1 Composite Components

!!! note "V1 only"
    These use row-based layout and work with `StatefulView` only.

`ConfirmationButtons`, `PaginationControls`, `ToggleGroup`, `ProgressBar`  -- 
pre-built V1 component groups that attach to a view via `.add_to_view()`.
See `cascadeui.components.patterns.v1` for the full API.

---

## Utilities

### `slugify(text)`

Converts display strings to safe `custom_id` fragments:

```python
from cascadeui import slugify

slugify("Color Roles")  # "color_roles"
slugify("He/Him")       # "he_him"
slugify("Tickets #1")   # "tickets_1"
```
