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
from unittest.mock import MagicMock


_REPO_ROOT = Path(__file__).resolve().parent.parent
_PLUGIN_SRC = _REPO_ROOT / "Home Intelligence.indigoPlugin" / "Contents" / "Server Plugin"

# Stub indigo. Most attributes are MagicMock'd (any attribute access returns
# a mock), but PluginBase must be a real class because plugin.py subclasses
# it at module-import time (`class Plugin(indigo.PluginBase)`). Subclassing
# a MagicMock instance raises TypeError.
if "indigo" not in sys.modules:
    indigo_stub = MagicMock()
    indigo_stub.PluginBase = type(
        "PluginBase",
        (),
        {"__init__": lambda self, *a, **kw: None},
    )
    sys.modules["indigo"] = indigo_stub

if str(_PLUGIN_SRC) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_SRC))
