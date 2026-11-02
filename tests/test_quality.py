"""답변 품질 어림 지표(app/quality.py)와 턴 기록·비교 집계를 확인한다."""
import asyncio

import pytest

from app import chat_service, config, quality
from app.chat_service import ChatService
from app.db import DB
from app.main import seed_characters
from app.providers import close_all
from app.providers.base import Provider, StreamEvent, Usage
from tests.conftest import event
from tests.test_api import new_chat, send, settings


def test_script_counts():
    assert quality.script_counts("안녕 世界 hello") == {"hangul": 2, "han": 2, "kana": 0, "latin": 5, "other": 0}
    assert quality.script_counts("カタ привет 123!") == {"hangul": 0, "han": 0, "kana": 2, "latin": 0, "other": 6}


def test_speech_level_reads_dialogue_only():
    polite = quality.speech_level('*그가 웃었다.* "어서 오세요. 천천히 보세요."')
    assert polite == {"polite": 2, "plain": 0, "level": "polite"}  # 묘사의 '웃었다'는 세지 않는다
    assert quality.speech_level('"왔어? 앉아."') == {"polite": 0, "plain": 2, "level": "plain"}
    assert quality.speech_level("*문을 열었다.* 어서 와.")["level"] == "plain"  # 대사가 없으면 묘사를 뺀 나머지
    assert quality.speech_level("*고개를 끄덕였다.*")["level"] is None
    # 모음이 줄어든 반말 어미도 반말로 센다
    assert quality.speech_level('"고마워. 뭐 해? 이리 줘."') == {"polite": 0, "plain": 3, "level": "plain"}


def test_prompt_markup_leak_is_flagged():
    leaked = quality.measure('"책 구경? <system-memo>가 말씀하신 설정을 기억해야 해요."', [])
    assert leaked["leak"] and "leak" in leaked["flags"]
    assert quality.measure('"{{user}} 님, 어서 오세요."', [])["leak"]
    plain = quality.measure('"어서 오세요. 천천히 보세요."', [])
    assert not plain["leak"] and "leak" not in plain["flags"]


def test_archaic_polite_ending_is_polite():
    assert quality.speech_level('"그렇게 하겠습네다."')["level"] == "polite"


def test_repeat_prev_flag_starts_at_forty_percent():
    first = "".join(chr(0xAC00 + i) for i in range(24))
    second = "".join(chr(0xAC00 + i) for i in range(24, 48))
    measured = quality.measure(first + second, [first])  # 5-gram 44개 중 20개가 직전 답에 있다
    assert 0.4 <= measured["repeat_prev"] < 0.5 and "repeat_prev" in measured["flags"]


def test_unclosed_quote_does_not_misread_dialogue():
    # 큰따옴표가 홀수면 짝짓기가 어긋난다. 첫 따옴표를 닫지 않아도 묘사의 "~했다"가 반말 대사로 잡히면 안 된다
    text = '"안녕하세요? *그가 웃었다.* "어서 오세요. 천천히 보세요." "차 한잔 하시겠어요?"'
    assert text.count('"') % 2 == 1
    assert quality.speech_level(text) == {"polite": 4, "plain": 0, "level": "polite"}
    assert quality.speech_level('"어서 와." *그가 웃었다.* "앉아."')["level"] == "plain"  # 짝이 맞으면 전과 같다


def test_polite_endings_are_checked_first():
    assert quality.speech_level('"그렇게 합니다."') == {"polite": 1, "plain": 0, "level": "polite"}
    assert quality.speech_level('"네? 무슨 말씀이세요?"')["level"] == "polite"  # 한 글자 대답 '네'는 존댓말
    assert quality.speech_level('"좋아요. 그래."')["level"] == "mixed"


def test_speech_shift_against_previous_majority():
    previous = ['"어서 오세요."', '"천천히 보세요."']
    shifted = quality.measure('"왔어? 앉아."', previous)
    assert shifted["speech_prev"] == "polite" and shifted["speech_shift"] and "speech_shift" in shifted["flags"]
    mixed = quality.measure('"좋아요. 그래."', previous)
    assert mixed["speech"]["level"] == "mixed" and not mixed["speech_shift"]
    tie = quality.measure('"왔어?"', ['"어서 오세요."', '"앉아."'])
    assert tie["speech_prev"] is None and not tie["speech_shift"]  # 직전 말투가 동률이면 판단하지 않는다


