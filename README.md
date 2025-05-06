# CascadeUI

## Component System

CascadeUI provides a powerful component composition system that makes it easy to create complex UI patterns:

### Composite Components

```python
from cascadeui import CompositeComponent, StatefulButton

# Create a custom component
class ActionBar(CompositeComponent):
    def __init__(self, on_save=None, on_cancel=None):
        super().__init__()
        
        # Add child components
        self.add_component(StatefulButton("Save", callback=on_save))
        self.add_component(StatefulButton("Cancel", callback=on_cancel))
    
    # Use in a view
    # action_bar.add_to_view(my_view)