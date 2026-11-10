"""실험용 덮어쓰기 설정을 확인한다: 제공사 옵션(provider_options)과 프롬프트 파일 내용(prompt_overrides)."""
import json

import pytest

from app import config, quality
from app.prompt import load_prompt, memo_markers, prompts_fingerprint
from tests.conftest import event
from tests.test_api import new_chat, send
from tests.test_experiments import INPUTS, answered, body, start, wait_finished


def test_provider_options_are_checked():
    def coerce(value):
        return config.coerce_setting("provider_options", value)

    assert coerce({"openai_compat": {"reasoning_effort": "LOW", "top_p": "0.9"}}) == \
        {"openai_compat": {"reasoning_effort": "low", "top_p": 0.9}}
    # None과 빈 값은 '보내지 않음'(.env에 값이 있어도 모델 기본값을 쓴다)
    assert coerce({"openai_compat": {"reasoning_effort": None, "top_p": ""}}) == \
        {"openai_compat": {"reasoning_effort": None, "top_p": None}}
    for bad, message in [({"mock": {}}, "제공사"), ({"openai_compat": {"temperature": 1}}, "알 수 없는 옵션"),
                         ({"openai_compat": {"top_p": 1.5}}, "0~1"), ({"anthropic": {"effort": "아주 높게"}}, "영문 소문자"),
                         (["low"], "형태")]:
        with pytest.raises(config.SettingsError, match=message):
            coerce(bad)


def test_prompt_overrides_are_checked():
    def coerce(value):
        return config.coerce_setting("prompt_overrides", value)

    assert coerce({"final_user": "메모\r\n{input}  "}) == {"final_user.txt": "메모\n{input}"}
    for bad, message in [({"nope": "x"}, "없는 파일"), ({"final_user": "입력 자리가 없다"}, r"\{input\}"),
                         ({"main": "  "}, "비어 있지"), ({"main": 3}, "비어 있지")]:
        with pytest.raises(config.SettingsError, match=message):
            coerce(bad)


def test_merging_keeps_other_entries():
    merge = config.merge_setting
    saved = {"openai_compat": {"reasoning_effort": "none"}}
    assert merge("provider_options", saved, {"openai_compat": {"top_p": 0.8}}) == \
        {"openai_compat": {"reasoning_effort": "none", "top_p": 0.8}}
    # 옵션 하나의 None은 '보내지 않음'이라는 값이라 남고, 제공사 묶음의 None은 덮어쓰기를 지운다
    assert merge("provider_options", saved, {"openai_compat": {"reasoning_effort": None}}) == \
        {"openai_compat": {"reasoning_effort": None}}
    assert merge("provider_options", saved, {"openai_compat": None}) == {}
    assert merge("prompt_overrides", {"main.txt": "규칙"}, {"final_user": "{input}"}) == \
        {"main.txt": "규칙", "final_user.txt": "{input}"}
    assert merge("prompt_overrides", {"main.txt": "규칙"}, {"main": None}) == {}


def test_overridden_prompt_and_fingerprint():
    original = load_prompt("final_user.txt")
    overrides = {"final_user.txt": "<note>\n{instructions}\n</note>\n\n{input}"}
    assert load_prompt("final_user.txt", overrides).startswith("<note>")
    assert load_prompt("main.txt", overrides) == load_prompt("main.txt")
    assert prompts_fingerprint(overrides) != prompts_fingerprint()
    assert prompts_fingerprint({"final_user.txt": original}) == prompts_fingerprint()  # 내용이 같으면 지문도 같다
    assert memo_markers() == ["system-memo"] and memo_markers(overrides) == ["note"]


def test_leak_follows_prompt_markers():
    text = '"<note>에 적힌 대로 할게요."'
    assert not quality.measure(text, [])["leak"]
    leaked = quality.measure(text, [], markers=["note"])
    assert leaked["leak"] and "leak" in leaked["flags"]


def test_saved_overrides_apply_to_turns(client, echo_id):
    marked = "[[덮어쓴 메모]]\n{input}\n\n<system-memo>\n{lore}{instructions}\n</system-memo>"
    response = client.put("/api/settings", json={"prompt_overrides": {"final_user": marked}})
    assert response.status_code == 200, response.text
    chat_id = new_chat(client, echo_id)["chat"]["id"]
    done = event(send(client, chat_id, "안녕"), "done")[0]["message"]
    assert done["meta"]["run"]["prompt_version"] == prompts_fingerprint({"final_user.txt": marked}) != prompts_fingerprint()
    prompt = client.get(f"/api/messages/{done['id']}/prompt").json()
    assert "[[덮어쓴 메모]]" in json.dumps(prompt["payload"], ensure_ascii=False)
    # 지우면 파일 내용으로 돌아간다
    client.put("/api/settings", json={"prompt_overrides": {"final_user": None}})
    done = event(send(client, chat_id, "또 안녕"), "done")[0]["message"]
    assert done["meta"]["run"]["prompt_version"] == prompts_fingerprint()
    assert client.put("/api/settings", json={"prompt_overrides": {"final_user": "입력 없음"}}).status_code == 400
    assert client.put("/api/settings", json={"provider_options": {"mock": {"x": 1}}}).status_code == 400


def test_variant_prompt_overrides_split_rows(client, echo_id):
    client.put("/api/settings", json={"prompt_overrides": {"main": "[[저장된 규칙]] {{char}}로서 답하라."}})
    moved = "<system-memo>\n[[메모를 앞에]]\n{lore}{instructions}\n</system-memo>\n\n{input}"
    payload = body(echo_id, inputs=INPUTS[:2], variants=[
        {"label": "기본", "settings": {}},
        {"label": "메모 앞", "settings": {"prompt_overrides": {"final_user": moved}}},
    ])
    data = wait_finished(client, start(client, payload))
    assert data["status"] == "done"
    assert data["stats"]["varying"] == ["label", "prompt_version"]  # 프롬프트만 다른 변형은 지문으로 행이 나뉜다
    sent = {}
    for run in data["runs"]:
        message = answered(client, run["chat_id"])[-1]
        sent[run["label"]] = json.dumps(client.get(f"/api/messages/{message['id']}/prompt").json()["payload"],
                                        ensure_ascii=False)
    assert "[[메모를 앞에]]" in sent["메모 앞"] and "[[메모를 앞에]]" not in sent["기본"]
    assert all("[[저장된 규칙]]" in text for text in sent.values())  # 변형의 덮어쓰기는 저장된 것에 합쳐진다
    stored = data["spec"]["variants"][1]["settings"]["prompt_overrides"]
    assert set(stored) == {"main.txt", "final_user.txt"}
    assert client.get("/api/config").json()["settings"]["prompt_overrides"] == {"main.txt": "[[저장된 규칙]] {{char}}로서 답하라."}
