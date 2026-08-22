# // ========================================( Modules )======================================== // #


import asyncio
import inspect
import logging
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Optional, Union

import discord
from discord.ui import Item

from ..state.actions import ActionCreators
from ..state.store import _CURRENT_INTERACTION
from ..utils.coercion import coerce_snowflake_match
from ..utils.hooks import accepts_second_positional, await_maybe, can_accept_positional
from ..utils.responses import ack_backstop, open_modal_safe, respond_safe, trailing_ack
from .types import MAX_SELECT_OPTIONS

logger = logging.getLogger(__name__)


# // ========================================( Functions )======================================== // #


def _describe_callback(fn) -> str:
    """Name a user callback and its signature for an error message.

    A refusal that prints only "wrong number of arguments" leaves the
    reader counting parameters; printing the signature back makes the
    mistake self-diagnosing, and an unbound method pulled off a class body
    shows its ``self`` immediately.
    """
    name = getattr(fn, "__name__", None) or repr(fn)
    try:
        return f"{name}{inspect.signature(fn, follow_wrapped=False)}"
    except (ValueError, TypeError):
        return name


def require_url(url, owner: str, label: str = "") -> None:
    """Refuse a link destination that cannot resolve.

    A blank destination is not a degraded link: Discord answers it with a
    form error naming no component. The pre-flight validator catches one
    in a V2 tree, but a V1 view reaches no pre-flight, so the check
    belongs where the button is built.

    Takes the owner's name because two constructors reach it: refusing in
    ``LinkButton``'s vocabulary from a ``link_section`` call would point
    at a class the caller never wrote.
    """
    named = f"(label={label!r}) " if label else ""
    # Checked before the emptiness test, because a truthy non-str answers
    # ``strip`` by raising from inside this guard rather than reporting
    # the input. A yarl.URL is the realistic one: aiohttp is a hard
    # dependency, so every consumer holds the type, and discord.py stores
    # whatever it is given and fails on it at serialization.
    if not isinstance(url, str):
        raise TypeError(
            f"{owner}{named}needs a str url, got {type(url).__name__}.\n"
            f"  Fix: pass the address as a string -- str(url) if you are "
            f"holding a URL object."
        )
    if url.strip():
        return
    raise ValueError(
        f"{owner}{named}needs a non-empty url.\n"
        f"  Fix: supply the destination, or use an ordinary button with a "
        f"callback if there is nowhere to link."
    )


def refuse_wrong_arity(fn, arity: int, message: str) -> None:
    """Refuse a callback that cannot take the argument list it will be given.

    Takes the arity the caller has ALREADY decided to use, rather than
    deciding again. A seam that chooses the call shape from one reading of
    the signature and refuses from another can disagree with itself: a
    ``functools.wraps`` adapter advertises the signature it wraps, so a
    narrowing one declares an argument it cannot take and a widening one
    takes an argument it does not declare. Passing the chosen arity in
    makes that class of disagreement unrepresentable.

    An unreadable signature reports ``None`` and is allowed through,
    matching every other introspection seam here.
    """
    if can_accept_positional(fn, arity) is False:
        raise TypeError(message)


def require_value_callback(fn, owner: str, param: str, value_name: str) -> None:
    """Refuse a callback that cannot receive the value its control delivers.

    The mirror of the button-side refusal. A control that reports what was
    picked calls its callback with two arguments always, so a callback
    declaring one is broken on the first click, with a bare arity
    ``TypeError`` raised from inside the builder's own closure. Accepting
    it instead and dropping the value would be worse: the callback cannot
    know what was chosen, which is silent wrong behavior rather than a
    loud failure.

    An unreadable signature is allowed through, matching every other
    introspection seam here.
    """
    if fn is None:
        return
    if can_accept_positional(fn, 2) is False:
        # The parameter is sometimes itself named "callback", so the word is
        # added only when it does not already read as one.
        label = param if "callback" in param else f"{param} callback"
        raise TypeError(
            f"{owner}: {label} {_describe_callback(fn)} cannot be called "
            f"with (interaction, {value_name}).\n"
            f"  Fix: accept (interaction, {value_name}) -- this control reports "
            f"{value_name} to its callback on every click."
        )


