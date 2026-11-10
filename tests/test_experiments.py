"""대본 재생 실험 실행기(app/experiments.py)와 /api/experiments를 확인한다."""
import asyncio
import csv
import io
import time

import pytest
from fastapi.testclient import TestClient

from app import config, memory, pricing
from app.chat_service import ChatService
from app.db import DB
from app.main import create_app
from app.providers import close_all, get_provider, reset_mock
from tests.test_api import new_chat

INPUTS = ["안녕하세요", "책 구경해도 돼요?", "이 서점은 언제 생겼어요?", "할머니 얘기도 듣고 싶어요",
          "저 고양이 이름이 뭐예요?", "2층에는 뭐가 있어요?"]


def body(character_id, **overrides) -> dict:
    return {
        "name": "창 전략 비교",
        "character_id": character_id,
        "persona_name": "민준",
        "inputs": INPUTS,
        "variants": [
            {"label": "full", "settings": {"window_strategy": "full", "input_token_budget": 1200}},
            {"label": "anchored", "settings": {"window_strategy": "anchored", "input_token_budget": 1200}},
        ],
        "reset_mock": True,
        "seed": 7,
        "wait_summary": True,
        "cooldown_seconds": 0,
        **overrides,
    }


def start(client, payload) -> int:
    response = client.post("/api/experiments", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["id"]


def wait_finished(client, experiment_id, timeout=20.0) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        data = client.get(f"/api/experiments/{experiment_id}").json()
        if data["status"] != "running":
            return data
        assert time.monotonic() < deadline, data
        time.sleep(0.02)


def answered(client, chat_id) -> list[dict]:
    view = client.get(f"/api/chats/{chat_id}").json()
    return [m for m in view["messages"] if m["role"] == "assistant" and m["meta"].get("usage")]


def test_variants_replay_the_script(client, echo_id):
    saved = client.get("/api/config").json()["settings"]
    data = wait_finished(client, start(client, body(echo_id)))
    assert data["status"] == "done" and data["error"] is None and data["finished_at"]
    assert [(r["label"], r["status"], r["turns_done"]) for r in data["runs"]] == [("full", "done", 6), ("anchored", "done", 6)]
    assert data["progress"] == {"run": 2, "runs": 2, "variant": 2, "variants": 2, "repeat": 1, "repeats": 1,
                                "turn": 6, "turns": 6}
    assert [r["repeat"] for r in data["runs"]] == [1, 1] and "spread" not in data["stats"]  # 반복하지 않으면 편차가 없다
    assert len({r["chat_id"] for r in data["runs"]}) == 2
    for run in data["runs"]:
        view = client.get(f"/api/chats/{run['chat_id']}").json()
        assert view["chat"]["title"] == f"[실험] 창 전략 비교 · {run['label']}" and view["persona_name"] == "민준"
        assert sum(m["role"] == "user" for m in view["messages"]) == 6
        assert {m["meta"]["run"]["label"] for m in answered(client, run["chat_id"])} == {run["label"]}
    stats = data["stats"]
    assert [(g["label"], g["strategy"], g["turns"]) for g in stats["groups"]] == [("full", "full", 6), ("anchored", "anchored", 6)]
    assert stats["varying"] == ["label", "strategy"]
    assert client.get("/api/config").json()["settings"] == saved  # 변형 설정은 그 실험에만 쓰인다


def test_reset_mock_makes_variants_reproducible(client, haram_id):
    same = [{"label": "A", "settings": {}}, {"label": "B", "settings": {}}]
    data = wait_finished(client, start(client, body(haram_id, variants=same)))
    a, b = (answered(client, run["chat_id"]) for run in data["runs"])
    assert len(a) == len(b) == 6
    assert [m["content"] for m in a] == [m["content"] for m in b]
    reads = [m["meta"]["usage"]["cache_read"] for m in a]
    assert reads == [m["meta"]["usage"]["cache_read"] for m in b] and reads[0] == 0 and any(reads)

    # 비우지 않으면 뒤 변형이 앞 변형의 캐시를 읽어서 첫 턴부터 달라진다
    client.post("/api/mock/cache/clear")
    data = wait_finished(client, start(client, body(haram_id, variants=same, reset_mock=False)))
    a, b = (answered(client, run["chat_id"]) for run in data["runs"])
    assert a[0]["meta"]["usage"]["cache_read"] == 0 < b[0]["meta"]["usage"]["cache_read"]


@pytest.mark.parametrize("change, message", [
    ({"inputs": []}, "입력 대본"),
    ({"inputs": ["안녕", "   "]}, "2번째 입력"),
    ({"inputs": ["가" * 2001]}, "1번째 입력"),
    ({"variants": [{"label": f"v{i}", "settings": {}} for i in range(7)]}, "변형(variants)"),
    ({"variants": [{"label": "같음", "settings": {}}, {"label": "같음", "settings": {}}]}, "겹쳐요"),
    ({"variants": [{"label": "", "settings": {}}]}, "라벨"),
    ({"variants": [{"label": "a", "settings": {"nope": 1}}]}, "알 수 없는 설정"),
    ({"variants": [{"label": "a", "settings": {"run_label": "x"}}]}, "run_label"),
    ({"variants": [{"label": "a", "settings": {"window_strategy": "nope"}}]}, "window_strategy"),
    ({"variants": [{"label": "a", "settings": {"provider": "anthropic"}}]}, "제공사"),
    ({"variants": [{"label": "a", "settings": ["full"]}]}, "JSON 객체"),
    ({"cooldown_seconds": 601}, "대기"),
    ({"seed": "7"}, "seed"),
    ({"repeats": 0}, "반복"),
    ({"repeats": 6}, "반복"),
    ({"repeats": "2"}, "반복"),
    ({"variants": [{"label": "a", "settings": {"provider_options": {"mock": {}}}}]}, "제공사"),
    ({"variants": [{"label": "a", "settings": {"prompt_overrides": {"final_user": "입력 없음"}}}]}, "자리표시자"),
    ({"character_id": 9999}, "캐릭터"),
])
def test_invalid_requests_are_rejected(client, haram_id, change, message, monkeypatch):
    # .env에 Anthropic 키가 있으면 'anthropic 변형'이 통과해 실제 API를 부르게 되므로 키를 비워 둔다
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", None)
    response = client.post("/api/experiments", json={**body(haram_id), **change})
    assert response.status_code == 400, response.text
    assert message in response.json()["detail"]
    assert client.get("/api/experiments").json() == []


def slow_down_send(monkeypatch):
    """실험의 턴마다 0.05초씩 기다리게 해서 실행 중인 동안 다른 요청을 끼워 넣을 수 있게 한다. 원래 send를 돌려준다."""
    original = ChatService.send

    def slow_send(self, chat_id, content, settings=None):
        stream = original(self, chat_id, content, settings=settings)

        async def slowed():
            await asyncio.sleep(0.05)
            async for chunk in stream:
                yield chunk
        return slowed()

    monkeypatch.setattr(ChatService, "send", slow_send)
    return original


def test_second_start_conflicts_and_cancel_stops(client, echo_id, monkeypatch):
    original = slow_down_send(monkeypatch)
    experiment_id = start(client, body(echo_id, inputs=[f"질문 {i}" for i in range(100)]))
    assert client.post("/api/experiments", json=body(echo_id)).status_code == 409
    assert client.delete(f"/api/experiments/{experiment_id}").status_code == 409  # 실행 중에는 못 지운다
    assert client.post(f"/api/experiments/{experiment_id}/cancel").json() == {"ok": True}
    data = wait_finished(client, experiment_id)
    assert data["status"] == "cancelled" and data["finished_at"]
    assert [r["status"] for r in data["runs"]] == ["cancelled", "cancelled"]
    assert data["runs"][0]["turns_done"] < 100 and data["runs"][1]["chat_id"] is None
    assert client.post(f"/api/experiments/{experiment_id}/cancel").status_code == 409
    monkeypatch.setattr(ChatService, "send", original)
    assert wait_finished(client, start(client, body(echo_id, inputs=INPUTS[:1])))["status"] == "done"


def test_mock_chats_wait_while_a_mock_experiment_runs(client, echo_id, monkeypatch):
    slow_down_send(monkeypatch)
    chat_id = new_chat(client, echo_id)["chat"]["id"]
    experiment_id = start(client, body(echo_id, inputs=[f"질문 {i}" for i in range(100)]))
    # mock의 캐시와 난수는 모든 대화가 함께 쓰므로, 실험이 도는 동안은 다른 대화를 보내지 못한다
    response = client.post(f"/api/chats/{chat_id}/messages", json={"content": "끼어들기"})
    assert response.status_code == 409 and "실험이 실행 중" in response.json()["detail"]
    assert client.post(f"/api/experiments/{experiment_id}/cancel").json() == {"ok": True}
    wait_finished(client, experiment_id)
    monkeypatch.undo()
    assert client.post(f"/api/chats/{chat_id}/messages", json={"content": "이제 보내요"}).status_code == 200


def test_estimate_counts_paid_calls_and_warns_about_shared_cache(client, haram_id, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "PRICING_OVERRIDE_PATH", tmp_path / "none.json")  # 개발자의 pricing.json에 기대지 않는다
    monkeypatch.setattr(config, "OPENAI_COMPAT_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setitem(pricing.BUILTIN, "openai_compat:fake-model", {"input": 1.0, "output": 2.0})
    real = {"provider": "openai_compat", "models": {"openai_compat": "fake-model"}, "input_token_budget": 1000,
            "max_output_tokens": 500, "moderation_backend": "llm"}
    payload = body(haram_id, variants=[
        {"label": "모의", "settings": {}},
        {"label": "실제A", "settings": real},
        {"label": "실제B", "settings": {**real, "moderation_backend": "rules"}},
    ])
    response = client.post("/api/experiments/estimate", json=payload)
    assert response.status_code == 200, response.text
    data = response.json()
    rows = {row["label"]: row for row in data["variants"]}
    assert rows["실제A"]["chat_calls"] == 6 and rows["실제A"]["judge_calls"] == 12 and rows["실제B"]["judge_calls"] == 0
    assert rows["실제A"]["max_usd"] == pytest.approx((6 * 1000 * 1.0 + 6 * 500 * 2.0) / 1_000_000)
    assert data["paid_calls"] == 6 + 12 + 6  # mock 변형은 세지 않는다
    assert data["paid_max_usd"] == pytest.approx(2 * rows["실제A"]["max_usd"]) and data["unpriced"] == []
    assert data["cache_overlap"] is True  # 실제 제공사 변형 둘이 기다림 없이 이어지면 캐시가 섞인다

    assert client.post("/api/experiments/estimate", json={**payload, "cooldown_seconds": 300}).json()["cache_overlap"] is False
    other = {**real, "models": {"openai_compat": "other-model"}}  # 캐시는 모델마다 따로라서 모델이 다르면 섞이지 않는다
    different = {**payload, "variants": [{"label": "실제A", "settings": real}, {"label": "실제C", "settings": other}]}
    assert client.post("/api/experiments/estimate", json=different).json()["cache_overlap"] is False
    unpriced = {**payload, "variants": [{"label": "값없음", "settings": {**real, "models": {"openai_compat": "no-price"}}}]}
    assert client.post("/api/experiments/estimate", json=unpriced).json()["unpriced"] == ["값없음"]
    assert client.post("/api/experiments/estimate", json={**payload, "inputs": []}).status_code == 400  # start와 같은 검증
    assert client.get("/api/experiments").json() == []  # 어림만 하고 실험을 만들지 않는다


def test_blocked_input_is_counted_and_skipped(client, echo_id):
    payload = body(echo_id, inputs=["안녕", "이건 [[검수테스트]] 입력이에요", "다음 질문"],
                   variants=[{"label": "규칙 검수", "settings": {}}])
    data = wait_finished(client, start(client, payload))
    run = data["runs"][0]
    assert data["status"] == "done" and run["status"] == "done"
    assert run["blocked"] == 1 and run["turns_done"] == 3
    assert data["stats"]["groups"][0]["turns"] == 2


def test_provider_error_stops_only_that_variant(client, echo_id):
    payload = body(echo_id, inputs=["안녕", "/error 끊겨 봐", "세 번째"], variants=[
        {"label": "바로 표시", "settings": {"output_buffer": "none"}},     # 받은 만큼 오류 답으로 저장된다
        {"label": "문장 단위", "settings": {"output_buffer": "sentence"}},  # 보여 준 글이 없어 답이 저장되지 않는다
    ])
    data = wait_finished(client, start(client, payload))
    assert data["status"] == "done"
    for run in data["runs"]:
        assert run["status"] == "error" and run["turns_done"] == 1 and "mock 오류" in run["error"]
    assert data["progress"]["turn"] == 2


def test_export_csv_and_delete_keep_chats(client, echo_id):
    data = wait_finished(client, start(client, body(echo_id, inputs=INPUTS[:2])))
    response = client.get(f"/api/experiments/{data['id']}/export.csv")
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/csv")
    lines = response.content.decode("utf-8-sig").splitlines()
    assert lines[0].startswith("label,provider,model,strategy,") and len(lines) == 3
    assert lines[1].startswith("full,mock,mock-1,full,")
    listed = client.get("/api/experiments").json()
    assert listed[0]["spec"]["input_count"] == 2 and "inputs" not in listed[0]["spec"]
    assert listed[0]["character_name"] == "에코"
    assert client.delete(f"/api/experiments/{data['id']}").json() == {"ok": True}
    assert client.get(f"/api/experiments/{data['id']}").status_code == 404
    for run in data["runs"]:  # 만들어진 대화는 남는다
        assert client.get(f"/api/chats/{run['chat_id']}").status_code == 200


def test_running_rows_become_interrupted_on_startup(tmp_path):
    path = tmp_path / "restart.sqlite3"
    db = DB(path)
    assert db.one("PRAGMA user_version")["user_version"] == 2  # 마이그레이션 버전 2: experiments 표
    experiment_id = db.create_experiment("끊긴 실험", None, {"inputs": ["안녕"], "variants": []})
    db.close()
    app = create_app(path)
    with TestClient(app) as client:
        data = client.get(f"/api/experiments/{experiment_id}").json()
    app.state.db.close()
    assert data["status"] == "interrupted" and data["finished_at"]
    assert data["stats"] == {"varying": [], "groups": []}


def test_wait_chat_and_reset_mock():
    async def wait_while_running() -> float:
        await memory.wait_chat(424242, timeout=5)  # 요약 중이 아니면 바로 돌아온다
        memory._running.add(424242)
        try:
            started = time.monotonic()
            await memory.wait_chat(424242, timeout=0.2)  # 시간이 넘으면 그냥 돌아온다
            return time.monotonic() - started
        finally:
            memory._running.discard(424242)

    assert asyncio.run(wait_while_running()) >= 0.15
    provider = get_provider("mock")
    provider.cache.entries["prefix"] = time.monotonic() + 60
    provider.rng.seed(3)
    expected = [provider.rng.random() for _ in range(3)]
    assert reset_mock(3) == 1 and not provider.cache.entries
    assert [provider.rng.random() for _ in range(3)] == expected
    asyncio.run(close_all())


def test_repeats_alternate_variants_and_report_spread(client, haram_id):
    same = [{"label": "A", "settings": {}}, {"label": "B", "settings": {}}]
    data = wait_finished(client, start(client, body(haram_id, inputs=INPUTS[:3], variants=same, repeats=3)))
    assert data["status"] == "done"
    # 1회차에 모든 변형, 2회차에 모든 변형 순으로 번갈아 돈다
    assert [(r["label"], r["repeat"], r["status"]) for r in data["runs"]] == [
        ("A", 1, "done"), ("B", 1, "done"), ("A", 2, "done"), ("B", 2, "done"), ("A", 3, "done"), ("B", 3, "done")]
    assert data["progress"] == {"run": 6, "runs": 6, "variant": 2, "variants": 2, "repeat": 3, "repeats": 3,
                                "turn": 3, "turns": 3}
    assert client.get(f"/api/chats/{data['runs'][2]['chat_id']}").json()["chat"]["title"] == "[실험] 창 전략 비교 · A · 2회"
    replies = [[m["content"] for m in answered(client, run["chat_id"])] for run in data["runs"]]
    assert replies[0] == replies[1] and replies[2] == replies[3]  # 같은 회차의 변형끼리는 같은 시드
    assert replies[0] != replies[2]  # 회차마다 시드가 하나씩 다르다

    assert [(g["label"], g["turns"]) for g in data["stats"]["groups"]] == [("A", 9), ("B", 9)]  # 비교 표는 반복을 모은다
    spread = data["stats"]["spread"]
    assert list(spread) == ["A", "B"] and spread["A"]["runs"] == 3
    described = spread["A"]["avg_input"]
    assert described["n"] == 3 and described["sd"] is not None and described["sd"] >= 0
    timing = ("median_ttft_ms", "median_first_visible_ms")  # 시간은 실제로 잰 값이라 같은 시드여도 다르다
    assert {k: v for k, v in spread["A"].items() if k not in timing} == \
        {k: v for k, v in spread["B"].items() if k not in timing}  # 두 변형의 설정과 회차별 시드가 같다

    rows = list(csv.reader(io.StringIO(client.get(f"/api/experiments/{data['id']}/export.csv").text.lstrip("﻿"))))
    first = dict(zip(rows[0], rows[1]))
    assert first["label"] == "A" and first["runs"] == "3" and first["avg_input_sd"] != ""


def test_estimate_counts_repeats(client, haram_id, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "PRICING_OVERRIDE_PATH", tmp_path / "none.json")
    monkeypatch.setattr(config, "OPENAI_COMPAT_BASE_URL", "http://127.0.0.1:9/v1")
    real = {"provider": "openai_compat", "models": {"openai_compat": "local-model"}}
    payload = body(haram_id, variants=[{"label": "하나", "settings": real}], repeats=2)
    data = client.post("/api/experiments/estimate", json=payload).json()
    assert data["repeats"] == 2 and data["variants"][0]["chat_calls"] == 12 and data["paid_calls"] == 12
    assert data["cache_overlap"] is True  # 변형이 하나여도 반복하면 같은 모델로 대화를 두 번 보낸다
    assert client.post("/api/experiments/estimate", json={**payload, "repeats": 1}).json()["cache_overlap"] is False
