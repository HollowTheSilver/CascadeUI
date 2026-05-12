# // ========================================( Modules )======================================== // #


from typing import List, Tuple, Type

from discord.ui import (
    ActionRow,
    Button,
    Checkbox,
    CheckboxGroup,
    Container,
    File,
    FileUpload,
    Label,
    MediaGallery,
    RadioGroup,
    Section,
    Separator,
    TextDisplay,
    Thumbnail,
)
from discord.ui.select import BaseSelect

# Item types that belong inside a ``Modal``, never inside a ``LayoutView``
# tree. discord.py accepts them at any tree position because every check
# is ``isinstance(item, Item)``; Discord's API server rejects them at send
# time. The validator catches them at top-level and Container-child
# positions with a clear "this belongs in a Modal" message.
_MODAL_ONLY_TYPES: Tuple[Type, ...] = (Label, RadioGroup, CheckboxGroup, Checkbox, FileUpload)

# MediaGallery's documented item-count range. Discord's API docs at
# ``developers/components/reference.mdx`` describe MediaGallery as
# displaying "1-10 media attachments" with the ``items`` field typed
# as "1 to 10 media gallery items". discord.py does not enforce either
# bound at construction.
_MEDIA_GALLERY_MIN_ITEMS = 1
_MEDIA_GALLERY_MAX_ITEMS = 10

# Section's documented child-count minimum. Discord's API docs at
# ``developers/components/reference.mdx`` describe the Section
# ``components`` field as "One to three child components". The
# ``accessory`` is a separate field and does NOT count toward this
# cap. discord.py's ``Section.__init__`` routes every child through
# ``add_item`` (``section.py:80-83``) which raises on the fourth
# child, so the upper bound is unreachable through normal
# construction -- the validator enforces only the lower bound here.
_SECTION_MIN_CHILDREN = 1

# Container minimum child count. Discord's API docs do NOT document a
# minimum or maximum on Container children -- the ``components`` field
# is typed as just "array of container child components". The library
# enforces ``min = 1`` conservatively because an empty Container has no
# content to render. There is no library-enforced maximum: the only
# documented Discord cap that applies is the message-level 40-component
# recursive total, which the validator does not check.
_CONTAINER_MIN_CHILDREN = 1

# ActionRow minimum child count. Discord's API docs describe ActionRow
# as holding "Up to 5 interactive button components or a single select
# component". An empty ActionRow has no interactive component to render.
# discord.py enforces the upper width budget (5 button-units, with
# selects = 5) at construction so the validator only enforces the
# minimum here.
_ACTION_ROW_MIN_CHILDREN = 1

# // ========================================( V2 Placement Validator )======================================== // #


def validate_placement(view) -> None:
    """Walk the V2 component tree, raise ``ValueError`` on first violation.

    discord.py validates ``isinstance(item, Item)`` plus a few hard size
    limits. Most placement rules (Section accessory type, Container
    nesting, top-level types) are enforced only by Discord's API server
    at send time, returning HTTP 400 with terse error text far from the
    construction site. This walker catches the same violations earlier
    and raises a clear ``ValueError`` naming the violation, the path
    through the tree, and the suggested fix.

    Called from ``_send_pipeline`` before the Discord HTTP round-trip.
    Subclasses can opt out by setting ``validate_placement = False`` on
    the view class -- the recommended use is for trees that exercise a
    composition discord.py / Discord has updated to allow that the
    validator has not caught up to yet.

    Args:
        view: The ``LayoutView`` (or ``StatefulLayoutView`` subclass)
            whose component tree should be validated.

    Raises:
        ValueError: First placement violation encountered. Subsequent
            violations in the same tree are not reported -- fix the
            first one and re-validate.
    """
    root_path: List[str] = [type(view).__name__]
    for index, child in enumerate(view.children):
        child_path = root_path + [f"{type(child).__name__}[{index}]"]
        _validate_top_level(child, child_path)


# // ========================================( Layer Walkers )======================================== // #


