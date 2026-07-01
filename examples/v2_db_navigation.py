"""
V2 Database-Backed Navigation -- on_load + reload-on-render
===========================================================

A repo-backed task list: a root list view, a pushed detail view, and a
"new task" modal, all reading their rows from a repository instead of
carrying them in constructor kwargs. The source of truth is the repo;
the views never cache rows.

The pattern (the reason this example exists):
    - The repo is a stable, process-lifetime handle passed as a ``db=``
      constructor kwarg. It lands in ``_init_kwargs``, so ``pop()`` can
      reconstruct a view -- but no *rows* ride in kwargs, only the handle.
    - Each view loads its rows in ``async def on_load(self)`` and builds
      its tree there. CascadeUI calls ``on_load`` automatically before the
      first send and on every push/pop edit, so a view always renders
      against current data.
    - After a mutation, ``reload()`` (``on_load`` + ``refresh``) re-fetches
      and re-renders in one call.

Reload-on-render payoff:
    The detail view deletes a task and ``pop()``s. The root view's
    ``on_load`` runs on the way back, re-reads the repo, and shows the
    list without the deleted task -- with no manual refresh of the root
    and no ``rebuild=`` callback on the navigation call. Compare
    ``examples/navigation.py`` (V1), which threads
    ``rebuild=lambda v: {"embed": v.build_embed()}`` through every push
    and pop; ``on_load`` removes that boilerplate.

Also shown: a ``PaginatedRegion`` pages the row list inside the root view.
``on_load`` feeds it the current rows and renders one page's slice, so a
page turn re-slices against fresh repo data.

Commands:
    /tasks   Open the task list

Usage:
    Load this cog in your bot. Requires: pip install pycascadeui discord.py
"""

# // ========================================( Modules )======================================== // #


import asyncio

import discord
from discord.ext import commands
from discord.ext.commands import Context
from discord.ui import ActionRow

from cascadeui import (
    Modal,
    PaginatedRegion,
    StatefulButton,
    StatefulLayoutView,
    TextInput,
    action_section,
    card,
    key_value,
)

# // ========================================( Repository )======================================== // #


class _TaskRepo:
    """In-memory stand-in for an async database repository.

    Every method is ``async`` with a no-op ``await`` marking where real
    database I/O would happen (a Postgres query, an HTTP call). A real bot
    builds one of these from its connection pool once and hands it to the
    views via the ``db=`` kwarg.
    """

    def __init__(self) -> None:
        self._tasks = {
            1: {"title": "Write the migration", "done": True},
            2: {"title": "Review the open PR", "done": False},
            3: {"title": "Ship the release", "done": False},
            4: {"title": "Draft the changelog", "done": True},
            5: {"title": "Update the docs site", "done": False},
            6: {"title": "Triage the bug queue", "done": False},
            7: {"title": "Plan the next sprint", "done": False},
        }
        self._next_id = 8

    async def list_tasks(self):
        await asyncio.sleep(0)  # where a real SELECT awaits
        return [(tid, dict(task)) for tid, task in sorted(self._tasks.items())]

    async def get_task(self, task_id):
        await asyncio.sleep(0)
        task = self._tasks.get(task_id)
        return dict(task) if task else None

    async def add_task(self, title):
        await asyncio.sleep(0)
        task_id = self._next_id
        self._next_id += 1
        self._tasks[task_id] = {"title": title, "done": False}
        return task_id

    async def toggle_task(self, task_id):
        await asyncio.sleep(0)
        if task_id in self._tasks:
            self._tasks[task_id]["done"] = not self._tasks[task_id]["done"]

    async def delete_task(self, task_id):
        await asyncio.sleep(0)
        self._tasks.pop(task_id, None)


# // ========================================( Views )======================================== // #


