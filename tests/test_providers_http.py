"""실제 SDK(anthropic, google-genai, openai)를 가짜 HTTP 서버에 붙여서 어댑터를 검증한다.

API 키 없이도 요청 본문 형식과 스트림 해석이 맞는지 확인할 수 있다.
"""
import asyncio
import json
import socket
import threading
import time

import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from app import config
from app.providers.base import ChatRequest, ProviderError

RECORDED: list[dict] = []
MODE = {"gemini_finish": "STOP", "anthropic_status": 200, "openai_stream": "think_tags"}


def sse_lines(events):
    for name, data in events:
        prefix = f"event: {name}\n" if name else ""
        yield f"{prefix}data: {json.dumps(data, ensure_ascii=False)}\n\n"


async def anthropic_messages(request: Request):
    body = await request.json()
    RECORDED.append({"provider": "anthropic", "body": body})
    if MODE["anthropic_status"] != 200:
        return JSONResponse({"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}},
                            status_code=MODE["anthropic_status"])
    usage_start = {"input_tokens": 30, "cache_creation_input_tokens": 200, "cache_read_input_tokens": 1500,
                   "output_tokens": 1}
    if not body.get("stream"):
        return JSONResponse({
            "id": "msg_1", "type": "message", "role": "assistant", "model": body["model"],
            "content": [{"type": "text", "text": "요약 결과입니다"}],
            "stop_reason": "end_turn", "stop_sequence": None, "usage": usage_start,
        })
    events = [
        ("message_start", {"type": "message_start", "message": {
            "id": "msg_1", "type": "message", "role": "assistant", "model": body["model"], "content": [],
            "stop_reason": None, "stop_sequence": None, "usage": usage_start}}),
        ("content_block_start", {"type": "content_block_start", "index": 0,
                                 "content_block": {"type": "thinking", "thinking": "", "signature": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "thinking_delta", "thinking": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "signature_delta", "signature": "sig"}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("content_block_start", {"type": "content_block_start", "index": 1,
                                 "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 1,
                                 "delta": {"type": "text_delta", "text": "*하람이 웃는다.* "}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 1,
                                 "delta": {"type": "text_delta", "text": "\"어서 오세요.\""}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 1}),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                           "usage": {"output_tokens": 42, "output_tokens_details": {"thinking_tokens": 12}}}),
        ("message_stop", {"type": "message_stop"}),
    ]
    return StreamingResponse(sse_lines(events), media_type="text/event-stream")


async def gemini_models(request: Request):
    body = await request.json()
    path = request.path_params["rest"]
    RECORDED.append({"provider": "gemini", "path": path, "query": dict(request.query_params), "body": body})
    usage = {"promptTokenCount": 5000, "cachedContentTokenCount": 4096, "candidatesTokenCount": 30,
             "thoughtsTokenCount": 15, "totalTokenCount": 5045}
    if path.endswith(":generateContent"):
        return JSONResponse({"candidates": [{"content": {"role": "model", "parts": [{"text": "요약"}]},
                                             "finishReason": "STOP"}], "usageMetadata": usage})
    finish = MODE["gemini_finish"]
    chunks = [
        {"candidates": [{"content": {"role": "model", "parts": [{"text": "생각 중…", "thought": True}]}}]},
        {"candidates": [{"content": {"role": "model", "parts": [{"text": "삐빅! "}]}}]},
        {"candidates": [{"content": {"role": "model", "parts": [{"text": "환영해요."}]}, "finishReason": finish}],
         "usageMetadata": usage},
    ]
    return StreamingResponse(sse_lines((None, c) for c in chunks), media_type="text/event-stream")


async def openai_chat(request: Request):
    body = await request.json()
    RECORDED.append({"provider": "openai", "body": body})
    if not body.get("stream"):
        return JSONResponse({"id": "c1", "object": "chat.completion", "created": 0, "model": body["model"],
                             "choices": [{"index": 0, "finish_reason": "stop",
                                          "message": {"role": "assistant", "content": "<think>음</think>요약"}}],
                             "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}})

    def chunk(content=None, finish=None, usage=None, reasoning=None):
        delta = {k: v for k, v in (("content", content), ("reasoning", reasoning)) if v}
        data = {"id": "c1", "object": "chat.completion.chunk", "created": 0, "model": body["model"],
                "choices": [] if usage else [{"index": 0, "delta": delta, "finish_reason": finish}]}
        if usage:
            data["usage"] = usage
        return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

    async def gen():
        if MODE["openai_stream"] == "reasoning":
            # Ollama처럼 사고는 reasoning 필드로 따로 오고, 본문에 짝 없는 </think>가 섞인 경우
            for piece in ["생각 ", "중…"]:
                yield chunk(reasoning=piece)
            for piece in ["반복된 초안", "</think>", "\n\n안녕", "하세요"]:
                yield chunk(piece)
        else:
            for piece in ["<thi", "nk>속으로", " 고민</think>", "\n\n안녕", "하세요"]:
                yield chunk(piece)
        yield chunk(finish="stop")
        yield chunk(usage={"prompt_tokens": 900, "completion_tokens": 7, "total_tokens": 907,
                           "prompt_tokens_details": {"cached_tokens": 640}})
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


fake_app = Starlette(routes=[
    Route("/v1/messages", anthropic_messages, methods=["POST"]),
    Route("/v1beta/models/{rest:path}", gemini_models, methods=["POST"]),
    Route("/v1/chat/completions", openai_chat, methods=["POST"]),
])


@pytest.fixture(scope="module")
def fake_server():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(uvicorn.Config(fake_app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


def request(**overrides):
    base = dict(
        model="test-model",
        fixed_system="고정 프롬프트",
        summary="## 지금까지의 줄거리\n요약",
        history=[{"role": "user", "content": "(이야기 시작)"}, {"role": "assistant", "content": "어서 오세요."}],
        final_user="안녕하세요\n\n<system-memo>…</system-memo>",
        max_tokens=700,
        temperature=0.9,
        cache_mode="explicit",
    )
    base.update(overrides)
    return ChatRequest(**base)


async def stream_events(provider, req):
    return [ev async for ev in provider.stream(req)]


async def collect(provider, req):
    texts, done = [], None
    for ev in await stream_events(provider, req):
        if ev.type == "text":
            texts.append(ev.text)
        elif ev.type == "reset":
            texts.clear()
        elif ev.type == "done":
            done = ev
    return "".join(texts), done


def test_anthropic_stream(fake_server, monkeypatch):
    from app.providers.anthropic_provider import AnthropicProvider

    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(config, "ANTHROPIC_BASE_URL", fake_server)
    monkeypatch.setattr(config, "ANTHROPIC_EFFORT", "low")
    monkeypatch.setattr(config, "ANTHROPIC_THINKING", None)
    MODE["anthropic_status"] = 200
    provider = AnthropicProvider()
    events = asyncio.run(stream_events(provider, request()))
    assert [ev.type for ev in events].count("thinking") == 3  # 블록 시작, thinking_delta, signature_delta
    text, done = asyncio.run(collect(provider, request()))
    assert text == "*하람이 웃는다.* \"어서 오세요.\""
    assert done.finish_reason == "stop"
    assert done.usage.input_total == 30 + 200 + 1500
    assert done.usage.cache_read == 1500 and done.usage.cache_write == 200
    assert done.usage.output == 42 and done.usage.thinking == 12
    body = RECORDED[-1]["body"]
    assert "temperature" not in body and "top_p" not in body
    assert body["output_config"] == {"effort": "low"} and body["max_tokens"] == 700 + 2048
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert body["system"][1]["cache_control"] == {"type": "ephemeral"}
    assert body["messages"][-2]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    assert body["messages"][-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}

    auto = asyncio.run(collect(provider, request(cache_mode="auto")))
    assert auto[1].finish_reason == "stop"
    body = RECORDED[-1]["body"]
    assert body["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in json.dumps(body["messages"]) and "cache_control" not in json.dumps(body["system"])

    result = asyncio.run(provider.complete("sys", "user", "test-model", 100))
    assert result.text == "요약 결과입니다"


def test_anthropic_auth_error(fake_server, monkeypatch):
    from app.providers.anthropic_provider import AnthropicProvider

    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "bad")
    monkeypatch.setattr(config, "ANTHROPIC_BASE_URL", fake_server)
    MODE["anthropic_status"] = 401
    try:
        with pytest.raises(ProviderError, match="API 키"):
            asyncio.run(collect(AnthropicProvider(), request()))
    finally:
        MODE["anthropic_status"] = 200


def test_gemini_stream(fake_server, monkeypatch):
    from app.providers.gemini_provider import GeminiProvider

    monkeypatch.setattr(config, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(config, "GEMINI_BASE_URL", fake_server)
    monkeypatch.setattr(config, "GEMINI_THINKING_LEVEL", "low")
    MODE["gemini_finish"] = "STOP"

    async def scenario():
        # google-genai 클라이언트는 이벤트 루프 하나에 묶이므로 모든 호출을 한 루프에서 한다
        provider = GeminiProvider()
        thinking = [ev.text for ev in await stream_events(provider, request(model="gemini-3.8-flash"))
                    if ev.type == "thinking"]
        assert thinking == ["생각 중…"]
        first = await collect(provider, request(model="gemini-3.8-flash"))
        first_body = RECORDED[-1]
        MODE["gemini_finish"] = "SAFETY"
        blocked = await collect(provider, request(model="gemini-3.8-flash"))
        MODE["gemini_finish"] = "STOP"
        summary = await provider.complete("sys", "user", "gemini-3.8-flash", 100)
        return first, first_body, blocked, summary

    (text, done), recorded, (_, blocked), summary = asyncio.run(scenario())
    assert text == "삐빅! 환영해요."  # thought 파트는 빠진다
    assert done.finish_reason == "stop"
    assert done.usage.input_total == 5000 and done.usage.cache_read == 4096
    assert done.usage.output == 30 and done.usage.thinking == 15
    assert recorded["path"] == "gemini-3.8-flash:streamGenerateContent" and recorded["query"].get("alt") == "sse"
    body = recorded["body"]
    assert [c["role"] for c in body["contents"]] == ["user", "model", "user"]
    assert "줄거리" in body["systemInstruction"]["parts"][0]["text"]
    cfg = body["generationConfig"]
    assert cfg["maxOutputTokens"] == 700 + 2048 and cfg["temperature"] == 0.9
    thinking = cfg["thinkingConfig"]  # SDK 2.29는 thinking_level(스네이크 표기)로 보낸다
    assert (thinking.get("thinkingLevel") or thinking.get("thinking_level")) == "LOW"
    assert len(body["safetySettings"]) == 4
    assert blocked.finish_reason == "safety"
    assert summary.text == "요약"


def test_openai_compat_stream(fake_server, monkeypatch):
    from app.providers.openai_compat import OpenAICompatProvider

    monkeypatch.setattr(config, "OPENAI_COMPAT_BASE_URL", f"{fake_server}/v1")
    monkeypatch.setattr(config, "OPENAI_COMPAT_API_KEY", None)
    monkeypatch.setattr(config, "OPENAI_COMPAT_REASONING_EFFORT", None)
    monkeypatch.setattr(config, "OPENAI_COMPAT_TOP_P", None)
    MODE["openai_stream"] = "think_tags"
    provider = OpenAICompatProvider()
    text, done = asyncio.run(collect(provider, request(model="local-model")))
    assert text == "안녕하세요"
    assert done.usage.input_total == 900 and done.usage.cache_read == 640 and done.usage.output == 7
    assert done.usage.raw["thinking_estimated"] is True
    body = RECORDED[-1]["body"]
    assert body["messages"][0]["role"] == "system" and "줄거리" in body["messages"][0]["content"]
    assert body["max_tokens"] == 700 and body["stream_options"] == {"include_usage": True}
    assert body["temperature"] == 0.9 and "top_p" not in body and "reasoning_effort" not in body
    assert asyncio.run(provider.complete("sys", "user", "local-model", 100)).text == "요약"


def test_openai_compat_reasoning_field_and_options(fake_server, monkeypatch):
    from app.providers.openai_compat import OpenAICompatProvider

    monkeypatch.setattr(config, "OPENAI_COMPAT_BASE_URL", f"{fake_server}/v1")
    monkeypatch.setattr(config, "OPENAI_COMPAT_API_KEY", None)
    monkeypatch.setattr(config, "OPENAI_COMPAT_REASONING_EFFORT", "none")
    monkeypatch.setattr(config, "OPENAI_COMPAT_TOP_P", 0.95)
    MODE["openai_stream"] = "reasoning"
    try:
        provider = OpenAICompatProvider()
        events = asyncio.run(stream_events(provider, request(model="local-model")))
        body = RECORDED[-1]["body"]
        asyncio.run(provider.complete("sys", "user", "local-model", 100))
        complete_body = RECORDED[-1]["body"]
    finally:
        MODE["openai_stream"] = "think_tags"
    kinds = [ev.type for ev in events]
    assert "".join(ev.text for ev in events if ev.type == "thinking").startswith("생각 중…")
    assert kinds.index("reset") > kinds.index("text")  # 초안이 먼저 보였다가 지워진다
    assert "".join(ev.text for ev in events[kinds.index("reset"):] if ev.type == "text") == "안녕하세요"
    assert body["reasoning_effort"] == "none" and body["top_p"] == 0.95
    assert complete_body["reasoning_effort"] == "none" and complete_body["top_p"] == 0.95


# ── 설정의 제공사 옵션 덮어쓰기(provider_options) ──────────────
# 키가 있으면 .env 값 대신 그 값을 쓰고, None이면 .env에 값이 있어도 보내지 않는다(모델 기본값).

def test_openai_compat_option_overrides(fake_server, monkeypatch):
    from app.providers.openai_compat import OpenAICompatProvider

    monkeypatch.setattr(config, "OPENAI_COMPAT_BASE_URL", f"{fake_server}/v1")
    monkeypatch.setattr(config, "OPENAI_COMPAT_API_KEY", None)
    monkeypatch.setattr(config, "OPENAI_COMPAT_REASONING_EFFORT", "none")
    monkeypatch.setattr(config, "OPENAI_COMPAT_TOP_P", 0.95)
    provider = OpenAICompatProvider()
    overrides = {"reasoning_effort": "low", "top_p": None, "presence_penalty": 1.5}
    body = provider.render(request(model="local-model", options=overrides))
    assert body["reasoning_effort"] == "low" and "top_p" not in body and body["presence_penalty"] == 1.5
    assert provider.options(overrides) == {"reasoning_effort": "low", "top_p": None, "presence_penalty": 1.5,
                                           "max_tokens_param": "max_tokens"}
    assert provider.options() == {"reasoning_effort": "none", "top_p": 0.95, "presence_penalty": None,
                                  "max_tokens_param": "max_tokens"}
    assert "presence_penalty" not in provider.render(request(model="local-model"))  # .env 값이 없어 보내지 않는다
    asyncio.run(provider.complete("sys", "user", "local-model", 100, options={"reasoning_effort": None}))
    sent = RECORDED[-1]["body"]  # 요약·검수 호출에도 같은 덮어쓰기가 들어간다
    assert "reasoning_effort" not in sent and sent["top_p"] == 0.95


def test_anthropic_option_overrides(monkeypatch):
    from app.providers.anthropic_provider import AnthropicProvider

    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(config, "ANTHROPIC_EFFORT", "low")
    monkeypatch.setattr(config, "ANTHROPIC_THINKING", None)
    provider = AnthropicProvider()
    body = provider.render(request(options={"effort": "high", "thinking": "disabled"}))
    assert body["output_config"] == {"effort": "high"} and body["thinking"] == {"type": "disabled"}
    assert body["max_tokens"] == 700  # 사고를 끄면 사고 여유분을 더하지 않는다
    assert "output_config" not in provider.render(request(options={"effort": None}))
    assert provider.options({"thinking": "disabled"}) == {"effort": "low", "thinking": "disabled", "thinking_headroom": 0}
    assert provider.options()["thinking_headroom"] == 2048


def test_gemini_option_overrides(fake_server, monkeypatch):
    from app.providers.gemini_provider import GeminiProvider

    monkeypatch.setattr(config, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(config, "GEMINI_BASE_URL", fake_server)
    monkeypatch.setattr(config, "GEMINI_THINKING_LEVEL", "low")
    provider = GeminiProvider()
    assert provider.render(request(options={"thinking_level": "high"}))["config"]["thinking_config"] == {"thinking_level": "high"}
    assert "thinking_config" not in provider.render(request(options={"thinking_level": None}))["config"]
    assert provider.options({"thinking_level": "minimal"})["thinking_level"] == "minimal"
    assert provider.options()["thinking_level"] == "low"
