from .extension import ExtensionStrategy
from .filename import FilenameStrategy
from .manpage import ManpageStrategy
from .modeline import ModeLineStrategy
from .shebang import ShebangStrategy
from .xml import XMLStrategy

__all__ = [
    "ModeLineStrategy",
    "ManpageStrategy",
    "ExtensionStrategy",
    "XMLStrategy",
    "FilenameStrategy",
    "ShebangStrategy",
]
