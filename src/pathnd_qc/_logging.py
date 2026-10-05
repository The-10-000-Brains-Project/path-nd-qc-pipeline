"""Run correlation on Path-ND QC records without changing the application's record factory."""
import contextvars
import logging

RUN_ID = contextvars.ContextVar("pathnd_run_id", default="-")
LOG_FORMAT = "%(asctime)s %(levelname)s [%(run_id)s] %(name)s: %(message)s"


class _RunFilter(logging.Filter):
    def filter(self, record):
        record.run_id = RUN_ID.get()
        return True


def get_logger(name):
    logger = logging.getLogger(name)
    if not any(isinstance(f, _RunFilter) for f in logger.filters):
        logger.addFilter(_RunFilter())
    return logger


def configure_logging(quiet=False):
    """CLI console setup; library callers configure their own handlers."""
    handler = logging.StreamHandler()
    handler.setLevel(logging.WARNING if quiet else logging.INFO)
    handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt="%H:%M:%S", defaults={"run_id": "-"}))
    logging.basicConfig(level=logging.WARNING if quiet else logging.INFO, handlers=[handler])
