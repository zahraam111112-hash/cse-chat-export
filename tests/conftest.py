"""Point the app at a throwaway database before anything imports store.

`app.main` runs `store.init()` at import time, so the env var has to be set
first — otherwise a test run would open (and create) the real data/app.db.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["CSE_DB"] = os.path.join(tempfile.mkdtemp(prefix="cse-tests-"), "app.db")

# VACUUM would rewrite the throwaway database on every delete for no benefit.
os.environ["CSE_VACUUM"] = "0"