def test_repeat_prev_compares_with_the_last_answer():
    text = '*책장을 넘긴다.* "이 책은 할머니가 아끼던 책이에요."'
    assert quality.measure(text, [text])["repeat_prev"] == 1.0
    assert quality.measure(text, ["완전히 딴 얘기 abc 12345"])["repeat_prev"] == 0.0
    assert quality.measure(text, [])["repeat_prev"] == 0.0
    assert quality.measure(text, [text, "완전히 딴 얘기 abc 12345"])["repeat_prev"] == 0.0  # 바로 앞 답만 본다


def test_repeat_self():
    text = "같은 말을 또 하네요. 같은 말을 또 하네요. 이번에는 다른 이야기예요."
    assert quality.measure(text, [])["repeat_self"] == pytest.approx(1 / 3, abs=0.001)
    assert quality.measure("짧은 말. 짧은 말. 짧은 말.", [])["repeat_self"] == 0.0  # 8자 미만 문장은 세지 않는다


def test_foreign_and_latin_flags():
    clean = quality.measure('"오늘은 비가 오네요. 차 한 잔 드릴까요?"', [])
    assert clean["foreign_ratio"] == 0 and clean["latin_ratio"] == 0 and clean["flags"] == []
    mixed = quality.measure('"这个故事 정말 interesting해요."', [])
    assert {"foreign", "latin"} <= set(mixed["flags"])
    assert mixed["chars"] == len('"这个故事 정말 interesting해요."') and mixed["sentences"] == 1


def test_quality_test_command_flags_answer(client, haram_id):
    chat_id = new_chat(client, haram_id)["chat"]["id"]
    first = event(send(client, chat_id, "안녕하세요"), "done")[0]["message"]["meta"]["quality"]
    assert first["speech_prev"] == "polite" and first["flags"] == []  # 인사말도 직전 답변으로 친다
    done = event(send(client, chat_id, "/quality-test"), "done")[0]["message"]
    measured = done["meta"]["quality"]
    assert "foreign" in measured["flags"] and measured["repeat_self"] > 0
    group = client.get(f"/api/chats/{chat_id}/stats").json()["groups"][0]
    assert group["quality_turns"] == 2 and group["flagged_turns"] >= 1
    assert group["avg_foreign_ratio"] > 0 and group["speech_shift_turns"] == 0
    assert group["foreign_turns"] == 1  # 외국 문자가 한 글자라도 낀 턴 수(평균 비율과 달리 턴당 한 번만 센다)
    assert group["median_ttft_ms"] is not None and group["median_first_visible_ms"] is not None


def test_hidden_error_and_edited_answers_are_not_measured(client, echo_id):
    chat_id = new_chat(client, echo_id)["chat"]["id"]
    retracted = event(send(client, chat_id, "/mod-test-output"), "done")[0]["message"]
    assert retracted["excluded"] and "quality" not in retracted["meta"]
    settings(client, output_buffer="none")
    errored = event(send(client, chat_id, "/error"), "done")[0]["message"]
    assert errored["meta"]["status"] == "error" and "quality" not in errored["meta"]
    response = client.post(f"/api/messages/{errored['id']}/edit", json={"content": '*손을 흔든다.* "안녕!"'})
    assert response.status_code == 200 and "quality" not in response.json()["meta"]


def test_thinking_only_answer_is_not_measured(client, echo_id, monkeypatch):
    class ThinkOnly(Provider):
        name = "mock"

        def render(self, req):
            return {"fake": True}

        async def stream(self, req):
            yield StreamEvent("thinking", text="생각만 계속한다")
            yield StreamEvent("done", usage=Usage(output=50, thinking=50), finish_reason="length")

    monkeypatch.setattr(chat_service, "get_provider", lambda name: ThinkOnly())
    chat_id = new_chat(client, echo_id)["chat"]["id"]
    done = event(send(client, chat_id, "안녕"), "done")[0]["message"]
    assert done["excluded"] and "quality" not in done["meta"]


def test_aborted_answer_is_not_measured(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MOCK_DELAY_SCALE", 0)
    db = DB(tmp_path / "abort.sqlite3")
    seed_characters(db)
    service = ChatService(db, lambda: config.build_settings({"output_buffer": "none"}))
    chat_id = db.create_chat(db.list_characters()[0]["id"], "민준")

    async def stop_after_first_delta():
        stream = service.send(chat_id, "안녕")
        async for chunk in stream:
            if chunk.startswith("event: delta"):
                await stream.aclose()  # 멈춤
                break
        await close_all()

    asyncio.run(stop_after_first_delta())
    leaf = db.get_message(db.get_chat(chat_id)["active_leaf_id"])
    assert leaf["meta"]["status"] == "aborted" and "quality" not in leaf["meta"]
    db.close()
