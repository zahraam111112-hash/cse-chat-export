"""Bot entry point: `python run_bot.py` starts the long-polling loop.

`python -I -m app.bot` cannot work — `-I` implies `-E`/`-s`, so neither the
working directory nor `PYTHONPATH` reaches `sys.path`.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from app.bot import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
