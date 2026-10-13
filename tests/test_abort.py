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
