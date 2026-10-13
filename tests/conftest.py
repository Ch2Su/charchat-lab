"""테스트 공통 설정. app 모듈을 불러오기 전에 환경 변수를 고정한다."""
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="charchat-test-")
os.environ.update({
    "DATA_DIR": _TMP,
    "PROVIDER": "mock",
    "MOCK_DELAY_SCALE": "0",
    "MOCK_SEED": "7",
})
for key in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENAI_COMPAT_BASE_URL"):
    os.environ.pop(key, None)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import config  # noqa: E402
from app.main import create_app  # noqa: E402


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path / "test.sqlite3")
    with TestClient(app) as test_client:
        yield test_client
    app.state.db.close()


def parse_sse(text: str) -> list[tuple[str, dict]]:
    events = []
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        name, data = "message", ""
        for line in block.splitlines():
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data += line[5:].strip()
        events.append((name, json.loads(data) if data else {}))
    return events


def post_sse(client, url: str, body: dict) -> list[tuple[str, dict]]:
    response = client.post(url, json=body)
    assert response.status_code == 200, response.text
    return parse_sse(response.text)


def event(events, name):
    return [data for ev, data in events if ev == name]


@pytest.fixture
def haram_id(client):
    return next(c["id"] for c in client.get("/api/characters").json() if c["name"] == "하람")


@pytest.fixture
def echo_id(client):
    return next(c["id"] for c in client.get("/api/characters").json() if c["name"] == "에코")


def load_card(name: str) -> dict:
    return json.loads((config.CHARACTERS_DIR / name).read_text(encoding="utf-8"))
