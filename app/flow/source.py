"""Old import path for app.sources.flow.protocol. It is the same module object, so patches made here reach it."""

import sys

from app.sources.flow import protocol as _module

sys.modules[__name__] = _module
