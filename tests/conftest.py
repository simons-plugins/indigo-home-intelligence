"""Pytest conftest for the Home Intelligence plugin tests.

The plugin's source is under `Home Intelligence.indigoPlugin/Contents/Server Plugin/`
and uses bare imports (`import indigo`, `from history_db import HistoryDB`, etc.).
To import those modules from pytest outside of a running Indigo server, we:

1. Stub the `indigo` module so `import indigo` succeeds without Indigo installed.
2. Add the plugin's source directory to sys.path so its bare imports resolve.

The tests in this suite only touch pure helper functions — they don't exercise
any real Indigo surface (no `indigo.devices`, `indigo.variables`, etc.). The
MagicMock stub is just enough to let the modules import; any test that
accidentally reached into Indigo would fail loudly instead of silently
hitting a mocked value.
"""

import sys
from pathlib import Path
from types import ModuleType


_REPO_ROOT = Path(__file__).resolve().parent.parent
_PLUGIN_SRC = _REPO_ROOT / "Home Intelligence.indigoPlugin" / "Contents" / "Server Plugin"


class _StrictIndigoStub(ModuleType):
    """A stand-in for the `indigo` module that raises on any attribute
    access outside the explicit allowlist. The Tier-A test suite covers
    pure helpers only — no Indigo surface should be reachable. A lax
    MagicMock would make an accidental `indigo.devices[...]` reach pass
    silently, which defeats the point. This class fails loudly instead.
    """

    _ALLOWED = ("PluginBase", "Dict")

    def __getattr__(self, name):
        if name in type(self)._ALLOWED:
            return super().__getattribute__(name)
        raise AttributeError(
            f"Tier-A tests must not access indigo.{name}. "
            f"If the helper genuinely needs it, it isn't pure — move it "
            f"or add {name} to _StrictIndigoStub._ALLOWED."
        )


if "indigo" not in sys.modules:
    stub = _StrictIndigoStub("indigo")

    # PluginBase must be a real class — plugin.py subclasses it at
    # import time (`class Plugin(indigo.PluginBase)`).
    class _PluginBase:
        def __init__(self, *args, **kwargs):
            pass

    stub.PluginBase = _PluginBase

    # indigo.Dict is used in places like Actions.xml body parsing.
    # A plain dict subclass is behaviour-equivalent for the tests
    # that never run handle_feedback directly.
    stub.Dict = dict

    sys.modules["indigo"] = stub

if str(_PLUGIN_SRC) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_SRC))
