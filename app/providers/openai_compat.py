"""OpenAI 호환 엔드포인트 어댑터 (Ollama, LM Studio, llama.cpp server, vLLM, OpenAI 등).

- Ollama 예: OPENAI_COMPAT_BASE_URL=http://localhost:11434/v1
- 로컬 서버는 앞부분이 같으면 KV 캐시를 재사용해 첫 토큰이 빨라진다. 사용량에 캐시 토큰이
  보고되지 않더라도 화면의 '첫 토큰' 시간으로 효과를 볼 수 있다.
- <think>…</think>를 내보내는 추론형 모델은 그 부분을 화면과 저장 내용에서 숨긴다.
"""
from __future__ import annotations

from typing import AsyncIterator

from .. import config
from .base import ChatRequest, CompleteResult, Provider, ProviderError, StreamEvent, Usage

FINISH = {"stop": "stop", "length": "length", "content_filter": "safety"}


def _partial_suffix(text: str, tag: str) -> int:
    for size in range(min(len(tag) - 1, len(text)), 0, -1):
        if text.endswith(tag[:size]):
            return size
    return 0


class ThinkStripper:
    """스트림에서 <think>…</think> 구간을 걸러 낸다. 태그가 청크 경계에 걸쳐도 동작한다."""

    OPEN, CLOSE = "<think>", "</think>"

    def __init__(self) -> None:
        self.buf = ""
        self.inside = False
        self.hidden_chars = 0
        self._strip_next = False

    def feed(self, text: str) -> str:
        self.buf += text
        out: list[str] = []
        while self.buf:
            if self.inside:
                index = self.buf.find(self.CLOSE)
                if index == -1:
                    keep = _partial_suffix(self.buf, self.CLOSE)
                    self.hidden_chars += len(self.buf) - keep
                    self.buf = self.buf[len(self.buf) - keep:] if keep else ""
                    break
                self.hidden_chars += index
                self.buf = self.buf[index + len(self.CLOSE):]
                self.inside = False
                self._strip_next = True
            else:
                index = self.buf.find(self.OPEN)
                if index == -1:
                    keep = _partial_suffix(self.buf, self.OPEN)
                    out.append(self.buf[: len(self.buf) - keep])
                    self.buf = self.buf[len(self.buf) - keep:] if keep else ""
                    break
                out.append(self.buf[:index])
                self.buf = self.buf[index + len(self.OPEN):]
                self.inside = True
        result = "".join(out)
        if self._strip_next and result:
            result = result.lstrip()
            if result:
                self._strip_next = False
        return result

    def flush(self) -> str:
        rest = "" if self.inside else self.buf
        self.buf = ""
        return rest.lstrip() if self._strip_next else rest


def _usage(u) -> Usage:
    if u is None:
        return Usage()
    raw = u.model_dump(exclude_none=True) if hasattr(u, "model_dump") else dict(u)
    prompt_details = raw.get("prompt_tokens_details") or {}
    completion_details = raw.get("completion_tokens_details") or {}
    return Usage(
        input_total=raw.get("prompt_tokens") or 0,
        cache_read=prompt_details.get("cached_tokens") or 0,
        output=raw.get("completion_tokens") or 0,
        thinking=completion_details.get("reasoning_tokens") or 0,
        raw=raw,
    )


def _friendly_error(exc: Exception) -> ProviderError:
    import openai

    if isinstance(exc, openai.APIConnectionError):
        return ProviderError(
            f"{config.OPENAI_COMPAT_BASE_URL}에 연결하지 못했어요. 로컬 서버(Ollama 등)가 켜져 있는지 확인하세요."
        )
    if isinstance(exc, openai.NotFoundError):
        return ProviderError("모델이나 경로를 찾지 못했어요(404). 모델 이름과 BASE_URL(/v1 포함)을 확인하세요.")
    if isinstance(exc, openai.APIStatusError):
        return ProviderError(f"API 오류 {exc.status_code}: {getattr(exc, 'message', exc)}")
    return ProviderError(f"OpenAI 호환 호출 실패: {exc}")


class OpenAICompatProvider(Provider):
    name = "openai_compat"

    def __init__(self) -> None:
        from openai import AsyncOpenAI

        self.client = AsyncOpenAI(
            base_url=config.OPENAI_COMPAT_BASE_URL,
            api_key=config.OPENAI_COMPAT_API_KEY or "not-needed",
            max_retries=1,
        )

    def _messages(self, system: str, history: list[dict], final_user: str) -> list[dict]:
        messages = [{"role": "system", "content": system}]
        messages += [{"role": m["role"], "content": m["content"]} for m in history]
        messages.append({"role": "user", "content": final_user})
        return messages

    def render(self, req: ChatRequest) -> dict:
        system = req.fixed_system + (f"\n\n{req.summary}" if req.summary else "")
        payload = {
            "model": req.model,
            "messages": self._messages(system, req.history, req.final_user),
            config.OPENAI_COMPAT_MAX_TOKENS_PARAM: req.max_tokens,
            "stream": True,
        }
        if req.temperature is not None:
            payload["temperature"] = req.temperature
        if config.OPENAI_COMPAT_STREAM_USAGE:
            payload["stream_options"] = {"include_usage": True}
        return payload

    async def stream(self, req: ChatRequest) -> AsyncIterator[StreamEvent]:
        import openai

        payload = self.render(req)
        stripper = ThinkStripper()
        usage, finish = None, None
        try:
            stream = await self.client.chat.completions.create(**payload)
            async for chunk in stream:
                if getattr(chunk, "usage", None):
                    usage = chunk.usage
                for choice in (chunk.choices or [])[:1]:
                    if choice.finish_reason:
                        finish = choice.finish_reason
                    piece = getattr(choice.delta, "content", None) if choice.delta else None
                    if piece:
                        visible = stripper.feed(piece)
                        if visible:
                            yield StreamEvent("text", text=visible)
        except openai.OpenAIError as exc:
            raise _friendly_error(exc) from exc
        tail = stripper.flush()
        if tail:
            yield StreamEvent("text", text=tail)
        result = _usage(usage)
        if stripper.hidden_chars:
            result.raw["hidden_think_chars"] = stripper.hidden_chars
        yield StreamEvent("done", usage=result, finish_reason=FINISH.get(finish or "stop", finish or "stop"))

    async def complete(self, system: str, user: str, model: str, max_tokens: int,
                       purpose: str = "") -> CompleteResult:
        import openai

        try:
            response = await self.client.chat.completions.create(
                model=model,
                messages=self._messages(system, [], user),
                **{config.OPENAI_COMPAT_MAX_TOKENS_PARAM: max_tokens},
            )
        except openai.OpenAIError as exc:
            raise _friendly_error(exc) from exc
        text = (response.choices[0].message.content or "") if response.choices else ""
        stripper = ThinkStripper()
        text = stripper.feed(text) + stripper.flush()
        return CompleteResult(text, _usage(response.usage))

    async def aclose(self) -> None:
        await self.client.close()
