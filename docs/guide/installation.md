# Installation

## Requirements

- Python 3.10 or higher
- discord.py 2.7 or higher

## From PyPI

```bash
pip install pycascadeui

# With the SQLite persistence backend
pip install pycascadeui[sqlite]

# With the PostgreSQL persistence backend
pip install pycascadeui[postgres]
```

### From Source

```bash
git clone https://github.com/HollowTheSilver/CascadeUI.git
cd CascadeUI
pip install -e .
```

### Development Install

If you want to run tests or contribute:

```bash
pip install -e ".[dev]"
```

This installs additional dependencies: `pytest`, `pytest-asyncio`, `black`, `isort`, and `testcontainers[postgres]` (for the real-database backend tests).

## Verify Installation

```python
import cascadeui
print(cascadeui.__version__)  # Should print the version
```
