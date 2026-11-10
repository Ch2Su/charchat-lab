"""OpenAI 호환 엔드포인트 어댑터 (Ollama, LM Studio, llama.cpp server, vLLM, OpenAI 등).

- Ollama 예: OPENAI_COMPAT_BASE_URL=http://localhost:11434/v1
- 로컬 서버는 앞부분이 같으면 KV 캐시를 재사용해 첫 토큰이 빨라진다. 사용량에 캐시 토큰이
  보고되지 않더라도 화면의 '첫 토큰' 시간으로 효과를 볼 수 있다.
- 추론형 모델의 사고는 화면과 저장 내용에서 숨기고 분량과 시간만 잰다. 서버가 사고를 따로
  보내면(Ollama는 reasoning, vLLM·LM Studio는 reasoning_content) 그 필드를 읽고, 본문에
  <think>…</think>로 섞여 오면 걸러 낸다.
- Ollama의 /v1은 temperature·top_p를 안 보내면 모델 기본값 대신 1.0을 쓴다.
  OPENAI_COMPAT_TOP_P로 모델 기본값(ollama show로 확인)을 맞춰 보낼 수 있다.
"""
from __future__ import annotations

from typing import AsyncIterator

from .. import config
from ..tokens import estimate_tokens
from .base import ChatRequest, CompleteResult, Provider, ProviderError, StreamEvent, Usage

FINISH = {"stop": "stop", "length": "length", "content_filter": "safety"}


def _partial_suffix(text: str, tag: str) -> int:
    for size in range(min(len(tag) - 1, len(text)), 0, -1):
        if text.endswith(tag[:size]):
            return size
    return 0


def _think(events: list[tuple[str, str]], text: str) -> None:
    if text:
        events.append(("thinking", text))


class ThinkSplitter:
    """본문 스트림을 사고("thinking")와 답변("text")으로 나눈다. 태그가 청크 경계에 걸쳐도 동작한다.

    여는 태그 없이 </think>만 오면 그 앞은 전부 사고로 본다. Qwen·DeepSeek 계열은 채팅 템플릿이
    <think>를 프롬프트 쪽에 넣어 두기 때문에, 서버가 사고를 나눠 주지 않으면 닫는 태그만 보인다.
    그 전에 답변으로 내보낸 글이 있으면 ("reset", "")을 내서 받는 쪽이 지우게 한다.
    """

    OPEN, CLOSE = "<think>", "</think>"

    def __init__(self) -> None:
        self.buf = ""
        self.inside = False
        self.shown = 0  # 마지막 reset 이후 답변으로 내보낸 글자 수
        self._strip_next = False

    def _text(self, events: list[tuple[str, str]], text: str) -> None:
        if self._strip_next:
            text = text.lstrip()
            if not text:
                return
            self._strip_next = False
        if text:
            events.append(("text", text))
            self.shown += len(text)

    def feed(self, text: str) -> list[tuple[str, str]]:
        self.buf += text
        events: list[tuple[str, str]] = []
        while self.buf:
            if self.inside:
                index = self.buf.find(self.CLOSE)
                if index == -1:
                    keep = _partial_suffix(self.buf, self.CLOSE)
                    _think(events, self.buf[: len(self.buf) - keep])
                    self.buf = self.buf[len(self.buf) - keep:]
                    break
                _think(events, self.buf[:index])
                self.buf = self.buf[index + len(self.CLOSE):]
                self.inside = False
                self._strip_next = True
                continue
            opened, closed = self.buf.find(self.OPEN), self.buf.find(self.CLOSE)
            if closed != -1 and (opened == -1 or closed < opened):
                # 짝 없는 닫는 태그: 지금까지 답변으로 보인 글도 사실은 사고였다
                if self.shown:
                    events.append(("reset", ""))
                    self.shown = 0
                _think(events, self.buf[:closed])
                self.buf = self.buf[closed + len(self.CLOSE):]
                self._strip_next = True
                continue
            if opened != -1:
                self._text(events, self.buf[:opened])
                self.buf = self.buf[opened + len(self.OPEN):]
                self.inside = True
                continue
            keep = max(_partial_suffix(self.buf, self.OPEN), _partial_suffix(self.buf, self.CLOSE))
            self._text(events, self.buf[: len(self.buf) - keep])
            self.buf = self.buf[len(self.buf) - keep:]
            break
        return events

    def flush(self) -> list[tuple[str, str]]:
        rest, self.buf = self.buf, ""
        events: list[tuple[str, str]] = []
        if self.inside:
            _think(events, rest)
        else:
            self._text(events, rest)
        return events


