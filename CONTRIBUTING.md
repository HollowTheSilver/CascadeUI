# Contributing to CascadeUI

Thanks for your interest in contributing! Here's how to get started.

## Reporting Bugs

Open a [GitHub issue](https://github.com/HollowTheSilver/CascadeUI/issues) with:

- What you expected vs what happened
- A minimal code snippet that reproduces the problem
- Your Python version and discord.py version
- Relevant log output (see below)

### Logs

CascadeUI writes daily log files to `logs/cascadeui-YYYY-MM-DD.log` in your bot's
working directory. These logs capture state dispatches, persistence operations, and
error details that are often essential for diagnosing issues.

When reporting a bug, include the relevant log entries from around the time the issue
occurred. For short snippets, paste them directly in the issue. For longer logs,
upload them as a [GitHub Gist](https://gist.github.com/) and link it in your report.

## Suggesting Features

Open an issue describing the use case and how you'd expect the API to look.

## Development Setup

```bash
git clone https://github.com/HollowTheSilver/CascadeUI.git
cd CascadeUI
pip install -e ".[dev]"
```

## Running Tests

```bash
pytest tests/ -v
```

## Code Style

This project uses [Black](https://github.com/psf/black) (100 char line length) and
[isort](https://pycqa.github.io/isort/) (black profile):

```bash
black --line-length 100 cascadeui/
isort --profile black --line-length 100 cascadeui/
```

## Pull Requests

1. Fork the repo and create your branch from `dev`
2. Add tests for any new functionality
3. Make sure all tests pass and formatting is clean
4. Open a PR with a clear description of the change
