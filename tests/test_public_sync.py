"""The public sync must resolve both roots and never publish server detail.

Three defects here were live, each one caught only after it shipped:
  * every top-level file (.gitignore, requirements.txt, run.py) was resolved
    against the app subdirectory, so it was never copied;
  * then resolving everything against the repo root deleted app/ and tests/;
  * deploy/ is hand-curated in the public repo and was outside the leak scan.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "sync-public.sh"


def _text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _copy_loop() -> str:
    body = _text()
    loop = body[body.index('for f in "${FILES[@]}"'):]
    return loop[:loop.index("\ndone")]


def _allowlist() -> list[str]:
    body = _text()
    block = body.split("FILES=(", 1)[1].split("\n)", 1)[0]
    return [ln.strip() for ln in block.splitlines()
            if ln.strip() and not ln.strip().startswith("#")]


def test_copy_loop_resolves_both_roots():
    # app/ and tests/ are inside the app subdirectory; the rest are at the root.
    loop = _copy_loop()
    assert '"$APP_DIR"' in loop, "the app subdirectory is not searched"
    assert '"$TOP"' in loop, "the repo root is not searched"


def test_allowlist_does_not_publish_the_runbooks():
    listed = _allowlist()
    for private in ("CLAUDE.md", "OPERATIONS.md", "SECURITY-REVIEW.md"):
        assert private not in listed, private


def test_leak_scan_covers_deploy():
    # deploy/ is left as-is in the public repo, so only this scan can catch a
    # server path a hand edit left behind.
    scan = _text()
    scan = scan[scan.index("SCAN=("):]
    scan = scan[:scan.index(")")]
    assert "deploy" in re.split(r"[\s()]+", scan), "deploy/ is not scanned"


def test_script_is_valid_bash():
    result = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_every_allowlisted_path_exists_in_the_private_tree(tmp_path):
    """A stale allowlist entry is silently skipped at runtime, so assert it here.

    This is the check that would have caught both copy-root bugs without
    publishing an empty app/ to find out.
    """
    private = SCRIPT.resolve().parents[2]
    app_dir = SCRIPT.resolve().parents[1]
    missing = [f for f in _allowlist()
               if not (app_dir / f).is_file() and not (private / f).is_file()]
    assert not missing, missing
