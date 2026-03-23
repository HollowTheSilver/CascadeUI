# Installation

## Requirements

- Python 3.10 or higher
- discord.py 2.7 or higher

## From PyPI

```bash
pip install cascadeui

# With optional backends
pip install cascadeui[sqlite]
pip install cascadeui[redis]
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

This installs additional dependencies: `pytest`, `pytest-asyncio`, `black`, and `isort`.

## Verify Installation

```python
import cascadeui
print(cascadeui.__version__)  # Should print the version
```
