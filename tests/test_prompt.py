from app import config
from app.cards import parse_card_json
from app.prompt import PLACEHOLDER_AFTER_SUMMARY, PLACEHOLDER_START, assemble, fill
from tests.conftest import load_card


def character(name="haram.json", cid=1):
    data, lore = parse_card_json(load_card(name))
    for i, entry in enumerate(lore):
        entry["id"] = i + 1
    return {"id": cid, **data, "lore": lore}


def settings(**overrides):
    return config.build_settings(overrides)


CHAT = {"id": 1, "character_id": 1, "persona_name": "민준", "persona_desc": "비를 맞은 대학생"}


def msg(mid, role, content, excluded=False):
    return {"id": mid, "role": role, "content": content, "excluded": excluded}


def test_basic_structure_and_macros():
    char = character()
    prior = [msg(1, "assistant", char["first_mes"])]
    asm = assemble(char, CHAT, prior, "안녕하세요, 비 좀 피해도 될까요?", settings(), None)
    assert "{{char}}" not in asm.fixed_system and "{{user}}" not in asm.fixed_system
    assert "하람" in asm.fixed_system and "민준" in asm.fixed_system
    assert "1958년" in asm.fixed_system  # 상시 로어는 고정 프롬프트에
    assert "### 예시 1" in asm.fixed_system
    # 인사말(assistant)로 시작하면 사용자 자리표시자를 앞에 둔다
    assert asm.history[0].role == "user" and asm.history[0].content == PLACEHOLDER_START
    assert asm.history[1].role == "assistant"
    assert asm.final_user.startswith("안녕하세요, 비 좀 피해도 될까요?")
    assert "<system-memo>" in asm.final_user and "[진행 지시]" in asm.final_user
    assert "하람의 말투" in asm.final_user  # 카드의 post_history_instructions + {{original}}
    assert asm.est["total"] == sum(asm.est[k] for k in ("fixed", "summary", "history", "final"))


def test_lore_trigger_goes_to_final_message_only():
    char = character()
    asm = assemble(char, CHAT, [], "저 고양이 이름이 뭐예요?", settings(), None)
    assert [h["name"] for h in asm.lore.summary()["hits"]] == ["먹물"]
    assert "먹물:" in asm.final_user
    assert "거짓말하는 사람" not in asm.fixed_system


def test_selective_entry_needs_secondary_key():
    char = character()
    without = assemble(char, CHAT, [], "자정이 되면 문 닫으세요?", settings(), None)
    assert "움직이는 문장" not in [h["name"] for h in without.lore.summary()["hits"]]
    with_secondary = assemble(char, CHAT, [], "자정이 되면 책 속 글자가 움직인다던데요", settings(), None)
    assert "움직이는 문장" in [h["name"] for h in with_secondary.lore.summary()["hits"]]


def test_lore_budget_keeps_higher_priority():
    char = character()
    text = "고양이랑 할머니 얘기, 그리고 맡아 두는 책도 궁금해요"
    asm = assemble(char, CHAT, [], text, settings(lore_token_budget=150), None)
    names = [h["name"] for h in asm.lore.summary()["hits"]]
    dropped = [h["name"] for h in asm.lore.summary()["dropped"]]
    assert "맡아 두는 서가" in names  # priority 30
    assert "하람의 할머니" in dropped  # priority 10이 먼저 빠진다


def conversation(n):
    prior = [msg(1, "assistant", "어서 오세요.")]
    for i in range(n):
        prior.append(msg(2 + i * 2, "user", f"질문 {i} " + "가" * 120))
        prior.append(msg(3 + i * 2, "assistant", f"답변 {i} " + "나" * 160))
    return prior


def test_sliding_window_drops_oldest():
    char = character("echo.json")
    prior = conversation(12)
    asm = assemble(char, CHAT, prior, "다음 질문", settings(window_strategy="sliding", input_token_budget=2000), None)
    assert asm.window["dropped"] > 0
    assert asm.history[-1].content.startswith("답변 11")
    assert asm.est["total"] <= 2000 + 50
    assert not asm.window["overflow"]  # sliding에서 버리는 건 정상 동작


def test_full_ignores_budget():
    char = character("echo.json")
    prior = conversation(12)
    asm = assemble(char, CHAT, prior, "다음", settings(window_strategy="full", input_token_budget=1000), None)
    assert asm.window["dropped"] == 0 and asm.window["included"] == len(prior)


def test_anchored_starts_after_summary():
    char = character("echo.json")
    prior = conversation(6)
    summary = {"id": 9, "covers_until_msg_id": 7, "content": "민준은 정거장에 도착했다."}
    asm = assemble(char, CHAT, prior, "그래서?", settings(), summary)
    assert asm.summary_block and "민준은 정거장에 도착했다." in asm.summary_block
    assert asm.window["first_id"] == 8
    assert asm.history[0].content.startswith("질문 3")
    # 다른 전략에서는 요약을 쓰지 않는다
    sliding = assemble(char, CHAT, prior, "그래서?", settings(window_strategy="sliding"), summary)
    assert sliding.summary_block is None


def test_summary_placeholder_when_window_starts_with_assistant():
    char = character("echo.json")
    prior = conversation(4)
    summary = {"id": 1, "covers_until_msg_id": 2, "content": "요약"}
    asm = assemble(char, CHAT, prior, "다음", settings(), summary)
    assert asm.history[0].content == PLACEHOLDER_AFTER_SUMMARY


def test_anchored_overflow_when_over_budget():
    char = character("echo.json")
    asm = assemble(char, CHAT, conversation(20), "다음", settings(input_token_budget=2000), None)
    assert asm.window["overflow"] is True and asm.window["dropped"] > 0


def test_excluded_and_trailing_user_are_handled():
    char = character("echo.json")
    prior = [
        msg(1, "assistant", "환영해요"),
        msg(2, "user", "막힌 입력", excluded=True),
        msg(3, "user", "첫 질문"),
        msg(4, "assistant", "가려진 응답", excluded=True),
    ]
    asm = assemble(char, CHAT, prior, "두 번째 질문", settings(), None)
    contents = [m.content for m in asm.history]
    assert "막힌 입력" not in " ".join(contents) + asm.final_user
    assert "가려진 응답" not in " ".join(contents) + asm.final_user
    # 답변이 없는 '첫 질문'은 이번 입력과 합쳐진다(같은 역할이 연속되지 않게)
    assert asm.final_user.startswith("첫 질문\n\n두 번째 질문")
    roles = [m.role for m in asm.history] + ["user"]
    assert all(a != b for a, b in zip(roles, roles[1:]))


def test_fill_does_not_resubstitute_inserted_text():
    out = fill("{input}|{lore}", {"input": "{lore}", "lore": "L"})
    assert out == "{lore}|L"
