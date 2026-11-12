import json
import time

from app.cards import export_card, parse_card_json, png_with_card
from tests.conftest import event, load_card, post_sse


def new_chat(client, character_id, persona="민준"):
    response = client.post("/api/chats", json={"character_id": character_id, "persona_name": persona})
    assert response.status_code == 200, response.text
    return response.json()


def send(client, chat_id, text):
    return post_sse(client, f"/api/chats/{chat_id}/messages", {"content": text})


def settings(client, **values):
    response = client.put("/api/settings", json=values)
    assert response.status_code == 200, response.text
    return response.json()


def test_seed_and_greetings(client, haram_id):
    names = [c["name"] for c in client.get("/api/characters").json()]
    assert names == ["에코", "하람"]
    view = new_chat(client, haram_id)
    greeting = view["messages"][0]
    assert greeting["role"] == "assistant" and greeting["sibling_count"] == 2  # 대체 인사말이 형제로


def test_send_streams_and_stores(client, haram_id):
    chat = new_chat(client, haram_id)["chat"]
    events = send(client, chat["id"], "비 좀 피해도 될까요? 저 고양이 이름이 뭐예요?")
    names = [e for e, _ in events]
    assert names[0] == "user" and names[1] == "meta" and names[-1] == "done"
    meta = event(events, "meta")[0]
    assert meta["diagnostics"]["lore"]["hits"][0]["name"] == "먹물"
    streamed = "".join(d["text"] for d in event(events, "delta"))
    done = event(events, "done")[0]["message"]
    assert done["content"] == streamed and "먹물" in streamed  # mock은 반영한 로어 이름을 답변 끝에 적는다
    assert done["meta"]["usage"]["input_total"] > 0
    assert done["meta"]["latency"]["first_visible_ms"] is not None
    view = client.get(f"/api/chats/{chat['id']}").json()
    assert [m["role"] for m in view["messages"]] == ["assistant", "user", "assistant"]
    prompt = client.get(f"/api/messages/{done['id']}/prompt").json()["payload"]
    assert prompt["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "temperature" not in prompt


def test_regenerate_switch_and_edit_branches(client, echo_id):
    chat_id = new_chat(client, echo_id)["chat"]["id"]
    first = event(send(client, chat_id, "관측실부터 보여 줘"), "done")[0]["message"]
    second = event(post_sse(client, f"/api/chats/{chat_id}/regenerate", {}), "done")[0]["message"]
    assert second["parent_id"] == first["parent_id"] and second["id"] != first["id"]
    view = client.get(f"/api/chats/{chat_id}").json()
    assert view["messages"][-1]["sibling_count"] == 2 and view["messages"][-1]["id"] == second["id"]
    view = client.post(f"/api/chats/{chat_id}/switch", json={"message_id": first["id"]}).json()
    assert view["messages"][-1]["id"] == first["id"]

    user_msg = view["messages"][1]
    events = post_sse(client, f"/api/messages/{user_msg['id']}/edit", {"content": "선장님은 어디 계셔?"})
    edited = event(events, "user")[0]["message"]
    assert edited["parent_id"] == user_msg["parent_id"] and edited["meta"]["edited_from"] == user_msg["id"]
    hits = [h["name"] for h in event(events, "meta")[0]["diagnostics"]["lore"]["hits"]]
    assert "한도윤 선장" in hits and "C구역" in hits  # C구역은 인사말에 나와서 같이 걸린다
    view = client.get(f"/api/chats/{chat_id}").json()
    assert view["messages"][1]["sibling_count"] == 2

    assistant = view["messages"][-1]
    edited_reply = client.post(f"/api/messages/{assistant['id']}/edit", json={"content": "*손을 흔든다.* \"안녕!\""})
    assert edited_reply.status_code == 200 and edited_reply.json()["meta"]["manual"] is True


def test_input_block_is_not_sent_later(client, echo_id):
    chat_id = new_chat(client, echo_id)["chat"]["id"]
    events = send(client, chat_id, "이건 [[검수테스트]] 입력이에요")
    assert event(events, "notice")[0]["kind"] == "blocked"
    done = event(send(client, chat_id, "다음 입력"), "done")[0]["message"]
    prompt = client.get(f"/api/messages/{done['id']}/prompt").json()["payload"]
    assert "[[검수테스트]]" not in str(prompt)
    log = client.get(f"/api/chats/{chat_id}/mod-events").json()
    assert log[0]["stage"] == "input" and log[0]["action"] == "blocked"


def test_crisis_flow(client, echo_id):
    chat_id = new_chat(client, echo_id)["chat"]["id"]
    events = send(client, chat_id, "요즘 너무 지쳐서 죽고 싶어")
    notice = event(events, "notice")[0]
    assert notice["kind"] == "crisis" and "109" in notice["text"]
    assert not event(events, "delta")
    assert client.get(f"/api/chats/{chat_id}/mod-events").json()[0]["action"] == "crisis"


def test_output_retract_with_sentence_buffer(client, echo_id):
    chat_id = new_chat(client, echo_id)["chat"]["id"]
    events = send(client, chat_id, "/mod-test-output")
    streamed = "".join(d["text"] for d in event(events, "delta"))
    assert "[[검수테스트]]" not in streamed  # 문장 버퍼가 걸러서 화면에 나가기 전에 멈춘다
    assert event(events, "retract")
    done = event(events, "done")[0]["message"]
    assert done["excluded"] is True and done["meta"]["retracted"]["category"] == "test"


def test_output_retract_without_buffer_and_log_mode(client, echo_id):
    chat_id = new_chat(client, echo_id)["chat"]["id"]
    settings(client, output_buffer="none")
    events = send(client, chat_id, "/mod-test-output")
    streamed = "".join(d["text"] for d in event(events, "delta"))
    assert "[[검수테스트]]" in streamed and event(events, "retract")  # 이미 보낸 뒤 사후에 가린다

    settings(client, moderation_mode="log", output_buffer="sentence")
    events = send(client, chat_id, "/mod-test-output")
    assert not event(events, "retract")
    assert "[[검수테스트]]" in event(events, "done")[0]["message"]["content"]
    assert client.get(f"/api/chats/{chat_id}/mod-events").json()[0]["action"] == "logged"


def test_provider_error_keeps_partial(client, echo_id):
    chat_id = new_chat(client, echo_id)["chat"]["id"]
    settings(client, output_buffer="none")
    events = send(client, chat_id, "/error")
    assert event(events, "error")[0]["partial"] is True
    done = event(events, "done")[0]["message"]
    assert done["meta"]["status"] == "error" and done["content"]


def test_summary_runs_in_background(client, echo_id):
    settings(client, input_token_budget=1200)
    chat_id = new_chat(client, echo_id)["chat"]["id"]
    scheduled = False
    for i in range(8):
        done = event(send(client, chat_id, f"질문 {i}: 정거장 생활은 어때? " + "자세히 " * 10), "done")[0]
        scheduled = scheduled or done["summary_scheduled"]
    assert scheduled
    for _ in range(50):
        view = client.get(f"/api/chats/{chat_id}").json()
        if view["summary"] and not view["summarizing"]:
            break
        time.sleep(0.05)
    assert view["summary"] and "mock 요약" in view["summary"]["content"]
    meta = event(send(client, chat_id, "요약 다음 질문"), "meta")[0]
    assert meta["diagnostics"]["summary"]["id"] == view["summary"]["id"]
    assert meta["diagnostics"]["window"]["first_id"] > view["summary"]["covers_until_msg_id"]


def test_manual_summary_and_stats(client, haram_id):
    chat_id = new_chat(client, haram_id)["chat"]["id"]
    reads = []
    for text in ["안녕하세요", "책 구경해도 돼요?", "이 서점은 언제 생겼어요?", "할머니 얘기도 듣고 싶어요"]:
        done = event(send(client, chat_id, text), "done")[0]["message"]
        reads.append(done["meta"]["usage"]["cache_read"])
    assert reads[0] == 0 and all(b > a for a, b in zip(reads[1:], reads[2:]))  # 대화가 쌓일수록 캐시 적중량이 는다
    summary = client.post(f"/api/chats/{chat_id}/summarize")
    assert summary.status_code == 200, summary.text
    group = client.get(f"/api/chats/{chat_id}/stats").json()["groups"][0]
    assert group["turns"] == 4 and group["cache_hit_ratio"] > 0 and group["has_cost"]


def test_auto_cache_mode_rarely_hits(client, haram_id):
    settings(client, cache_mode="auto")
    chat_id = new_chat(client, haram_id)["chat"]["id"]
    reads = [event(send(client, chat_id, t), "done")[0]["message"]["meta"]["usage"]["cache_read"]
             for t in ["하나", "둘", "셋"]]
    assert reads == [0, 0, 0]


def test_card_import_export_and_block(client):
    card = load_card("echo.json")
    card["data"]["name"] = "에코 복제"
    response = client.post("/api/characters/import", files={"file": ("echo.json", json.dumps(card).encode())})
    assert response.status_code == 200 and response.json()["name"] == "에코 복제"
    character, lore = parse_card_json(load_card("haram.json"))
    character["name"] = "하람 PNG"
    png = png_with_card(export_card({**character, "lore": lore}))
    response = client.post("/api/characters/import", files={"file": ("haram.png", png, "image/png")})
    assert response.status_code == 200 and len(response.json()["lore"]) == 5

    exported = client.get(f"/api/characters/{response.json()['id']}/export")
    assert exported.json()["spec"] == "chara_card_v2"

    bad = load_card("echo.json")
    bad["data"]["description"] += " [[검수테스트]]"
    response = client.post("/api/characters/import", files={"file": ("bad.json", json.dumps(bad).encode())})
    assert response.status_code == 400 and "검수" in response.json()["detail"]

    odd = {"name": "모양이 다른 카드", "character_book": {"entries": [{"keys": ["k"], "priority": "높음"}]}}
    response = client.post("/api/characters/import", files={"file": ("odd.json", json.dumps(odd).encode())})
    assert response.status_code == 400 and "카드 형식" in response.json()["detail"]  # 서버 오류(500)가 아니다


def test_settings_validation(client):
    assert client.put("/api/settings", json={"window_strategy": "nope"}).status_code == 400
    assert client.put("/api/settings", json={"provider": "anthropic"}).status_code == 400  # 키 없음
    body = settings(client, models={"mock": "mock-2"}, input_token_budget=10)
    assert body["model"] == "mock-2" and body["settings"]["input_token_budget"] == 500  # 최소값으로 보정
    assert client.post("/api/settings/reset").json()["model"] == "mock-1"

    # 하나라도 틀리면 함께 보낸 다른 값도 저장하지 않는다
    before = client.get("/api/config").json()["settings"]["input_token_budget"]
    mixed = {"input_token_budget": before + 500, "window_strategy": "nope"}
    assert client.put("/api/settings", json=mixed).status_code == 400
    assert client.get("/api/config").json()["settings"]["input_token_budget"] == before


def test_empty_model_is_rejected_before_streaming(client, echo_id, monkeypatch):
    from app import config

    monkeypatch.setattr(config, "OPENAI_COMPAT_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setitem(config.DEFAULT_MODELS, "openai_compat", "")
    chat_id = new_chat(client, echo_id)["chat"]["id"]
    settings(client, provider="openai_compat")
    response = client.post(f"/api/chats/{chat_id}/messages", json={"content": "안녕"})
    assert response.status_code == 400 and "모델 이름" in response.json()["detail"]


def test_mock_cache_clear(client, haram_id):
    chat_id = new_chat(client, haram_id)["chat"]["id"]
    send(client, chat_id, "안녕하세요")
    assert client.post("/api/mock/cache/clear").json()["cleared"] > 0
    done = event(send(client, chat_id, "다시 안녕하세요"), "done")[0]["message"]
    assert done["meta"]["usage"]["cache_read"] == 0  # 비웠으니 처음부터 다시 쓴다


def test_greeting_cannot_be_regenerated(client, echo_id):
    chat_id = new_chat(client, echo_id)["chat"]["id"]
    response = client.post(f"/api/chats/{chat_id}/regenerate", json={})
    assert response.status_code == 400 and "인사말" in response.json()["detail"]


def test_thinking_is_measured_but_hidden(client, echo_id):
    chat_id = new_chat(client, echo_id)["chat"]["id"]
    events = send(client, chat_id, "/think 관측실 보여 줘")
    assert event(events, "thinking")
    done = event(events, "done")[0]["message"]
    assert done["meta"]["thinking"]["chars"] > 0 and done["meta"]["usage"]["thinking"] > 0
    assert "캐릭터 설정과" not in done["content"]


def test_leaked_thinking_is_reset(client, echo_id):
    settings(client, output_buffer="none")
    chat_id = new_chat(client, echo_id)["chat"]["id"]
    events = send(client, chat_id, "/think-leak 안녕")
    names = [e for e, _ in events]
    reset_at = names.index("reset")
    assert "delta" in names[:reset_at]  # 초안이 먼저 화면에 나갔다가 지워진다
    done = event(events, "done")[0]["message"]
    assert "초안" not in done["content"]
    assert done["content"] == "".join(d["text"] for e, d in events[reset_at:] if e == "delta")
    assert done["meta"]["thinking"]["chars"] > len("(초안")


def test_thinking_only_reply_becomes_notice(client, echo_id, monkeypatch):
    from app import chat_service
    from app.providers.base import Provider, StreamEvent, Usage

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
    assert done["excluded"] and "최대 출력" in done["content"]


def test_empty_reply_becomes_notice(client, echo_id, monkeypatch):
    """빈 답은 안내문으로 바꿔 맥락에서 뺀다. 그대로 저장하면 다음 턴 요청에 빈 메시지가 들어가, 빈 블록을 받지 않는 제공사(Claude 등)가 400을 낸다."""
    from app import chat_service
    from app.providers.base import Provider, StreamEvent, Usage

    requests = []

    class Empty(Provider):
        name = "mock"
        usage = Usage(input_total=10)

        def render(self, req):
            return {"fake": True}

        async def stream(self, req):
            requests.append(req)
            yield StreamEvent("text", text="\n")
            yield StreamEvent("done", usage=self.usage, finish_reason="stop")

    provider = Empty()
    monkeypatch.setattr(chat_service, "get_provider", lambda name: provider)
    chat_id = new_chat(client, echo_id)["chat"]["id"]
    done = event(send(client, chat_id, "안녕"), "done")[0]["message"]
    assert done["excluded"] and "빈 답변" in done["content"]

    provider.usage = Usage(input_total=10, output=50, thinking=50)  # 사고 내용은 안 주고 토큰 수만 알리는 제공사
    done = event(send(client, chat_id, "다시 안녕"), "done")[0]["message"]
    assert done["excluded"] and "사고만" in done["content"]
    assert all(m["content"].strip() for m in requests[-1].history)  # 앞의 빈 답이 요청에 들어가지 않았다


def test_blank_assistant_edit_is_rejected(client, echo_id):
    chat_id = new_chat(client, echo_id)["chat"]["id"]
    reply = event(send(client, chat_id, "안녕"), "done")[0]["message"]
    response = client.post(f"/api/messages/{reply['id']}/edit", json={"content": " \n"})
    assert response.status_code == 400 and "비어" in response.json()["detail"]
