"""Console logging with millisecond precision.

During the snipe window every millisecond matters for the post-mortem, so
all log lines carry HH:MM:SS.mmm timestamps.
"""

import logging
import sys

FORMAT = "%(asctime)s.%(msecs)03d %(levelname)-5s %(message)s"
DATEFMT = "%H:%M:%S"


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(FORMAT, datefmt=DATEFMT))
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers[:] = [handler]
    # httpx/httpcore are chatty at DEBUG; keep them at INFO even when verbose
    logging.getLogger("httpx").setLevel(logging.INFO if verbose else logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
