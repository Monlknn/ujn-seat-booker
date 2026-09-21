"""日志模块：同时输出到控制台与 logs/ 目录下的按日日志文件。"""
from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_LOG_DIR = _PROJECT_ROOT / "logs"
_LOG_DIR.mkdir(exist_ok=True)

_FMT = "%(asctime)s [%(levelname)s] %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"

_today = datetime.now().strftime("%Y-%m-%d")
_log_file = _LOG_DIR / f"booker_{_today}.log"

logger = logging.getLogger("ujn_seat")
logger.setLevel(logging.DEBUG)
logger.handlers.clear()

_console = logging.StreamHandler(sys.stdout)
_console.setLevel(logging.INFO)
_console.setFormatter(logging.Formatter(_FMT, _DATEFMT))
logger.addHandler(_console)

_file = logging.FileHandler(_log_file, encoding="utf-8")
_file.setLevel(logging.DEBUG)
_file.setFormatter(logging.Formatter(_FMT, _DATEFMT))
logger.addHandler(_file)


def set_debug(debug: bool) -> None:
    """调试模式：把控制台也设为 DEBUG 级别。"""
    if debug:
        _console.setLevel(logging.DEBUG)


def get_log_path() -> Path:
    return _log_file
