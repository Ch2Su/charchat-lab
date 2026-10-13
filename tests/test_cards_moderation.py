import json

import pytest

from app.cards import CardError, apply_macros, export_card, parse_card_bytes, parse_card_json, png_with_card
from app.moderation import RULES, SentenceGate, parse_judge
from app.providers.mock import CacheSimulator
from app.providers.openai_compat import ThinkStripper
from tests.conftest import load_card


# ── 카드 ──────────────────────────────────────────────
def test_parse_v2_card():
    character, lore = parse_card_json(load_card("haram.json"))
    assert character["name"] == "하람"
    assert len(character["alternate_greetings"]) == 1
    assert len(lore) == 5
    assert [e["name"] for e in lore if e["constant"]] == ["물푸레 서점"]
    assert next(e for e in lore if e["name"] == "움직이는 문장")["selective"] is True


def test_export_png_roundtrip():
    character, lore = parse_card_json(load_card("haram.json"))
    card = export_card({**character, "lore": lore})
    again, again_lore = parse_card_bytes(png_with_card(card), "haram.png")
    assert again["name"] == "하람" and again["first_mes"] == character["first_mes"]
    assert [e["name"] for e in again_lore] == [e["name"] for e in lore]
    assert again_lore[3]["secondary_keys"] == lore[3]["secondary_keys"]


def test_v1_and_dict_entries():
    flat = {"name": "테스트", "first_mes": "안녕", "character_book": {"entries": {"0": {"keys": "a, b", "content": "x"}}}}
    character, lore = parse_card_json(flat)
    assert character["name"] == "테스트" and lore[0]["keys"] == ["a", "b"]
    with pytest.raises(CardError):
        parse_card_json({"spec": "chara_card_v2", "data": {"name": ""}})
    with pytest.raises(CardError):
        parse_card_bytes(b"\x89PNG\r\n\x1a\nbroken")


def test_macros():
    assert apply_macros("{{char}}와 {{ User }}, <BOT>/<USER>", "하람", "민준") == "하람와 민준, 하람/민준"


# ── 검수 ──────────────────────────────────────────────
def test_rules():
    assert RULES.check("요즘 너무 힘들어서 죽고 싶어").category == "self_harm"
    assert not RULES.check("배고파서 밥 먹으러 갈래").flagged
    assert RULES.check("이건 [[검수테스트]] 문장").category == "test"


def test_sentence_gate_releases_by_sentence():
    gate = SentenceGate(RULES.check)
    out, flag = gate.feed("*고개를 끄덕인다.* \"좋아")
    assert flag is None and out == "*고개를 끄덕인다.* "
    out, flag = gate.feed("요.\" 그리고")
    assert out == "\"좋아요.\" "
    out, flag = gate.flush()
    assert out == "그리고"
    assert gate.released == "*고개를 끄덕인다.* \"좋아요.\" 그리고"


def test_sentence_gate_stops_on_flag_and_force_release():
    gate = SentenceGate(RULES.check, max_hold_chars=20)
    out, flag = gate.feed("첫 문장. 그 다음에 [[검수테스트]] 문장. ")
    assert out == "" and flag.category == "test"
    long_gate = SentenceGate(RULES.check, max_hold_chars=10)
    out, _ = long_gate.feed("마침표가없는아주긴문장이계속됩니다")
    assert out == "마침표가없는아주긴문장이계속됩니다"


def test_parse_judge():
    assert parse_judge('결과: {"flagged": true, "category": "sexual"}')["flagged"] is True
    assert parse_judge("판정 불가") is None


def test_think_stripper_across_chunks():
    stripper = ThinkStripper()
    pieces = ["<thi", "nk>속으로 생각", "중</th", "ink>\n\n안녕", "하세요 <", "b>"]
    out = "".join(stripper.feed(p) for p in pieces) + stripper.flush()
    assert out == "안녕하세요 <b>"
    assert stripper.hidden_chars > 0


# ── 캐시 흉내 ─────────────────────────────────────────
def payload(system, history, final, mode="explicit"):
    from app.providers.anthropic_format import build_anthropic_payload
    from app.providers.base import ChatRequest

    req = ChatRequest(model="m", fixed_system=system, summary=None, history=history, final_user=final,
                      max_tokens=100, temperature=None, cache_mode=mode)
    return build_anthropic_payload(req, 100)


def test_cache_simulator_explicit_reads_previous_turn():
    sim = CacheSimulator(min_tokens=50, ttl=300)
    system = "설정 " * 200
    turn1 = [{"role": "user", "content": "(이야기 시작)"}, {"role": "assistant", "content": "안녕하세요"}]
    first = sim.simulate(payload(system, turn1, "첫 질문 + 메모"))
    assert first["cache_read"] == 0 and first["cache_write"] > 0
    turn2 = turn1 + [{"role": "user", "content": "첫 질문"}, {"role": "assistant", "content": "첫 답변"}]
    second = sim.simulate(payload(system, turn2, "둘째 질문 + 메모"))
    # 직전 턴의 ③ 중단점(첫 답변 직전의 '안녕하세요'까지)이 캐시에서 읽힌다
    assert second["cache_read"] > first["cache_write"] - 40
    # 같은 요청을 다시 보내면(다시 생성) ④ 덕분에 거의 전부 캐시에서 읽힌다
    again = sim.simulate(payload(system, turn2, "둘째 질문 + 메모"))
    assert again["uncached"] == 0 and again["cache_write"] == 0


def test_cache_simulator_auto_mode_misses_when_last_message_changes():
    sim = CacheSimulator(min_tokens=50, ttl=300)
    system = "설정 " * 200
    history = [{"role": "user", "content": "시작"}, {"role": "assistant", "content": "안녕"}]
    sim.simulate(payload(system, history, "질문1 + 메모", mode="auto"))
    history2 = history + [{"role": "user", "content": "질문1"}, {"role": "assistant", "content": "답1"}]
    second = sim.simulate(payload(system, history2, "질문2 + 메모", mode="auto"))
    assert second["cache_read"] == 0  # 지난번에 쓴 위치(메모가 붙은 질문1)가 이번엔 다른 내용이라 적중하지 않음


def test_cache_simulator_minimum_length():
    sim = CacheSimulator(min_tokens=5000, ttl=300)
    result = sim.simulate(payload("짧은 설정", [], "질문"))
    assert result["cache_write"] == 0 and result["below_minimum"] > 0
