"""실행 조건 기록(meta.run)과 숨은 비용(LLM 검수 판정, 요약) 집계를 확인한다."""
import pytest

from app import config
from app.prompt import prompts_fingerprint
from tests.conftest import event
from tests.test_api import new_chat, send, settings


def stats(client, chat_id):
    response = client.get(f"/api/chats/{chat_id}/stats")
    assert response.status_code == 200, response.text
    return response.json()


def test_groups_split_by_input_budget(client, haram_id):
    chat_id = new_chat(client, haram_id)["chat"]["id"]
    for budget in (6000, 3000):
        settings(client, input_token_budget=budget)
        for text in ("안녕하세요", "책 구경해도 돼요?"):
            send(client, chat_id, text)
    data = stats(client, chat_id)
    assert data["varying"] == ["input_token_budget"]
    assert [(g["input_token_budget"], g["turns"]) for g in data["groups"]] == [(6000, 2), (3000, 2)]
    run = data["groups"][0]
    assert run["options"] == {"delay_scale": 0.0, "cache_min_tokens": 512, "cache_ttl": 300}
    assert run["prompt_version"] == prompts_fingerprint() and run["label"] is None


def test_groups_split_by_run_label(client, echo_id):
    chat_id = new_chat(client, echo_id)["chat"]["id"]
    for label in ("A", "B"):
        settings(client, run_label=label)
        for text in ("관측실 보여 줘", "선장님은 어디 계셔?"):
            done = event(send(client, chat_id, text), "done")[0]["message"]
    assert done["meta"]["run"]["label"] == "B"
    data = stats(client, chat_id)
    assert data["varying"] == ["label"]
    assert [(g["label"], g["turns"]) for g in data["groups"]] == [("A", 2), ("B", 2)]


def test_llm_judge_cost_is_counted(client, echo_id):
    settings(client, moderation_backend="llm")
    chat_id = new_chat(client, echo_id)["chat"]["id"]
    for text in ("관측실 보여 줘", "선장님은 어디 계셔?"):
        done = event(send(client, chat_id, text), "done")[0]["message"]
        aux = {a["purpose"]: a for a in done["meta"]["aux"]}
        assert set(aux) == {"judge_input", "judge_output"}
        assert all(a["provider"] == "mock" and a["cost"]["usd"] > 0 for a in aux.values())  # mock:* 가격표
    group = stats(client, chat_id)["groups"][0]
    assert group["turns"] == 2 and group["judge_calls"] == 2 * group["turns"]
    assert group["total_usd"] > group["usd"] > 0
    assert group["total_usd"] == pytest.approx(group["usd"] + group["judge_usd"])

    # LLM 판정에 막힌 입력은 답변이 없어도 안내 메시지에 판정 비용이 남고, 턴 수에는 들어가지 않는다
    events = send(client, chat_id, "이건 [[LLM검수테스트]] 입력이에요")
    notice = event(events, "done")[0]["message"]
    assert notice["role"] == "notice" and notice["meta"]["run"]["moderation_backend"] == "llm"
    assert [a["purpose"] for a in notice["meta"]["aux"]] == ["judge_input"]
    group = stats(client, chat_id)["groups"][0]
    assert group["turns"] == 2 and group["judge_calls"] == 5


def test_rules_only_turn_has_no_aux(client, echo_id):
    chat_id = new_chat(client, echo_id)["chat"]["id"]
    done = event(send(client, chat_id, "관측실 보여 줘"), "done")[0]["message"]
    assert done["meta"]["aux"] == []
    group = stats(client, chat_id)["groups"][0]
    assert group["judge_calls"] == 0 and group["total_usd"] == group["usd"]


def test_summary_cost_is_counted(client, haram_id):
    chat_id = new_chat(client, haram_id)["chat"]["id"]
    for text in ("안녕하세요", "책 구경해도 돼요?", "이 서점은 언제 생겼어요?"):
        send(client, chat_id, text)
    summary = client.post(f"/api/chats/{chat_id}/summarize")
    assert summary.status_code == 200, summary.text
    meta = summary.json()["meta"]
    assert meta["run"]["strategy"] == "anchored" and meta["cost"]["usd"] > 0
    group = stats(client, chat_id)["groups"][0]
    assert group["turns"] == 3 and group["summary_calls"] == 1 and group["summary_usd"] > 0
    assert group["total_usd"] == pytest.approx(group["usd"] + group["summary_usd"])


def test_prompts_fingerprint_follows_files(tmp_path, monkeypatch):
    real = prompts_fingerprint()
    for path in config.PROMPTS_DIR.glob("*.txt"):
        (tmp_path / path.name).write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(config, "PROMPTS_DIR", tmp_path)
    copied = prompts_fingerprint()
    assert len(copied) == 8 and copied == real == prompts_fingerprint()
    (tmp_path / "main.txt").write_text("바뀐 규칙", encoding="utf-8")
    assert prompts_fingerprint() != copied


def test_run_label_setting_is_trimmed(client):
    assert config.coerce_setting("run_label", "  x  ") == "x"
    assert config.coerce_setting("run_label", "가" * 41) == "가" * 40
    assert config.coerce_setting("run_label", None) == ""
    assert settings(client, run_label="  실험 A ")["settings"]["run_label"] == "실험 A"
    assert settings(client, run_label="")["settings"]["run_label"] == ""
    assert client.get("/api/config").json()["spec"]["run_label"] == {"type": "text", "max_len": 40}


def test_stats_tolerate_old_messages(client, echo_id):
    db = client.app.state.db
    chat_id = new_chat(client, echo_id)["chat"]["id"]
    greeting = db.get_chat(chat_id)["active_leaf_id"]
    usage = {"input_total": 100, "cache_read": 0, "cache_write": 0, "output": 10}
    user_id = db.add_message(chat_id, greeting, "user", "옛 입력")
    db.add_message(chat_id, user_id, "assistant", "run이 없는 옛 답변", meta={"usage": usage})
    db.add_message(chat_id, user_id, "assistant", "네 값만 남긴 옛 답변", meta={
        "run": {"provider": "mock", "model": "mock-1", "strategy": "anchored", "cache_mode": "explicit"},
        "usage": usage, "cost": {"usd": 0.001, "no_cache_usd": 0.001},
    })
    db.add_message(chat_id, user_id, "notice", "옛 안내", meta={"kind": "blocked"})
    db.add_summary(chat_id, user_id, "옛 요약", meta={"provider": "mock", "model": "mock-1"})
    send(client, chat_id, "새 입력")
    data = stats(client, chat_id)
    old, new = data["groups"]
    assert old["turns"] == 1 and old["input_token_budget"] is None and old["options"] is None
    assert old["total_usd"] == 0.001 and old["avg_first_visible_ms"] is None and old["summary_calls"] == 0
    assert new["turns"] == 1 and new["options"] is not None
    assert {"options", "prompt_version", "input_token_budget"} <= set(data["varying"])
