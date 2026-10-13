"""Anthropic(Claude) 어댑터.

현행 Claude 모델(Haiku/Sonnet/Opus 5.5 등) 주의점 (2026-10 문서 기준)
- temperature·top_p·top_k를 기본값이 아닌 값으로 보내면 400 오류가 난다. 그래서 보내지 않는다.
- 사고(thinking)가 기본으로 켜져 있고, 사고 토큰도 max_tokens 안에 들어간다.
  답변이 잘리지 않도록 ANTHROPIC_THINKING_HEADROOM만큼 여유를 더한다.
- 캐시 최소 길이는 512토큰. 그보다 짧은 앞부분은 cache_control을 붙여도 캐시되지 않는다(오류 없음).
"""
from __future__ import annotations

from typing import AsyncIterator

from .. import config
from .anthropic_format import build_anthropic_payload
from .base import ChatRequest, CompleteResult, Provider, ProviderError, StreamEvent, Usage

STOP_REASONS = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "model_context_window_exceeded": "length",
    "refusal": "refusal",
}


def _friendly_error(exc: Exception) -> ProviderError:
    import anthropic

    if isinstance(exc, anthropic.AuthenticationError):
        return ProviderError("Anthropic API 키가 올바르지 않아요. .env의 ANTHROPIC_API_KEY를 확인하세요.")
    if isinstance(exc, anthropic.RateLimitError):
        return ProviderError("요청 한도에 걸렸어요(429). 잠시 후 다시 시도하세요.")
    if isinstance(exc, anthropic.APIStatusError):
        message = getattr(exc, "message", str(exc))
        return ProviderError(f"Anthropic API 오류 {exc.status_code}: {message}")
    if isinstance(exc, anthropic.APIConnectionError):
        return ProviderError("Anthropic API에 연결하지 못했어요. 네트워크를 확인하세요.")
    return ProviderError(f"Anthropic 호출 실패: {exc}")


def _usage(u) -> Usage:
    raw = u.model_dump(exclude_none=True) if hasattr(u, "model_dump") else dict(u)
    details = raw.get("output_tokens_details") or {}
    read = raw.get("cache_read_input_tokens") or 0
    write = raw.get("cache_creation_input_tokens") or 0
    return Usage(
        input_total=(raw.get("input_tokens") or 0) + read + write,
        cache_read=read,
        cache_write=write,
        output=raw.get("output_tokens") or 0,
        thinking=details.get("thinking_tokens") or 0,
        raw=raw,
    )


class AnthropicProvider(Provider):
    name = "anthropic"

    def __init__(self) -> None:
        import anthropic

        self.client = anthropic.AsyncAnthropic(
            api_key=config.ANTHROPIC_API_KEY,
            base_url=config.ANTHROPIC_BASE_URL or None,
            max_retries=2,
        )

    def _max_tokens(self, wanted: int) -> int:
        if config.ANTHROPIC_THINKING in ("disabled", "between_tools"):
            return wanted
        return wanted + max(0, config.ANTHROPIC_THINKING_HEADROOM)

    def _options(self, payload: dict) -> dict:
        if config.ANTHROPIC_EFFORT:
            payload["output_config"] = {"effort": config.ANTHROPIC_EFFORT}
        if config.ANTHROPIC_THINKING:
            payload["thinking"] = {"type": config.ANTHROPIC_THINKING}
        return payload

    def render(self, req: ChatRequest) -> dict:
        return self._options(build_anthropic_payload(req, self._max_tokens(req.max_tokens)))

    async def stream(self, req: ChatRequest) -> AsyncIterator[StreamEvent]:
        import anthropic

        payload = self.render(req)
        try:
            async with self.client.messages.stream(**payload) as stream:
                async for event in stream:
                    if event.type == "content_block_delta":
                        kind = getattr(event.delta, "type", "")
                        if kind == "text_delta" and event.delta.text:
                            yield StreamEvent("text", text=event.delta.text)
                        elif kind in ("thinking_delta", "signature_delta"):
                            # 사고 내용이 생략(omitted)돼도 시작과 끝 시점은 이 이벤트로 잴 수 있다
                            yield StreamEvent("thinking", text=getattr(event.delta, "thinking", "") or "")
                    elif event.type == "content_block_start" and \
                            getattr(event.content_block, "type", "") in ("thinking", "redacted_thinking"):
                        yield StreamEvent("thinking")
                final = await stream.get_final_message()
        except anthropic.AnthropicError as exc:
            raise _friendly_error(exc) from exc
        yield StreamEvent(
            "done",
            usage=_usage(final.usage),
            finish_reason=STOP_REASONS.get(final.stop_reason or "", final.stop_reason or "stop"),
        )

    async def complete(self, system: str, user: str, model: str, max_tokens: int,
                       purpose: str = "") -> CompleteResult:
        import anthropic

        payload = self._options({
            "model": model,
            "max_tokens": self._max_tokens(max_tokens),
            "system": system,
            "messages": [{"role": "user", "content": user}],
        })
        try:
            message = await self.client.messages.create(**payload)
        except anthropic.AnthropicError as exc:
            raise _friendly_error(exc) from exc
        text = "".join(block.text for block in message.content if getattr(block, "type", "") == "text")
        return CompleteResult(text, _usage(message.usage))

    async def aclose(self) -> None:
        await self.client.close()
