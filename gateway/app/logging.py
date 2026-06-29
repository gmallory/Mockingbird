"""structlog configuration for the gateway.

Call ``configure_logging()`` once at startup. Other modules then use
``structlog.get_logger(__name__)``. Keeps a console renderer for local dev;
JSON/Prometheus wiring is a later (M7) concern.
"""

import logging

import structlog


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        cache_logger_on_first_use=True,
    )
