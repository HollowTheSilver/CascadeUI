# Examples

Working examples are in the [`examples/`](https://github.com/HollowTheSilver/CascadeUI/tree/main/examples) directory. Each is a discord.py cog that can be loaded into any bot.

## counter.py

Basic stateful counter with increment, decrement, and reset buttons. Demonstrates `StatefulView`, `StatefulButton`, and custom reducers.

## themed_form.py

Theme switching, component wrappers (loading state, confirmation, cooldowns), pagination, and form views. Shows how to use the theming system and behavioral wrappers together.

## persistent_counter.py

Data persistence to disk using `setup_persistence()` and `state_key`. The counter value survives across command re-invocations. Demonstrates Pattern 1 persistence (data only, re-invoke to restore).

## persistent_dashboard.py

A role selector panel using `PersistentView` that stays interactive across bot restarts. Demonstrates Pattern 2 persistence (view + data, survives restarts without user action).

## Running the Examples

1. Create a bot on the [Discord Developer Portal](https://discord.com/developers/applications)
2. Install CascadeUI: `pip install -e .`
3. Load the example cog in your bot:

```python
async def setup_hook(self):
    await self.load_extension("examples.counter")

    # For persistence examples, also call setup_persistence:
    from cascadeui import setup_persistence
    await setup_persistence(self, file_path="bot_state.json")
```

4. Run your bot and use the slash commands
