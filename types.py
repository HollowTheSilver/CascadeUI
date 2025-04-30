
# // ========================================( Modules )======================================== // #


from typing import TypeVar, ForwardRef


# // ========================================( Script )======================================== // #


# Forward references
UIManagerRef = ForwardRef('UIManager')
UISessionRef = ForwardRef('UISession')
CascadeViewRef = ForwardRef('CascadeView')

# Type variables
UIManagerObj = TypeVar('UIManagerObj', bound=UIManagerRef)
UISessionObj = TypeVar('UISessionObj', bound=UISessionRef)
CascadeViewObj = TypeVar('CascadeViewObj', bound=CascadeViewRef)
