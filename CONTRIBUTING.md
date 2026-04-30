# Contributing to CascadeUI

Thanks for your interest in contributing. This guide covers what
contributions are welcome, how to set up a development environment, and
the standards your code and prose must meet.

## Before You Start

**Open an issue first.** Bug fixes for clear regressions can go straight to
a PR, but new features, API changes, and new view/component patterns should
be discussed in a GitHub issue before code is written. This prevents wasted
effort on changes that don't align with the project's design direction.

**Read the Design Philosophy section below.** CascadeUI has a deliberate API
grammar and a specific stance on what belongs in the library vs. user code.
Understanding this upfront saves review cycles.

## What Contributions Are Welcome

- **Bug fixes** with a failing test or clear reproduction steps
- **Test coverage** for untested code paths
- **Documentation improvements** (typos, clarity, missing examples)
- **Performance improvements** with benchmarks showing the delta
- **New view/component patterns** that fit the design philosophy (discuss first)

## What to Discuss First

- New class attributes, method hooks, or public API surface
- Changes to existing method signatures or default values
- New pre-built view patterns or component patterns
- Architectural changes to the state system or middleware pipeline

## Design Philosophy

CascadeUI provides building blocks, not opinions. The library ships a small
set of pre-built patterns (menus, tabs, wizards, forms, pagination,
leaderboards, role panels) because those patterns solve structural problems
that every Discord UI eventually hits. Each one exists because the alternative
is every user reimplementing the same state machine independently.

**The bar for inclusion:** a pattern must solve a *structural* problem, not
an *application* problem. A `TabLayoutView` solves the structural problem
of switching between named sections. A `TicketPanelView` encodes application
logic about what a ticket is. The first belongs in the library; the second
belongs in user code.

The test: could three unrelated bot developers (a game developer, a
moderation bot author, and a community manager) each use this pattern for
different purposes without modifying it? If so, it's structural. If it
only makes sense for one domain, it's application logic.

**Patterns should be customizable, not opinionated.** A good library pattern
provides the navigation wiring, state management, and component layout. It
does not dictate which fields appear, what the content says, or how the
domain logic works. Users customize through subclassing, builder overrides,
and class attributes - the same mechanisms every existing pattern uses.

**Avoid pattern variants.** One `TabLayoutView` covers tabs. If someone needs
tabs with a dropdown selector instead of buttons, that's a subclass in their
code, not a second `DropdownTabLayoutView` in ours.

### API Grammar

CascadeUI follows a consistent API grammar. New contributions must match it:

- **`on_<event>`** for method hooks (override for custom behavior)
- **`<event>_message`** for static text paired with an `on_<event>` hook
  (e.g. `unauthorized_message` pairs with `on_unauthorized`; the pair is
  deliberate - static fallback plus dynamic escape hatch)
- **`<name>_policy`** for behavior-enum class attributes; the `_policy`
  suffix is reserved for policy attributes and never collides visually
  with method hooks
- **`_build_<thing>()`** for internal builder methods
- **Three-tier precedence:** class attribute, method override, explicit
  argument. Each tier overrides the one above; the explicit argument
  always wins.
- **Disambiguated roots when enum values overlap.** `state_scope` and
  `instance_scope` accept the same string values but govern different
  subsystems. When adding a new scope-style attribute, use a full-word
  prefix so it reads unambiguously alongside existing scope attributes.
- **Behavior values, not message strings, in policy attributes.**
  `replace_policy = "delete"`, never `replace_policy = "You can't open
  this twice."` - user-facing text belongs on a separate `*_message`
  attribute.

When proposing new public surface, show how it fits the existing grammar. If
it introduces a new naming convention, explain why the existing conventions
don't work.

## Branch Strategy

- **`main`** is the release branch. Only merge-ready code lands here.
- **`dev`** is the integration branch. PRs target `dev`.
- Feature branches are named descriptively: `fix/session-limit-race`,
  `feat/dropdown-tabs`, `docs/persistence-guide`.
- Merges from `dev` into `main` are always true merge commits, never
  squashed. This preserves the per-PR commit history on the release
  branch so `git log` remains useful for release notes and bisection.

Fork the repo, branch from `dev`, and open your PR against `dev`.

## Development Setup

```bash
git clone https://github.com/HollowTheSilver/CascadeUI.git
cd CascadeUI
pip install -e ".[dev]"
```