def _callback_arity_message(component, fn, is_select: bool) -> str:
    """Build the refusal text for a callback the component cannot call."""
    owner = type(component).__name__
    if is_select:
        return (
            f"{owner} callback {_describe_callback(fn)} cannot be called with "
            f"(interaction) or (interaction, values).\n"
            f"  Fix: accept (interaction), or (interaction, values) to receive "
            f"the selection."
        )
    return (
        f"{owner} callback {_describe_callback(fn)} cannot be called with "
        f"(interaction). This component passes no second value to its callback.\n"
        f"  Fix: accept a single positional argument and close over any extra "
        f"data, or use toggle_button / cycle_button / choice_row when the "
        f"callback needs the control's value."
    )


# // ========================================( Classes )======================================== // #


class StatefulComponent:
    """Base mixin for components that interact with state."""

    def create_stateful_callback(self, component, original_callback=None):
        """Create a callback that updates state.

        The wrapper enforces the callback contract: a callback receives
        the interaction, plus the component's ``values`` as a second
        argument when the component is a select and the callback declares
        a second positional parameter.

        Raises:
            TypeError: ``original_callback`` cannot accept the arguments
                the component will call it with -- ``(interaction)`` for a
                button, ``(interaction)`` or ``(interaction, values)`` for
                a select. An unreadable signature is allowed through.
        """
        component_id = getattr(component, "custom_id", None) or str(id(component))

        # Keep the caller's own function reachable. Once this returns,
        # ``component.callback`` is the wrapper below, and every stateful
        # component in a view shares its identity -- so anything that wants
        # to tell two components apart by what they DO has to read through
        # the wrapper to the function it closes over.
        component._cascadeui_user_callback = original_callback

        # Pre-compute whether to pass select values to the callback.
        # When the component is a select and the callback accepts a second
        # positional parameter, component.values is passed automatically.
        # The real property is "is this a select", which only a select's
        # ``values`` attribute answers: discord.py injects ``custom_id`` onto
        # every attached item, so that one cannot discriminate.
        _pass_values = False
        if original_callback:
            is_select = hasattr(component, "values")
            # Decide the call shape first, then refuse against that shape.
            # A signature the component can never call is a mistake made
            # here, at the builder line, and discovered on a click minutes
            # later as a bare arity TypeError raised from inside the wrapper
            # below. Refusing it names the component and the shape it wants.
            _pass_values = is_select and accepts_second_positional(original_callback)
            refuse_wrong_arity(
                original_callback,
                2 if _pass_values else 1,
                _callback_arity_message(component, original_callback, is_select),
            )

        async def stateful_callback(interaction):
            # Get view from the component itself
            view = component.view

            if not view:
                logger.error(f"Could not find view for component {component_id}")

                # Call original callback if provided
                if original_callback:
                    return await await_maybe(original_callback(interaction))
                return

            # Per-component owner-only gate. Routes through the view's
            # ``on_unauthorized`` hook + ``unauthorized_message`` so the
            # rejection UX matches the view-level gate. Skipped when the
            # view has no owner (``user_id is None``) so anonymous flows
            # still work. Strictly checks ``user_id`` -- ``allowed_users``
            # is intentionally NOT consulted because the typical use case
            # is a host-only button on an open-join view (lobby, ticket,
            # poll), where allowed_users would let participants through.
            #
            # ``is True`` rather than truthiness: the kwarg stores exactly
            # ``True`` or ``False``, so anything else reaching this
            # attribute is not a caller opting in.
            if (
                getattr(component, "_button_owner_only", False) is True
                and getattr(view, "user_id", None) is not None
                and interaction.user.id != view.user_id
            ):
                await await_maybe(view.on_unauthorized(interaction))
                return

            # Bind the live interaction for the acting-view fast path in
            # ``_StatefulMixin.refresh()``. Scope-narrow: set for the original
            # callback + dispatch sequence, reset in the ``finally`` so
            # subsequent interactions on the same event loop task never see
            # a stale value.
            token = _CURRENT_INTERACTION.set(interaction)
            try:
                # Call original callback FIRST so it can respond to the interaction
                # before state dispatch triggers on_state_changed notifications
                if original_callback:
                    if _pass_values and component.values is not None:
                        await await_maybe(original_callback(interaction, component.values))
                    else:
                        await await_maybe(original_callback(interaction))

                # Skip dispatch if the callback destroyed the view (exit, push, etc.)
                if view.is_finished():
                    return

                # Read the component's value after the callback so a component
                # that mutates itself in its own callback (ToggleButton flipping
                # ``is_toggled``) records the state it ended on, not the one it
                # was clicked in.
                value = None
                if hasattr(component, "value"):
                    value = component.value
                elif hasattr(component, "values"):
                    value = component.values
                elif isinstance(component, discord.ui.Button):
                    value = True

                # Then dispatch state update (may trigger on_state_changed on views)
                payload = ActionCreators.component_interaction(
                    component_id=component_id,
                    view_id=view.id,
                    user_id=interaction.user.id,
                    value=value,
                )
                await view.dispatch("COMPONENT_INTERACTION", payload)
            finally:
                _CURRENT_INTERACTION.reset(token)

        return stateful_callback


