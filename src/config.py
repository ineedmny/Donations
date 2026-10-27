"""项目路径配置。"""

from __future__ import annotations

import os
from pathlib import Path

from market_profile import get_profile, resolve_market


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DEBUG_DIR = PROJECT_ROOT / "debug"

MARKET = resolve_market()
PROFILE = get_profile(MARKET)

DEFAULT_DB = Path(
    os.environ.get("QUOTES_DB", str(DATA_DIR / PROFILE.db_filename))
).expanduser()
PROMPT_DEBUG_PATH = Path(
    os.environ.get(
        "PROMPT_DEBUG_PATH",
        str(DEBUG_DIR / PROFILE.prompt_debug_filename),
    )
).expanduser()

DB_PATHS = {
    "CN": Path(
        os.environ.get("CN_QUOTES_DB", str(DATA_DIR / "quotes.db"))
    ).expanduser(),
    "US": Path(
        os.environ.get("US_QUOTES_DB", str(DATA_DIR / "us_quotes.db"))
    ).expanduser(),
}
DB_PATHS[MARKET] = DEFAULT_DB

PROMPT_DEBUG_PATHS = {
    "CN": DEBUG_DIR / "prompt_debug.txt",
    "US": DEBUG_DIR / "us_prompt_debug.txt",
}
PROMPT_DEBUG_PATHS[MARKET] = PROMPT_DEBUG_PATH
