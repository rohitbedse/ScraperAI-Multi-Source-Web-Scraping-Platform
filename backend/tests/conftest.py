import os
import sys
import tempfile
import time
from pathlib import Path

# isolated data dir BEFORE the app is imported (worker subprocesses inherit it)
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="scraper-tests-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="session")
def client():
    from app.main import app
    with TestClient(app) as c:
        yield c


def wait_done(client, job_id, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        j = client.get(f"/jobs/{job_id}").json()
        if j["status"] in ("completed", "partially_completed", "failed", "cancelled"):
            return j
        time.sleep(0.2)
    raise AssertionError("job did not finish")