class StatefulButton(discord.ui.Button, StatefulComponent):
    """A button that interacts with state.

    The callback takes one positional argument, the interaction, sync or
    async. A button carries no value, so a callback declaring a required
    second parameter is refused at construction rather than raising a
    bare arity error on the first click.

    Setting ``owner_only=True`` gates the callback on
    ``interaction.user.id == view.user_id``. Mismatches route through
    the view's ``on_unauthorized`` hook (with ``unauthorized_message``
    as the default response) instead of invoking the user callback.
    Pairs with view-level ``owner_only=False`` to express "open view,
    host-only button" -- the canonical shape for lobby Start/Disband
    buttons, ticket Close buttons, and poll End buttons.

    Raises:
        TypeError: ``callback`` cannot be called with ``(interaction)``.
    """

    def __init__(self, *args, callback=None, owner_only: bool = False, **kwargs):
        super().__init__(*args, **kwargs)

        # Store original callback
        self.original_callback = callback

        # Per-component owner-only flag read by stateful_callback. Stored
        # under a leading-underscore name so it does not collide with the
        # underlying discord.py Button attribute namespace.
        self._button_owner_only = owner_only

        # Create stateful callback
        if callback:
            self.callback = self.create_stateful_callback(self, callback)


# Sentinel used for the placeholder option injected when ``options=[]``.
# The value is namespaced so it never collides with a real caller value,
# and the label is an em-dash to read as "nothing here" at a glance.
_EMPTY_SELECT_VALUE = "__cascadeui_empty__"
_EMPTY_SELECT_OPTION = discord.SelectOption(label="\u2014", value=_EMPTY_SELECT_VALUE)


