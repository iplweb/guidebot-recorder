"""Timing budgets with more than one reader.

Only the shared ones live here. A budget read by a single module stays next to the
code it bounds, and that is not a stylistic choice: three of them
(``_POPUP_REQUEST_LOOKUP_TIMEOUT``, ``_POPUP_CONTENT_BOX_TIMEOUT``,
``_AUDIO_BED_CONCURRENCY``) are test seams, and a seam must be defined in the same
module whose globals its reader resolves at call time — otherwise the patch lands
on one module and the read happens in another.
"""

from __future__ import annotations

_POPUP_DETECTION_SECONDS = 1.0
_POPUP_QUIESCENCE_SECONDS = 0.1