### Development Bot Scaffold

A minimal bot for iterating on CascadeUI locally. Enable verbose logging and
the ViewStore dispatch tracer so interaction-routing issues surface in the
console, and install `PersistenceMiddleware` with the in-memory backend so
you can exercise persistent views without committing a SQLite file:

```python
import discord
from discord.ext import commands

from cascadeui import (
    DevToolsCog,
    InMemoryBackend,
    PersistenceMiddleware,
    setup_logging,
    setup_middleware,
)


class DevBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=discord.Intents.all())

    async def setup_hook(self):
        setup_logging(level="DEBUG")
        await setup_middleware(
            PersistenceMiddleware(backend=InMemoryBackend(), bot=self),
        )
        await self.add_cog(DevToolsCog(self))
        # Load whichever example or test cog you are iterating on
        # await self.load_extension("examples.v2_hello_world")


DevBot().run("YOUR_BOT_TOKEN")
```

`DevToolsCog` registers the `/cascadeui` hybrid command group. The owner-only
`/cascadeui inspect` subcommand opens the visual state Inspector, which
surfaces the active view registry, session members, dispatch history, and
live aggregates (total views, state size, application keys) computed via
`@computed`. The other subcommands (`views`, `exit`, `exitall`, `sessions`,
`clear`, `flush`, `purge`, `reset`) are text-level inspection and cleanup
utilities for the same data.

Swap `InMemoryBackend` for `SQLiteBackend("cascadeui.db")` when you need
persistence to survive restarts.

Pass `trace=True` to `setup_logging()` only when diagnosing a specific
interaction-routing problem (a button click that never reaches its
callback, a persistent view that isn't re-attaching on restart). The
tracer wraps discord.py's `ViewStore` and logs every dispatch attempt,
which is noisy enough to drown normal debug output. Leave it off for
general development.

## Running Tests

```bash
pytest tests/ -v
```

All PRs must pass the full test suite. New features must include tests.
Bug fixes should include a test that reproduces the bug.

## Code Style

This project uses [Black](https://github.com/psf/black) (100 char line length)
and [isort](https://pycqa.github.io/isort/) (black profile):

```bash
black --line-length 100 cascadeui/
isort --profile black --line-length 100 cascadeui/
```

## AI-Assisted Contributions

CascadeUI was built with AI tooling and the project welcomes AI-assisted
contributions. The standard is the same regardless of how code is written:
it must pass tests, match the code style, and fit the design philosophy.

**Disclose AI usage** in your PR description. This is not a gate - it's
transparency, so reviewers know to check for the usual AI failure modes
(unreviewed output, wrong abstractions, hallucinated APIs).

AI-generated PRs that ignore the contributing guidelines, dump unreviewed
output, or show no evidence of human judgment will be closed.

## Questions?

For informal questions about contributing - whether a pattern qualifies,
whether your grammar fits, whether an idea is worth a full proposal -
join the official
[support Discord server](https://discord.com/invite/9Xj68BpKRb). It is
often faster than opening a design discussion issue, and it keeps
half-formed ideas out of the issue tracker until they are ready.

## Reporting Bugs

Open a [GitHub issue](https://github.com/HollowTheSilver/CascadeUI/issues)
using the **Bug Report** template. Include:

- What you expected vs. what happened
- A minimal code snippet that reproduces the problem
- Your environment info: run `python -m cascadeui` and paste the output
- Relevant log entries from `logs/cascadeui-YYYY-MM-DD.log`

### Logs

CascadeUI is silent by default. Call `setup_logging()` in your bot's
`setup_hook` to attach handlers - this writes daily files to
`logs/cascadeui-YYYY-MM-DD.log` in the working directory and emits to the
console. The logs capture state dispatches, persistence operations, and
error details. When reporting a bug, include the relevant entries from
around the time the issue occurred.

## Pull Requests

1. Open an issue first for anything beyond a trivial fix
2. Fork the repo and branch from `dev`
3. Add tests for any new functionality
4. Run the full test suite and formatting checks
5. Open a PR with a clear description of what and why
6. Use the PR template checklist

### Commit Messages

- Use present tense: "Add feature" not "Added feature"
- Prefix with scope when applicable: `feat(views):`, `fix(persistence):`,
  `docs:`, `test:`
- Keep the first line under 72 characters
- Reference issues with `#123` shorthand
