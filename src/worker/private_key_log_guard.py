"""Keeping the node's private key out of the meshcore library's log records.

The library logs at debug level every frame it sends and receives and every event it dispatches,
so an exported key's reply and an import's command frame would appear there in full. While such
a frame may cross the link, a filter on the library's logger drops its records below WARNING; the
records above carry no frame contents. The gateway holds the guard from before it sends the frame
until the command's own reply has arrived. After any other outcome (a lost reply that may still
come, or a late reply to an earlier command that the library took for this one's) the frame with
the key may still be on its way, and the guard stays held until the link is torn down.
"""

import logging
from typing import override

LIBRARY_LOGGER_NAME = "meshcore"


class LibraryFrameLogGuard(logging.Filter):
    def __init__(self) -> None:
        super().__init__()
        self._holder_count = 0

    @property
    def is_held(self) -> bool:
        return self._holder_count > 0

    def hold(self) -> None:
        self._holder_count += 1

    def release(self) -> None:
        self._holder_count = max(0, self._holder_count - 1)

    @override
    def filter(self, record: logging.LogRecord) -> bool:
        return not self.is_held or record.levelno >= logging.WARNING


def install_library_frame_log_guard() -> LibraryFrameLogGuard:
    """The guard on the library's logger, added the first time; every gateway shares it."""
    library_logger = logging.getLogger(LIBRARY_LOGGER_NAME)
    for installed_filter in library_logger.filters:
        if isinstance(installed_filter, LibraryFrameLogGuard):
            return installed_filter
    library_frame_log_guard = LibraryFrameLogGuard()
    library_logger.addFilter(library_frame_log_guard)
    return library_frame_log_guard