def _validate_top_level(item, path: List[str]) -> None:
    """LayoutView top-level child rules.

    Legal: Container, Section, TextDisplay, MediaGallery, File,
    Separator, ActionRow. Standalone Button / Select / Thumbnail are
    rejected -- Buttons and Selects belong inside an ActionRow, and
    Thumbnail is only legal as a Section accessory. Modal-only types
    (Label, RadioGroup, CheckboxGroup, Checkbox, FileUpload) are
    rejected with a directed message pointing at the Modal subsystem.
    """
    if isinstance(item, Container):
        _validate_container(item, path)
        return
    if isinstance(item, Section):
        _validate_section(item, path)
        return
    if isinstance(item, ActionRow):
        _validate_action_row(item, path)
        return
    if isinstance(item, MediaGallery):
        _check_media_gallery_size(item, path)
        return
    if isinstance(item, (TextDisplay, File, Separator)):
        return
    if isinstance(item, _MODAL_ONLY_TYPES):
        _raise_placement_error(
            path,
            type(item).__name__,
            "LayoutView",
            f"{type(item).__name__} is a Modal-only component. Move it inside a "
            "discord.ui.Modal subclass; LayoutView trees do not accept it.",
        )
    if isinstance(item, Button):
        _raise_placement_error(
            path,
            "Button",
            "LayoutView",
            "Wrap the Button in an ActionRow before adding it to the LayoutView.",
        )
    if isinstance(item, BaseSelect):
        _raise_placement_error(
            path,
            type(item).__name__,
            "LayoutView",
            "Wrap the Select in an ActionRow before adding it to the LayoutView.",
        )
    if isinstance(item, Thumbnail):
        _raise_placement_error(
            path,
            "Thumbnail",
            "LayoutView",
            "Thumbnail can only be used as a Section's accessory; wrap it in a Section.",
        )
    # Unknown Item subclass: silently allow. Could be a future Discord
    # type that the matrix has not been updated for yet.


def _validate_container(container: Container, path: List[str]) -> None:
    """Container child rules.

    Legal: ActionRow, TextDisplay, Section, MediaGallery, File,
    Separator. Container nesting is rejected. Standalone interactive
    components (Button, Select), Thumbnail, and Modal-only types are
    rejected. Container must hold at least one child (library-imposed
    -- Discord does not document a per-Container child cap; the
    message-level 40-component recursive total is the only documented
    Discord-level cap and is enforced separately at send time by
    Discord's API server).
    """
    _check_container_size(container, path)
    for index, child in enumerate(container.children):
        child_path = path + [f"{type(child).__name__}[{index}]"]
        if isinstance(child, Container):
            _raise_placement_error(
                child_path,
                "Container",
                "Container",
                "Containers cannot nest. Move the inner Container's children up to the "
                "outer Container, or split into a separate top-level Container.",
            )
        if isinstance(child, Section):
            _validate_section(child, child_path)
            continue
        if isinstance(child, ActionRow):
            _validate_action_row(child, child_path)
            continue
        if isinstance(child, MediaGallery):
            _check_media_gallery_size(child, child_path)
            continue
        if isinstance(child, (TextDisplay, File, Separator)):
            continue
        if isinstance(child, _MODAL_ONLY_TYPES):
            _raise_placement_error(
                child_path,
                type(child).__name__,
                "Container",
                f"{type(child).__name__} is a Modal-only component. Move it inside a "
                "discord.ui.Modal subclass; Container trees do not accept it.",
            )
        if isinstance(child, Button):
            _raise_placement_error(
                child_path,
                "Button",
                "Container",
                "Buttons must be wrapped in an ActionRow before being added to a Container.",
            )
        if isinstance(child, BaseSelect):
            _raise_placement_error(
                child_path,
                type(child).__name__,
                "Container",
                "Selects must be wrapped in an ActionRow before being added to a Container.",
            )
        if isinstance(child, Thumbnail):
            _raise_placement_error(
                child_path,
                "Thumbnail",
                "Container",
                "Thumbnail can only be used as a Section's accessory; wrap it in a Section.",
            )
        # Unknown Item subclass: silently allow.


