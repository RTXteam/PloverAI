# logging_setup.py — one shared logger, one shared rich console. every
# module that wants to log (clients, pipeline stages, runner) goes
# through the logger this module hands out. progress bars in runner.py
# share the same console object so they don't fight live log lines.

from __future__ import annotations

# logging: stdlib. we use the standard logger so the rich handler and
# the file handler can both subscribe to the same records.
import logging

# datetime: stdlib. used for the UTC run timestamp baked into every
# folder name and log file name. UTC is the modern alias for
# `timezone.utc` (added in 3.11) and matches our Python target.
from datetime import UTC, datetime

# pathlib.Path: stdlib. log file lives at
# pipeline/ploverai/logs/RUN_<run_id>/run.log — same RUN_<run_id> folder
# name as the matching outputs/RUN_<run_id>/ directory, so the two
# can be cross-referenced at a glance.
from pathlib import Path

# rich.console.Console: pretty terminal. one Console for the whole run
# means progress bars (rich.progress) and live log lines (RichHandler)
# do not interleave incorrectly.
from rich.console import Console

# rich.logging.RichHandler: standard-logging handler that renders log
# records through the Console with colours, time stamps, and tracebacks.
from rich.logging import RichHandler


# the shared Console. importable as `from .logging_setup import console`.
# using `record=True` would let us replay the session as HTML later, but
# we don't need that for the v15 study and it costs memory.
console = Console()


def utc_stamp() -> str:
    # returns "2026-04-29T07-22-13Z". used in folder names and log file
    # names — sortable, filesystem-safe (no colons), unambiguous (UTC).
    return datetime.now(tz=UTC).strftime("%Y-%m-%dT%H-%M-%SZ")


def log_path_for(logs_dir: Path, run_id: str) -> Path:
    # tiny helper so the runner can show the log file path in the
    # banner BEFORE the logger is actually wired up (e.g. on --dry-run
    # the file isn't created, but we still want to print where it
    # WOULD have lived). same convention used by setup_logger() below.
    return logs_dir / f"RUN_{run_id}" / "run.log"


def setup_logger(logs_dir: Path, run_id: str) -> tuple[logging.Logger, Path]:
    # we log to two places at once:
    #   1. the rich console (pretty, for the human watching the run live)
    #   2. a plain text file at logs/RUN_<run_id>/run.log
    #      (machine-parseable, archived, paired with outputs/RUN_<run_id>/)
    # both see the same records so a post-mortem grep on the log matches
    # exactly what scrolled past in the terminal.
    log_path = log_path_for(logs_dir, run_id)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("ploverai")
    logger.setLevel(logging.DEBUG)

    # wipe any existing handlers — calling setup_logger twice in one
    # process (e.g. tests, repeated --smoke runs) shouldn't double output.
    logger.handlers.clear()
    logger.propagate = False

    rich_h = RichHandler(
        console=console,
        rich_tracebacks=True,
        show_path=False,
        show_time=True,
        markup=True,
        log_time_format="[%H:%M:%S]",
    )
    rich_h.setLevel(logging.INFO)

    file_h = logging.FileHandler(log_path, encoding="utf-8")
    file_h.setLevel(logging.DEBUG)
    # tab-separated for easy parsing later. timestamps in UTC match the
    # run folder name so a log line and a results folder line up.
    file_h.setFormatter(logging.Formatter(
        "%(asctime)sZ\t%(levelname)s\t%(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    ))

    logger.addHandler(rich_h)
    logger.addHandler(file_h)
    return logger, log_path
