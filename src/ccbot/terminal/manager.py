"""The wired terminal-backend singleton.

Selects the backend named by ``config.backend`` (env ``CCBOT_BACKEND``,
default ``iterm2``) and exposes it as ``terminal_manager``. Every consumer
imports the manager from here:

    from .terminal.manager import terminal_manager

Lives in its own module (not ``terminal/__init__``) so importing backend
modules — which import ``terminal.base`` and thus trigger the package
``__init__`` — can't cycle back through manager construction.

To add a backend: create its module, register it (see ``registry``), and
import it below so its registration runs before selection.
"""

from __future__ import annotations

from ..config import config

# Import backend modules for their registration side effects. Importing a
# backend only registers a cheap factory; the selected one is instantiated
# lazily by ``registry.get`` below.
from .. import iterm2_manager as _iterm2_backend  # noqa: F401
from . import orca as _orca_backend  # noqa: F401
from .base import TerminalBackend
from .registry import get as _get_backend

terminal_manager: TerminalBackend = _get_backend(config.backend)
