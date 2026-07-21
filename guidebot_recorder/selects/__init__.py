"""DOM shim that makes native ``<select>`` dropdowns visible on camera.

A native select draws its option list as an operating-system popup, so it never
appears in Playwright's screencast. This package injects a DOM replacement:
``selects.py`` is the Python controller (mirroring ``overlay/``, ``slide/`` and
``chrome/``), ``selects.js`` is the widget itself.
"""

from guidebot_recorder.selects.selects import Selects

__all__ = ["Selects"]
