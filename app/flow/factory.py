"""Old import path for app.sources.flow.factory. It is the same module object, so patches made here reach it."""

import sys

from app.sources.flow import factory as _module

sys.modules[__name__] = _module