class StatefulSelect(discord.ui.Select, StatefulComponent):
    """A select menu that interacts with state.

    The callback takes ``(interaction)`` or ``(interaction, values)``,
    sync or async; the two-parameter form receives the selected values on
    every pick. A callback that can accept neither shape is refused at
    construction.

    When ``options=[]`` is passed, a disabled placeholder option is
    substituted automatically and the select is forced to ``disabled=True``.
    This absorbs the Discord error 50035 that otherwise fires for
    dynamically-filtered selects whose filter produces an empty list --
    callers can pass the filtered list directly without a bespoke
    "render a disabled fallback" branch at every usage site.

    Raises:
        TypeError: ``callback`` can accept neither ``(interaction)`` nor
            ``(interaction, values)``; or ``options`` is a mapping or
            holds a non-``SelectOption`` entry.
        ValueError: More than 25 options.
    """

    def __init__(self, *args, callback=None, owner_only: bool = False, **kwargs):
        # Dynamic filters (e.g. "abilities still below the cap") may leave
        # ``options`` empty. Discord rejects zero-option selects; swap in
        # a single disabled placeholder so the select still renders and
        # the surrounding layout stays stable across state changes.
        options = kwargs.get("options")
        if options is not None:
            # A mapping has to be excluded before len() reads it, because it
            # answers with a different meaning: len({"label": .., "value": ..})
            # is 2, which clears both the empty check and the cap below, and
            # discord.py then iterates the keys into two options named after
            # them. Entries are checked here rather than at send, where the
            # only symptom is an AttributeError raised inside discord.py.
            if hasattr(options, "items"):
                raise TypeError(
                    "StatefulSelect options must be a list of SelectOption, not a "
                    "single mapping. Iterating a mapping yields its keys.\n"
                    "  Fix: wrap it in a list, or use Dropdown for dict shorthand."
                )
            for index, opt in enumerate(options):
                if not isinstance(opt, discord.SelectOption):
                    raise TypeError(
                        f"StatefulSelect options[{index}] must be a SelectOption, got "
                        f"{type(opt).__name__}: {opt!r}\n"
                        f"  Fix: pass discord.SelectOption(label=..., value=...), or use "
                        f"Dropdown for {{'label': ..., 'value': ...}} shorthand."
                    )

            if len(options) == 0:
                kwargs["options"] = [_EMPTY_SELECT_OPTION]
                kwargs["disabled"] = True
            elif len(options) > MAX_SELECT_OPTIONS:
                # Discord 400s a select with more than 25 options at send. Raise
                # here so the mistake surfaces at construction, matching the cap
                # choice_row already enforces for the same limit.
                raise ValueError(
                    f"StatefulSelect has {len(options)} options; Discord accepts at most "
                    f"{MAX_SELECT_OPTIONS}. Trim the list or split the choices across selects."
                )

        super().__init__(*args, **kwargs)

        # Store original callback
        self.original_callback = callback

        # Per-component owner-only flag (mirrors StatefulButton). Routes
        # through the view's ``on_unauthorized`` hook on mismatch.
        self._button_owner_only = owner_only

        # Create stateful callback
        if callback:
            self.callback = self.create_stateful_callback(self, callback)

    def set_selected(self, value: Union[str, Iterable[str], None]) -> None:
        """Mark which option(s) render with ``default=True``.

        The canonical way to reflect state in a select without rebuilding
        the whole component. Walks ``self.options`` once and sets
        ``opt.default = (opt.value in targets)``, so callers can drop the
        ``clear_items()`` + ``add_item()`` rebuild dance entirely::

            def build_ui(self):
                current = self.scoped_state.get("settings", {}).get("theme")
                self._theme_select.set_selected(current)
                return {"embed": self.build_embed()}

        Accepts three shapes:

        - ``None`` or an empty iterable clears every ``default`` flag.
        - A single ``str`` marks one matching option (the single-select
          common case).
        - An iterable of strings marks every matching option, for
          selects with ``max_values > 1``.

        Values that do not match any existing option are silently
        ignored. State-driven rebuilds may temporarily reference values
        that no longer exist (e.g. after a config migration drops an
        enum variant); silently no-op keeps the render alive rather
        than crashing the rebuild path.
        """
        if value is None:
            targets: set = set()
        elif isinstance(value, str):
            targets = {value}
        else:
            targets = set(value)

        for opt in self.options:
            opt.default = opt.value in targets

    def get_selected(self) -> List[str]:
        """Return the values of all options currently marked ``default=True``.

        Always returns a list for type stability, matching discord.py's
        ``Select.values`` convention (which is also always a list even
        for ``max_values == 1``). Single-select views typically read the
        result as::

            current = self._theme_select.get_selected()
            current_theme = current[0] if current else "default"

        Multi-select views (``max_values > 1``) can iterate the list
        directly. Returns an empty list when no option is marked default.
        """
        return [opt.value for opt in self.options if opt.default]


# // ========================================( Dynamic Persistent Button )======================================== // #


# Registry mapping fully-qualified class path (module.QualName) -> subclass for every
# DynamicPersistentButton subclass that declares a template. The qualified key prevents
# cross-module collisions when two unrelated cogs define a class with the same bare
# name. Populated at class-definition time via __init_subclass__; consumed by
# PersistenceManager.reattach_persistent_views, which registers the full set with the
# bot via bot.add_dynamic_items(*classes) on every reattach pass.
_dynamic_button_classes: Dict[str, type] = {}


# Regex capture-group names that auto-coerce to int. Matches the snowflake domain
# convention used elsewhere in the library (user_id, guild_id, etc.).
_SNOWFLAKE_CAPTURES: FrozenSet[str] = frozenset(
    {
        "user_id",
        "guild_id",
        "channel_id",
        "role_id",
        "message_id",
    }
)


