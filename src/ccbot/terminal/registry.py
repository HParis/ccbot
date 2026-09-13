"""Backend registry: name -> lazy factory -> memoized singleton.

Adding a terminal backend is "write a file + register it":

    from .registry import register

    @register("orca")
    def _make_orca() -> TerminalBackend:
        return OrcaManager()

The selected backend is instantiated once, on first ``get(name)``; other
registered backends are never instantiated. Factories must be cheap and
side-effect-free (no connections, no subprocesses) — defer real I/O to
the backend's methods.
"""

from __future__ import annotations

from collections.abc import Callable

from .base import TerminalBackend

BackendFactory = Callable[[], TerminalBackend]

_factories: dict[str, BackendFactory] = {}
_instances: dict[str, TerminalBackend] = {}


def register(name: str) -> Callable[[BackendFactory], BackendFactory]:
    """Decorator: register ``factory`` under ``name``."""

    def decorator(factory: BackendFactory) -> BackendFactory:
        _factories[name] = factory
        return factory

    return decorator


def get(name: str) -> TerminalBackend:
    """Return the singleton backend for ``name``, instantiating on first use."""
    if name not in _instances:
        try:
            factory = _factories[name]
        except KeyError:
            raise KeyError(
                f"Unknown terminal backend {name!r}; "
                f"available: {', '.join(available()) or '(none)'}"
            ) from None
        _instances[name] = factory()
    return _instances[name]


def available() -> list[str]:
    """Sorted names of all registered backends."""
    return sorted(_factories)
