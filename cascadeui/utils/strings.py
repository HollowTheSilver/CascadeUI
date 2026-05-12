# // ========================================( Modules )======================================== // #


import re

# // ========================================( Functions )======================================== // #


def slugify(text: str) -> str:
    """Convert a display string to a safe ``custom_id`` fragment.

    Lowercases the text and replaces non-alphanumeric runs with a single
    underscore.  Useful for building deterministic ``custom_id`` values
    from user-facing labels in persistent views::

        from cascadeui.utils import slugify

        custom_id = f"roles:{slugify(category)}:{slugify(role_name)}"
    """
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
