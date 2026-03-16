# Installation

## Requirements

- Python 3.10 or higher
- discord.py 2.1 or higher

## From Source

CascadeUI is not yet published on PyPI. Install directly from the repository:

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

## From PyPI (Coming Soon)

```bash
pip install cascadeui
```

## Verify Installation

```python
import cascadeui
print(cascadeui.__version__)  # Should print the version
```