def answer_text(events: list[tuple[str, str]]) -> str:
    """사고를 뺀 최종 답변(마지막 reset 이후의 text)."""
    parts: list[str] = []
    for kind, text in events:
        if kind == "reset":
            parts.clear()
        elif kind == "text":
            parts.append(text)
    return "".join(parts)


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

    @staticmethod
    def _values(overrides: dict | None) -> dict:
        """.env 값에 설정의 덮어쓰기를 얹는다. None은 보내지 않는다는 뜻이다."""
        values = {"reasoning_effort": config.OPENAI_COMPAT_REASONING_EFFORT, "top_p": config.OPENAI_COMPAT_TOP_P}
        values.update(overrides or {})
        return values

    def _options(self, payload: dict, temperature: float | None = None, overrides: dict | None = None) -> dict:
        values = self._values(overrides)
        if temperature is not None:
            payload["temperature"] = temperature
        if values["top_p"] is not None:
            payload["top_p"] = values["top_p"]
        if values["reasoning_effort"]:
            payload["reasoning_effort"] = values["reasoning_effort"]
        return payload

    def options(self, overrides: dict | None = None) -> dict:
        return {**self._values(overrides), "max_tokens_param": config.OPENAI_COMPAT_MAX_TOKENS_PARAM}

    def render(self, req: ChatRequest) -> dict:
        system = req.fixed_system + (f"\n\n{req.summary}" if req.summary else "")
        payload = self._options({
            "model": req.model,
            "messages": self._messages(system, req.history, req.final_user),
            config.OPENAI_COMPAT_MAX_TOKENS_PARAM: req.max_tokens,
            "stream": True,
        }, req.temperature, req.options)
        if config.OPENAI_COMPAT_STREAM_USAGE:
            payload["stream_options"] = {"include_usage": True}
        return payload

    async def stream(self, req: ChatRequest) -> AsyncIterator[StreamEvent]:
        import openai

        payload = self.render(req)
        splitter = ThinkSplitter()
        answer: list[str] = []
        thought = False
        usage, finish = None, None

        def convert(pairs: list[tuple[str, str]]) -> list[StreamEvent]:
            nonlocal thought
            out = []
            for kind, text in pairs:
                if kind == "text":
                    answer.append(text)
                else:
                    thought = True
                    if kind == "reset":
                        answer.clear()
                out.append(StreamEvent(kind, text=text))
            return out

        try:
            stream = await self.client.chat.completions.create(**payload)
            async for chunk in stream:
                if getattr(chunk, "usage", None):
                    usage = chunk.usage
                for choice in (chunk.choices or [])[:1]:
                    if choice.finish_reason:
                        finish = choice.finish_reason
                    delta = choice.delta
                    if delta is None:
                        continue
                    reasoning = getattr(delta, "reasoning", None) or getattr(delta, "reasoning_content", None)
                    if reasoning:
                        thought = True
                        yield StreamEvent("thinking", text=reasoning)
                    if delta.content:
                        for event in convert(splitter.feed(delta.content)):
                            yield event
        except openai.OpenAIError as exc:
            raise _friendly_error(exc) from exc
        for event in convert(splitter.flush()):
            yield event
        result = _usage(usage)
        if thought and not result.thinking and result.output:
            # 사고 토큰을 따로 보고하지 않는 서버(Ollama 등): 출력 토큰에서 답변 추정치를 빼서 어림한다
            result.thinking = max(0, result.output - estimate_tokens("".join(answer)))
            result.raw["thinking_estimated"] = True
        yield StreamEvent("done", usage=result, finish_reason=FINISH.get(finish or "stop", finish or "stop"))

    async def complete(self, system: str, user: str, model: str, max_tokens: int,
                       purpose: str = "", options: dict | None = None) -> CompleteResult:
        import openai

        try:
            response = await self.client.chat.completions.create(**self._options({
                "model": model,
                "messages": self._messages(system, [], user),
                config.OPENAI_COMPAT_MAX_TOKENS_PARAM: max_tokens,
            }, overrides=options))
        except openai.OpenAIError as exc:
            raise _friendly_error(exc) from exc
        text = (response.choices[0].message.content or "") if response.choices else ""
        splitter = ThinkSplitter()
        return CompleteResult(answer_text(splitter.feed(text) + splitter.flush()), _usage(response.usage))

    async def aclose(self) -> None:
        await self.client.close()