def _validate_section(section: Section, path: List[str]) -> None:
    """Section rules.

    Children must all be ``TextDisplay`` (strings auto-wrap at the
    discord.py layer). The ``components`` field is documented as 1-3
    children; discord.py enforces the upper bound at construction so
    the validator only enforces the lower bound (empty Section). The
    accessory is a separate field and does not count toward the cap.
    Accessory must be ``Button`` or ``Thumbnail``. A ``Section`` whose
    children include another ``Section`` is rejected -- Discord rejects
    nested Sections at HTTP send. Unknown Item subclasses pass silently
    to leave headroom for future Discord types the matrix has not been
    updated for yet.
    """
    _check_section_size(section, path)
    for index, child in enumerate(section.children):
        child_path = path + [f"children[{index}]({type(child).__name__})"]
        if isinstance(child, Section):
            _raise_placement_error(
                child_path,
                "Section",
                "Section",
                "Sections cannot nest. Move the inner Section's content up to the "
                "outer Container, or replace one of the Sections with a TextDisplay.",
            )
        if isinstance(child, TextDisplay):
            continue
        # Children that are clearly wrong-domain raise; truly unknown
        # types pass through so a future Discord type does not break.
        if isinstance(
            child,
            (Container, ActionRow, MediaGallery, File, Separator, Thumbnail, Button, BaseSelect),
        ) or isinstance(child, _MODAL_ONLY_TYPES):
            _raise_placement_error(
                child_path,
                type(child).__name__,
                "Section.children",
                "Section's left column accepts only TextDisplay (or strings, which "
                "auto-wrap to TextDisplay at construction).",
            )

    accessory = section.accessory
    if isinstance(accessory, (Button, Thumbnail)):
        return
    # Known wrong-domain accessory types raise with a directed message.
    # Truly unknown Item subclasses pass through -- Discord may add a
    # third accessory kind, and the matrix should not lock that out.
    if isinstance(
        accessory,
        (Container, Section, ActionRow, MediaGallery, File, Separator, TextDisplay, BaseSelect),
    ) or isinstance(accessory, _MODAL_ONLY_TYPES):
        accessory_path = path + [f"accessory({type(accessory).__name__})"]
        _raise_placement_error(
            accessory_path,
            type(accessory).__name__,
            "Section.accessory",
            "Section's right-side accessory must be a Button or a Thumbnail. "
            "Use image_section() for a Thumbnail accessory or action_section() / "
            "toggle_section() / link_section() for a Button accessory.",
        )


def _validate_action_row(row: ActionRow, path: List[str]) -> None:
    """ActionRow rules.

    Children must be ``Button`` or ``Select``. Known wrong-domain
    types (Container, Section, etc.) raise with a directed message;
    unknown Item subclasses pass through to leave headroom for future
    Discord types and ``DynamicItem``-wrapped components that
    discord.py serializes as Button/Select at send time. The width
    budget (5 units, with Button=1 and Select=5) is enforced by
    discord.py at construction so the validator does not re-check it.
    Empty ActionRows are rejected per Discord's documented contract.
    """
    _check_action_row_size(row, path)
    for index, child in enumerate(row.children):
        child_path = path + [f"{type(child).__name__}[{index}]"]
        if isinstance(child, (Button, BaseSelect)):
            continue
        if isinstance(
            child,
            (Container, Section, ActionRow, MediaGallery, File, Separator, TextDisplay, Thumbnail),
        ) or isinstance(child, _MODAL_ONLY_TYPES):
            _raise_placement_error(
                child_path,
                type(child).__name__,
                "ActionRow",
                "ActionRow children must be Buttons or Selects.",
            )
        # Unknown Item subclass: silently allow.


# // ========================================( Cap Helpers )======================================== // #


def _check_media_gallery_size(gallery: MediaGallery, path: List[str]) -> None:
    """Reject a ``MediaGallery`` outside Discord's 1-10 item range.

    discord.py 2.7.x enforces the 10-item cap on post-construction
    mutations (``add_item`` / ``append_item`` / ``insert_item_at`` and
    the ``items`` setter all raise), but ``MediaGallery.__init__``
    routes through ``MediaGalleryComponent._raw_construct`` which
    bypasses validation. ``MediaGallery(*items)`` with eleven or more
    items therefore constructs cleanly and only fails at HTTP send.
    Empty galleries pass discord.py entirely and likewise fail at send.
    The validator catches both shapes here.
    """
    count = len(gallery.items)
    if count < _MEDIA_GALLERY_MIN_ITEMS:
        raise ValueError(
            f"Invalid V2 placement: MediaGallery has no items "
            f"(Discord requires {_MEDIA_GALLERY_MIN_ITEMS}-"
            f"{_MEDIA_GALLERY_MAX_ITEMS} items).\n"
            f"  Path: {' -> '.join(path)}\n"
            f"  Discord rejects this composition with HTTP 400.\n"
            f"  Fix: Add at least one MediaGalleryItem, or omit the "
            f"MediaGallery entirely."
        )
    if count > _MEDIA_GALLERY_MAX_ITEMS:
        raise ValueError(
            f"Invalid V2 placement: MediaGallery exceeds Discord's "
            f"{_MEDIA_GALLERY_MAX_ITEMS}-item cap (got {count}).\n"
            f"  Path: {' -> '.join(path)}\n"
            f"  Discord rejects this composition with HTTP 400.\n"
            f"  Fix: Split the gallery into multiple MediaGallery "
            f"components, each holding at most {_MEDIA_GALLERY_MAX_ITEMS} items."
        )