# Sentinel regex for DynamicPersistentButton itself. discord.py's
# DynamicItem.__init_subclass__ requires a ``template=`` kwarg on every
# subclass, so the intermediate base class must declare one. ``(?!)`` is a
# negative lookahead of empty string -- it never matches any input, so
# even if the base class were accidentally registered via
# bot.add_dynamic_items, no real custom_id would route to it.
_NEVER_MATCH_TEMPLATE = r"(?!)"


class DynamicPersistentButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=_NEVER_MATCH_TEMPLATE,
):
    """Persistent button whose state lives in its ``custom_id``.

    Subclass this when a button's click handler depends only on IDs
    encoded in the ``custom_id`` and no view-level state is involved.
    The subclass declares a ``template`` regex with named capture
    groups and defines ``__init__`` accepting those captures as
    keyword arguments; the default :meth:`from_custom_id` extracts and
    coerces them. Override :meth:`on_click` for click handling.

    Unlike :class:`PersistentView`, this has no view-level lifecycle.
    The click handler runs on a fresh instance re-constructed per
    click from the ``custom_id`` alone. Reach for this when the state
    that matters is purely "which button was clicked" (role ID,
    category slug, ticket type) rather than "what does the view know
    right now."

    Subclass registration is automatic: every subclass declaring a
    ``template=`` lands in a module-level registry. The reattach pass
    run by ``setup_middleware(PersistenceMiddleware(..., bot=bot))``
    calls ``bot.add_dynamic_items(*classes)`` so every subclass routes
    correctly after a restart; a subclass imported later (a cog loaded
    after setup) is wired in by the same re-drive when
    ``PersistenceManager.reattach()`` runs, matching the recovery for
    late-imported persistent views.

    Snowflake capture coercion is automatic for groups named
    ``user_id``, ``guild_id``, ``channel_id``, ``role_id``, or
    ``message_id``. The default :meth:`from_custom_id` converts those
    to ``int`` before constructing the instance.

    ``discord.py`` requires ``template=`` on every ``DynamicItem``
    subclass, so intermediate abstract bases in user code are not
    supported by the upstream API. Subclass this class directly with
    a concrete template.

    Example::

        class RoleToggleButton(
            DynamicPersistentButton,
            template=r"roles:(?P<category>[a-z_]+):(?P<role_id>[0-9]+)",
        ):
            def __init__(self, *, category: str, role_id: int):
                button = discord.ui.Button(
                    label=f"Toggle {category}",
                    custom_id=f"roles:{category}:{role_id}",
                    style=discord.ButtonStyle.primary,
                )
                super().__init__(button)
                self.category = category
                self.role_id = role_id

            async def on_click(self, interaction):
                # self.category and self.role_id set by __init__
                await self.respond(interaction, "Toggled!", ephemeral=True)
    """

    _persistent: bool = True

    # Ack backstop for the dynamic-dispatch path, which has no view-level
    # ``_scheduled_task`` timer. Mirrors ``_StatefulMixin.auto_defer_delay``.
    auto_defer_delay: float = 2.5

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # The custom_id is a template-matched value supplied by the
        # subclass __init__ (or the from_custom_id classmethod on
        # restore). Mark it as user-provided so the parent view's
        # _stabilize_custom_ids skips it -- rewriting would clobber the
        # template match and discord.py would raise on assignment.
        self._provided_custom_id = True

    def __init_subclass__(cls, **kwargs):
        """Auto-register every concrete subclass for bot-level dispatch.

        ``discord.py`` enforces ``template=`` on every subclass, so any
        class that reaches this method is dispatch-ready. Register it
        unconditionally; the only class that never appears here is
        ``DynamicPersistentButton`` itself, because a class's
        ``__init_subclass__`` runs on its subclasses, not on itself.

        Also validates ``auto_defer_delay`` (must be a positive number) at
        definition time, mirroring ``Modal``.
        """
        super().__init_subclass__(**kwargs)
        delay = cls.auto_defer_delay
        if not isinstance(delay, (int, float)) or isinstance(delay, bool) or delay <= 0:
            raise ValueError(
                f"{cls.__name__}.auto_defer_delay must be a positive number, got {delay!r}"
            )
        _dynamic_button_classes[f"{cls.__module__}.{cls.__qualname__}"] = cls

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        """Reconstruct this item from a matched ``custom_id``.

        Default behavior: extract ``match.groupdict()``, coerce any
        snowflake-named captures to ``int`` via
        :func:`~cascadeui.utils.coercion.coerce_snowflake_match`, and
        call ``cls(**captures)``. The subclass ``__init__`` receives
        the captured values as keyword arguments and builds the
        underlying Button.

        Override when the subclass needs custom extraction (non-
        snowflake coercion, combined keys, lookup-based restoration).
        """
        captures = coerce_snowflake_match(match.groupdict(), _SNOWFLAKE_CAPTURES)
        return cls(**captures)

    async def _auto_defer_timer(self, interaction) -> None:
        """Defer the interaction if ``on_click`` has not responded in time.

        Dynamic items dispatch outside a view's ``_scheduled_task``, so this is
        the click's only ack backstop. A role toggle runs one or two REST role
        mutations before its response, on the 3s interaction clock; without this
        timer a slow mutation drops the interaction (10062). Armed before
        ``on_click``, not after, because a trailing defer fires too late to beat
        the wall when the pre-response work is itself the slow part.
        """
        await ack_backstop(
            interaction, self.auto_defer_delay, owner=self.__class__.__name__, log=logger
        )

    async def callback(self, interaction):
        """Dispatch to :meth:`on_click`, binding ``_CURRENT_INTERACTION``.

        Subclasses override :meth:`on_click` rather than this method.
        The contextvar binding matches :class:`StatefulComponent`'s
        pattern so state dispatches inside ``on_click`` engage the
        acting-view fast path in ``_StatefulMixin.refresh()`` when a
        :class:`PersistentView` hosts this button and reacts to the
        same state change.

        An auto-defer timer is armed before ``on_click`` (the dynamic
        dispatch path has no ``_scheduled_task`` backstop), and a
        post-callback defer acks a fast handler that edited via the
        channel endpoint without touching the interaction response.
        """
        token = _CURRENT_INTERACTION.set(interaction)
        defer_task = asyncio.create_task(self._auto_defer_timer(interaction))
        try:
            await await_maybe(self.on_click(interaction))
        finally:
            _CURRENT_INTERACTION.reset(token)
            if not defer_task.done():
                defer_task.cancel()
            await trailing_ack(interaction, owner=self.__class__.__name__, log=logger)

    async def respond(
        self,
        interaction,
        content: Optional[str] = None,
        *,
        ephemeral: bool = False,
        **kwargs,
    ) -> None:
        """Send an interaction response, falling back to followup if already acked.

        ``callback`` arms an auto-defer timer before ``on_click`` runs, so a
        direct ``interaction.response.send_message`` raises
        ``InteractionResponded`` once that timer has acked. This checks
        ``interaction.response.is_done()`` and routes to
        ``interaction.followup.send`` when the slot is already consumed.
        The dynamic-item path has no view instance, so this mirrors
        ``_StatefulMixin.respond`` on the button itself.
        """
        await respond_safe(interaction, content, ephemeral=ephemeral, **kwargs)

    async def open_modal(
        self,
        interaction,
        modal: discord.ui.Modal,
        *,
        fallback_message: Optional[str] = None,
    ) -> bool:
        """Open a modal, with a fallback if the response slot is consumed.

        The sibling of :meth:`respond` for the one response type that cannot
        follow a defer. ``callback`` arms an auto-defer timer before
        ``on_click`` runs, so a bare ``interaction.response.send_modal``
        raises once that timer has acked; this routes to an ephemeral
        fallback instead. Returns ``True`` when the modal opened.
        """
        return await open_modal_safe(interaction, modal, fallback_message=fallback_message)

    async def on_click(self, interaction) -> None:
        """Handle the click. Default: no-op.

        Subclasses override to implement click behavior. The captured
        values from the ``custom_id`` template are available as
        instance attributes that the subclass ``__init__`` set.

        Use ``self.respond(interaction, ...)`` for replies: ``callback``
        arms an auto-defer timer around this method, so a bare
        ``interaction.response.send_message`` raises ``InteractionResponded``
        once the timer has acked.
        """
        pass
