import logging
from typing import Any

from app.core.redaction import redact

logger = logging.getLogger(__name__)


def report_exception(exception: BaseException, **context: Any) -> None:
    """Stable integration point for a future error-monitoring provider."""
    logger.error(
        "Unhandled application exception",
        extra={"exception_type": type(exception).__name__, **redact(context)},
        exc_info=exception,
    )