def _check_container_size(container: Container, path: List[str]) -> None:
    """Reject an empty ``Container``.

    Discord's API documentation does NOT specify a per-Container child
    cap; the ``components`` field is typed as just "array of container
    child components". The library enforces a minimum of one child
    because an empty Container has no content to render. The only
    documented Discord cap that applies to nested children is the
    message-level 40-component recursive total, which Discord's API
    server enforces at send time and the validator does not re-check.
    """
    count = len(container.children)
    if count < _CONTAINER_MIN_CHILDREN:
        raise ValueError(
            f"Invalid V2 placement: Container has no children.\n"
            f"  Path: {' -> '.join(path)}\n"
            f"  A Container without children has no content to render.\n"
            f"  Fix: Add at least one child to the Container, or remove "
            f"the empty Container from the tree."
        )


def _check_section_size(section: Section, path: List[str]) -> None:
    """Reject an empty ``Section``.

    Discord's API documentation specifies Section's ``components``
    field as "One to three child components". The ``accessory`` is a
    separate field and does NOT count toward this cap. discord.py's
    ``Section.add_item`` raises on the fourth child and ``__init__``
    routes every constructor argument through it, so the upper bound
    is enforced at construction; the validator catches only the lower
    bound (the empty-Section case ``Section(accessory=...)`` allows
    but Discord rejects at HTTP send).
    """
    count = len(section.children)
    if count < _SECTION_MIN_CHILDREN:
        raise ValueError(
            f"Invalid V2 placement: Section has no children "
            f"(Discord requires at least {_SECTION_MIN_CHILDREN}).\n"
            f"  Path: {' -> '.join(path)}\n"
            f"  Discord rejects this composition with HTTP 400.\n"
            f"  Fix: Add at least one TextDisplay child to the Section, or "
            f"replace the Section with the accessory component alone."
        )


def _check_action_row_size(row: ActionRow, path: List[str]) -> None:
    """Reject an empty ``ActionRow``.

    Discord's API documentation describes ActionRow as holding "Up to
    5 interactive button components or a single select component"; an
    ActionRow with zero children has nothing to render. discord.py
    enforces the upper bound (5-unit width budget) at construction but
    does not enforce the minimum. The maximum is left to discord.py.
    """
    count = len(row.children)
    if count < _ACTION_ROW_MIN_CHILDREN:
        raise ValueError(
            f"Invalid V2 placement: ActionRow has no children "
            f"(Discord requires at least {_ACTION_ROW_MIN_CHILDREN}).\n"
            f"  Path: {' -> '.join(path)}\n"
            f"  Discord rejects this composition with HTTP 400.\n"
            f"  Fix: Add at least one Button or Select to the ActionRow, "
            f"or remove the empty ActionRow from the tree."
        )


# // ========================================( Error Helper )======================================== // #


def _raise_placement_error(path: List[str], child_type: str, parent_type: str, fix: str) -> None:
    """Format a placement violation and raise ``ValueError``.

    Path elements join with `` -> `` for readability. Bracket indices
    on each segment make the offending node unambiguous when the same
    type appears multiple times at the same depth.
    """
    raise ValueError(
        f"Invalid V2 placement: {child_type} cannot be a child of {parent_type}.\n"
        f"  Path: {' -> '.join(path)}\n"
        f"  Discord rejects this composition with HTTP 400.\n"
        f"  Fix: {fix}"
    )