class TaskListView(StatefulLayoutView):
    """Root list, rebuilt from the repo every time it is displayed.

    ``on_load`` reads the rows fresh on the first send and on every return
    via ``pop()``, so the list is never stale -- a child that deleted or
    added a task is reflected the moment navigation lands back here.
    """

    owner_only = True
    instance_limit = 1
    instance_scope = "user"
    instance_policy = "replace"
    replace_policy = "delete"
    exit_policy = "delete"
    # Rows live in the repo, not Redux -- no scoped state, no subscriptions.
    state_scope = None
    subscribed_actions = set()
    # Callbacks rebuild then reload(); the library acks each click for us.
    auto_defer = True
    timeout = 300.0

    def __init__(self, *args, db, **kwargs):
        # ``db`` is a non-reserved kwarg, so it is captured into _init_kwargs
        # and survives pop() reconstruction. The repo is a cheap, stable
        # handle -- rows are NOT carried here; on_load() fetches them.
        self.db = db
        # Pages the row list inside this view. on_load feeds it the current
        # rows; controls(self) returns the Prev/Next row (first/last + goto
        # appear automatically once the page count reaches jump_threshold).
        self.tasks_pager = PaginatedRegion(per_page=4)
        super().__init__(*args, **kwargs)

    async def on_load(self) -> None:
        # Runs before the first send and on every navigation edit back to this
        # view; clear_items() resets the tree before each rebuild.
        tasks = await self.db.list_tasks()
        self.clear_items()

        if tasks:
            # The pager owns the page index; a page turn re-runs on_load (via
            # reload()) and re-slices the list automatically.
            self.tasks_pager.items = tasks
            sections = [
                action_section(
                    f"{'✅' if task['done'] else '⬜'} **{task['title']}**",
                    label="Open",
                    callback=self._make_open(task_id),
                )
                for task_id, task in self.tasks_pager.page_items
            ]
            self.add_item(card("## Tasks", *sections, *self.tasks_pager.controls(self)))
        else:
            self.add_item(card("## Tasks", "*No tasks yet -- add one below.*"))

        # The root is the stack floor -- no Back button -- so build the
        # footer row by hand (New Task + Exit) rather than make_nav_row().
        self.add_item(
            ActionRow(
                StatefulButton(
                    label="New Task",
                    style=discord.ButtonStyle.success,
                    emoji="➕",
                    callback=self._new_task,
                ),
                self.make_exit_button(),
            )
        )

    def _make_open(self, task_id):
        # The task id is bound at build time so each row's button opens its
        # own detail view. Only the id and the repo travel, never the row --
        # the detail's on_load re-fetches by id, so it shows the latest state
        # even if another action changed the task after this build.
        async def _open(interaction):
            await self.push(TaskDetailView, interaction, db=self.db, task_id=task_id)

        return _open

    async def _new_task(self, interaction):
        title_input = TextInput(
            label="Task title",
            placeholder="What needs doing?",
            required=True,
            min_length=1,
            max_length=80,
        )

        async def on_submitted(modal_interaction, values):
            # min_length=1 blocks an empty box, but a single space strips to
            # "", so guard before writing.
            title = (title_input.value or "").strip()
            if not title:
                await self.respond(modal_interaction, "Title cannot be blank.", ephemeral=True)
                return
            await self.db.add_task(title)
            # reload() = on_load() + refresh(): re-read the repo and re-render
            # this view so the new task appears immediately.
            await self.reload()

        await self.open_modal(
            interaction,
            Modal(title="New Task", inputs=[title_input], callback=on_submitted),
        )


class TaskDetailView(StatefulLayoutView):
    """Detail for one task, loaded by id from the repo.

    No row data is passed in -- only the ``task_id`` and the ``db`` handle.
    ``on_load`` fetches the row, so the detail reflects the latest state
    even if another view changed it. ``make_nav_row()`` supplies the
    Back + Exit footer; Back needs no ``rebuild=`` callback because the
    restored parent reloads through its own ``on_load``.
    """

    owner_only = True
    state_scope = None
    subscribed_actions = set()
    exit_policy = "delete"  # drop the detail message on exit, matching the root
    # Callbacks rebuild then reload(); the library acks each click for us.
    auto_defer = True
    # Match the root's timeout so pop() lands on a live view. Pushed sub-views
    # need no instance_* or replace_* policies: those gate user-facing send(),
    # and this view only ever arrives via push().
    timeout = 300.0

    def __init__(self, *args, db, task_id, **kwargs):
        self.db = db
        self.task_id = task_id
        super().__init__(*args, **kwargs)

    async def on_load(self) -> None:
        task = await self.db.get_task(self.task_id)
        self.clear_items()

        if task is None:
            self.add_item(card("## Task removed", "*This task no longer exists.*"))
            self.add_item(self.make_nav_row())
            return

        status = "✅ Done" if task["done"] else "⬜ Not done"
        self.add_item(card(f"## {task['title']}", key_value({"Status": status})))
        self.add_item(
            ActionRow(
                StatefulButton(
                    label="Toggle Done",
                    style=discord.ButtonStyle.primary,
                    callback=self._toggle,
                ),
                StatefulButton(
                    label="Delete",
                    style=discord.ButtonStyle.danger,
                    callback=self._delete,
                ),
            )
        )
        # make_nav_row() returns one ActionRow with Back + Exit. Back pops
        # the stack; the restored parent reloads via its own on_load.
        self.add_item(self.make_nav_row())

    async def _toggle(self, interaction):
        await self.db.toggle_task(self.task_id)
        await self.reload()  # re-fetch this task and re-render in place

    async def _delete(self, interaction):
        await self.db.delete_task(self.task_id)
        # pop() runs the root's on_load on the way back -- the list reappears
        # without the deleted task, no manual refresh needed.
        await self.pop(interaction)


# // ========================================( Cog )======================================== // #


class DatabaseNavigationExample(commands.Cog, name="db_navigation_example"):
    """Repo-backed navigation demo: on_load preload + reload-on-render."""

    def __init__(self, bot) -> None:
        self.bot = bot
        # One repo per process, built at cog load and shared by every view.
        self.repo = _TaskRepo()

    @commands.hybrid_command(name="tasks", description="Open the repo-backed task list.")
    async def tasks(self, context: Context) -> None:
        view = TaskListView(context=context, db=self.repo)
        await view.send()


async def setup(bot) -> None:
    await bot.add_cog(DatabaseNavigationExample(bot=bot))
