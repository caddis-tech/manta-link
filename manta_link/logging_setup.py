"""Log configuration.

One handler, one format, unbuffered to stdout so `docker logs -f` shows the
process working rather than nothing for hours.

Note for the step that adds worker threads: logging.Handler.handle wraps emit in
a per-handler lock, and StreamHandler.emit both writes and flushes inside it. So
the moment more than one thread logs, the reader can block behind a worker
that is stuck writing to a full or slow stdout pipe. The fix is a QueueHandler
over a bounded drop-on-full queue with its own drain thread, and it belongs with
the workers rather than here, where a single thread cannot contend with itself.
"""

import logging


def configure(level: int = logging.INFO) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
