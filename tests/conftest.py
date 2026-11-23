import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# tests never call the AI API: the offline classifier is deterministic
os.environ["ANTHROPIC_API_KEY"] = ""
os.environ["ANTHROPIC_AUTH_TOKEN"] = ""

from nagarsetu.service import Service  # noqa: E402
from nagarsetu.store import Store  # noqa: E402


@pytest.fixture
def svc():
    return Service(Store(":memory:"))
