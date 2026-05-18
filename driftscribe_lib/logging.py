"""Structured logging setup — Phase 11.2 stub.

Phase 15 will swap this for proper JSON-formatted logs that Cloud Logging
parses into structured payloads (with trace_id, decision_id, worker_name,
etc.). For now, configure the root logger with a basic format so workers
have a single entry point to call during startup.
"""
import logging
import os


def setup(service_name: str, level: int | str = "INFO") -> logging.Logger:
    """Configure the root logger and return one named for the service.

    Idempotent: calling twice doesn't double-attach handlers.
    """
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(
            level=os.environ.get("LOG_LEVEL", level),
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )
    return logging.getLogger(service_name)
