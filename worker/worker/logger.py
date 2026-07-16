"""
Centralized logging setup for the worker service.

Logs to stdout in a structured, single-line format so that CloudWatch Logs
(via the ECS awslogs driver) can capture and index them cleanly.
"""

import logging
import sys

from worker.config import WorkerConfig


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)

    if logger.handlers:
        # Avoid adding duplicate handlers if get_logger() is called more than once
        return logger

    logger.setLevel(WorkerConfig.LOG_LEVEL)

    handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.propagate = False

    return logger
