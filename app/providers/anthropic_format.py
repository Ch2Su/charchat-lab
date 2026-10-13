"""Anthropic Messages API 형식의 요청 본문 만들기(SDK 없이 쓸 수 있는 순수 함수).

캐시 중단점(cache_control) 배치
  explicit: ① 고정 system 끝 ② 요약 끝 ③ 직전 대화 메시지 끝 ④ 마지막 사용자 메시지 끝 (최대 4개)
            ③ 덕분에 다음 턴에 '직전 턴까지의 대화'가 캐시에서 읽히고, ④ 덕분에 다시 생성하면
            프롬프트 전체가 캐시에서 읽힌다.
  auto    : 최상위 cache_control 하나만 둔다. 서버가 마지막 블록에 중단점을 둔다.
            이 앱처럼 마지막 메시지에 매 턴 다른 메모를 붙이면, 다음 턴에는 그 위치의 내용이
            달라져 있어서 거의 적중하지 않는다. 직접 비교해 보는 실험용 옵션이다.
  off     : 캐시를 쓰지 않는다.
"""
from __future__ import annotations

from .base import ChatRequest

EPHEMERAL = {"type": "ephemeral"}


def _text_block(text: str, cached: bool) -> dict:
    block = {"type": "text", "text": text}
    if cached:
        block["cache_control"] = dict(EPHEMERAL)
    return block


def build_anthropic_payload(req: ChatRequest, max_tokens: int) -> dict:
    explicit = req.cache_mode == "explicit"
    system = [_text_block(req.fixed_system, explicit)]
    if req.summary:
        system.append(_text_block(req.summary, explicit))
    messages = [{"role": m["role"], "content": [_text_block(m["content"], False)]} for m in req.history]
    if explicit and messages:
        messages[-1]["content"][-1]["cache_control"] = dict(EPHEMERAL)
    messages.append({"role": "user", "content": [_text_block(req.final_user, explicit)]})
    payload = {"model": req.model, "max_tokens": max_tokens, "system": system, "messages": messages}
    if req.cache_mode == "auto":
        payload["cache_control"] = dict(EPHEMERAL)
    return payload
