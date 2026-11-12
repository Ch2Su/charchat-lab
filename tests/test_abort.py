"""'멈춤'(연결 끊김) 시 화면에 보인 만큼 저장되는지 확인한다."""
import asyncio
import socket
import threading
import time

import httpx
import uvicorn

from app import config
from app.chat_service import ChatService
from app.db import DB
from app.main import create_app, seed_characters


def test_generator_close_saves_partial(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MOCK_DELAY_SCALE", 0)
    db = DB(tmp_path / "abort.sqlite3")
    seed_characters(db)
    service = ChatService(db, lambda: config.build_settings({"output_buffer": "none"}))
    character_id = db.list_characters()[0]["id"]
    chat_id = db.create_chat(character_id, "민준")

    async def scenario():
        gen = service.send(chat_id, "안녕")
        seen = []
        async for chunk in gen:
            seen.append(chunk)
            if chunk.startswith("event: delta"):
                await gen.aclose()  # 클라이언트가 연결을 끊은 상황
                break
        return seen

    asyncio.run(scenario())
    leaf = db.get_message(db.get_chat(chat_id)["active_leaf_id"])
    assert leaf["role"] == "assistant" and leaf["meta"]["status"] == "aborted" and leaf["content"]
    db.close()


def test_http_disconnect_saves_partial(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MOCK_DELAY_SCALE", 1.0)
    app = create_app(tmp_path / "http.sqlite3")
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)
    base = f"http://127.0.0.1:{port}"
    try:
        with httpx.Client(base_url=base, timeout=10) as http:
            http.put("/api/settings", json={"output_buffer": "none"})
            character_id = http.get("/api/characters").json()[0]["id"]
            chat_id = http.post("/api/chats", json={"character_id": character_id}).json()["chat"]["id"]
            deltas = 0
            with http.stream("POST", f"/api/chats/{chat_id}/messages", json={"content": "/long 이야기해 줘"}) as response:
                for line in response.iter_lines():
                    if line.startswith("event: delta"):
                        deltas += 1
                        if deltas >= 5:
                            break  # 스트림 도중 연결을 닫는다
            leaf = None
            for _ in range(60):
                view = http.get(f"/api/chats/{chat_id}").json()
                leaf = view["messages"][-1]
                if leaf["role"] == "assistant" and leaf["meta"].get("status") == "aborted":
                    break
                time.sleep(0.1)
            assert leaf["role"] == "assistant" and leaf["meta"]["status"] == "aborted"
            assert 0 < len(leaf["content"]) < 400
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        app.state.db.close()


def test_stop_after_logged_output_keeps_event_linked(tmp_path, monkeypatch):
    """기록만(log) 모드에서 검수에 걸린 답을 받다가 멈춰도, 검수 기록이 저장된 답에 이어진다."""
    monkeypatch.setattr(config, "MOCK_DELAY_SCALE", 0)
    db = DB(tmp_path / "abort.sqlite3")
    seed_characters(db)
    service = ChatService(db, lambda: config.build_settings({"moderation_mode": "log", "output_buffer": "sentence"}))
    chat_id = db.create_chat(db.list_characters()[0]["id"], "민준")

    async def stop_after_flag():
        stream = service.send(chat_id, "/mod-test-output")
        async for chunk in stream:
            if chunk.startswith("event: delta") and "[[검수테스트]]" in chunk:
                await stream.aclose()
                break

    asyncio.run(stop_after_flag())
    leaf = db.get_message(db.get_chat(chat_id)["active_leaf_id"])
    assert leaf["role"] == "assistant" and leaf["meta"]["status"] == "aborted"
    events = db.list_mod_events(chat_id)
    assert events and events[0]["action"] == "logged" and events[0]["message_id"] == leaf["id"]
    db.close()


def test_stopped_before_any_text_keeps_judge_cost(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MOCK_DELAY_SCALE", 0)
    db = DB(tmp_path / "abort.sqlite3")
    seed_characters(db)
    service = ChatService(db, lambda: config.build_settings({"output_buffer": "sentence", "moderation_backend": "llm"}))
    chat_id = db.create_chat(db.list_characters()[0]["id"], "민준")

    async def stop_while_thinking():
        stream = service.send(chat_id, "/think 생각해 봐")
        async for chunk in stream:
            if chunk.startswith("event: thinking"):
                await stream.aclose()  # 보인 글이 하나도 없을 때 멈춤
                break

    asyncio.run(stop_while_thinking())
    leaf = db.get_message(db.get_chat(chat_id)["active_leaf_id"])
    assert leaf["role"] == "user"  # 답이 저장되지 않았다
    group = service.stats_for_chats([chat_id])["groups"][0]
    assert group["turns"] == 0 and group["judge_calls"] == 1 and group["judge_usd"] > 0
    db.close()

