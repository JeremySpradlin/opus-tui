"""Entry point for opus-tui.

Sets up file-only logging, runs the gh/~/Projects preflight, and launches
ProjectsApp. The actual TUI lives in app.py; data fetching in git_ops.py;
UI components and render helpers in widgets.py.

Theming follows the active Omarchy theme (theme.py) and re-applies live
within ~2 seconds when the user runs `omarchy theme set`.
"""

import logging
from datetime import datetime
from pathlib import Path

from app import ProjectsApp
from git_ops import preflight

LOG_DIR = Path(__file__).resolve().parent / "logs"
LOG_KEEP = 20  # keep this many most-recent log files

logger = logging.getLogger(__name__)


def setup_logging() -> Path:
    """Configure file-only logging at DEBUG level.

    One log file per app session, named opus-tui-YYYYMMDD-HHMMSS.log under
    ./logs/. Keeps only the most recent LOG_KEEP files. Returns the path of
    the freshly opened log file.
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / f"opus-tui-{datetime.now():%Y%m%d-%H%M%S}.log"
    logging.basicConfig(
        filename=str(log_file),
        filemode="w",
        level=logging.DEBUG,
        format="%(asctime)s.%(msecs)03d %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # Quiet down noisy third-party loggers
    logging.getLogger("markdown_it").setLevel(logging.WARNING)
    _prune_old_logs()
    return log_file


def _prune_old_logs(keep: int = LOG_KEEP) -> None:
    try:
        existing = sorted(LOG_DIR.glob("opus-tui-*.log"), reverse=True)
        for old in existing[keep:]:
            try:
                old.unlink()
            except OSError:
                pass
    except OSError:
        pass


def main() -> None:
    log_file = setup_logging()
    logger.info("opus-tui starting; log_file=%s", log_file)
    try:
        preflight()
        ProjectsApp().run()
    except Exception:
        logger.exception("Fatal error in main")
        raise
    finally:
        logger.info("opus-tui exiting")
        logging.shutdown()


if __name__ == "__main__":
    main()
