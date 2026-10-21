import re
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("page", ["pool.html", "users.html", "register.html"])
def test_inline_account_page_scripts_parse(page):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for JavaScript syntax checks")
    root = Path(__file__).resolve().parents[1]
    html = (root / "galgame_web" / "galgame" / page).read_text(encoding="utf-8")
    scripts = re.findall(r"<script>(.*?)</script>", html, re.DOTALL)
    assert scripts
    for script in scripts:
        result = subprocess.run(
            [node, "--check"], input=script, capture_output=True,
            text=True, encoding="utf-8", check=False,
        )
        assert result.returncode == 0, result.stderr
