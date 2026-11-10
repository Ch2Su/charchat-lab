"""Google Gemini 어댑터 (google-genai SDK, generateContent API).

- system_instruction에 고정 프롬프트 + 요약을 넣고, 대화는 contents(user/model)로 보낸다.
- 캐시는 '암묵적 캐시'가 자동으로 동작한다(중단점 지정 없음). 앞부분이 같아야 적중하며,
  Gemini 3.8 Flash 기준 최소 4,096토큰 이상이어야 한다. 적중량은 cached_content_token_count로 온다.
- Gemini 3.x는 사고 토큰도 max_output_tokens에 포함되므로 GEMINI_THINKING_HEADROOM만큼 여유를 더한다.
- 조정 가능한 안전 필터는 기본으로 꺼져 있어서, GEMINI_SAFETY_THRESHOLD로 명시적으로 켠다.
"""
from __future__ import annotations

from typing import AsyncIterator

from .. import config
from .base import ChatRequest, CompleteResult, Provider, ProviderError, StreamEvent, Usage

SAFETY_CATEGORIES = (
    "HARM_CATEGORY_HARASSMENT",
    "HARM_CATEGORY_HATE_SPEECH",
    "HARM_CATEGORY_SEXUALLY_EXPLICIT",
    "HARM_CATEGORY_DANGEROUS_CONTENT",
)
SAFETY_FINISH = {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "IMAGE_SAFETY", "RECITATION"}


def _name(value) -> str:
    if value is None:
        return ""
    return getattr(value, "name", None) or str(value)


def _friendly_error(exc: Exception) -> ProviderError:
    code = getattr(exc, "code", None)
    message = getattr(exc, "message", None) or str(exc)
    if code in (401, 403):
        return ProviderError("Gemini API 키가 올바르지 않거나 권한이 없어요. .env의 GEMINI_API_KEY를 확인하세요.")
    if code == 429:
        return ProviderError("Gemini 요청 한도에 걸렸어요(429). 잠시 후 다시 시도하세요.")
    if code:
        return ProviderError(f"Gemini API 오류 {code}: {message}")
    return ProviderError(f"Gemini 호출 실패: {message}")


def _usage(meta) -> Usage:
    if meta is None:
        return Usage()
    raw = meta.model_dump(exclude_none=True, mode="json") if hasattr(meta, "model_dump") else dict(meta)
    return Usage(
        input_total=raw.get("prompt_token_count") or 0,
        cache_read=raw.get("cached_content_token_count") or 0,
        output=raw.get("candidates_token_count") or 0,
        thinking=raw.get("thoughts_token_count") or 0,
        raw=raw,
    )


class GeminiProvider(Provider):
    name = "gemini"

    def __init__(self) -> None:
        from google import genai
        from google.genai import types

        http_options = types.HttpOptions(base_url=config.GEMINI_BASE_URL) if config.GEMINI_BASE_URL else None
        self.client = genai.Client(api_key=config.GEMINI_API_KEY, http_options=http_options)

    @staticmethod
    def _values(overrides: dict | None) -> dict:
        """.env 값에 설정의 덮어쓰기를 얹는다. None은 보내지 않는다는 뜻이다."""
        values = {"thinking_level": config.GEMINI_THINKING_LEVEL}
        values.update(overrides or {})
        return values

    def _config(self, system: str, max_tokens: int, temperature: float | None, overrides: dict | None = None) -> dict:
        cfg: dict = {
            "system_instruction": system,
            "max_output_tokens": max_tokens + max(0, config.GEMINI_THINKING_HEADROOM),
        }
        if temperature is not None:
            cfg["temperature"] = temperature
        threshold = config.GEMINI_SAFETY_THRESHOLD
        if threshold and threshold.upper() != "DEFAULT":
            cfg["safety_settings"] = [{"category": c, "threshold": threshold} for c in SAFETY_CATEGORIES]
        level = self._values(overrides)["thinking_level"]
        if level:
            cfg["thinking_config"] = {"thinking_level": level}
        return cfg

    def options(self, overrides: dict | None = None) -> dict:
        return {
            "thinking_level": self._values(overrides)["thinking_level"],
            "thinking_headroom": max(0, config.GEMINI_THINKING_HEADROOM),
            "safety_threshold": config.GEMINI_SAFETY_THRESHOLD,
        }

    def render(self, req: ChatRequest) -> dict:
        system = req.fixed_system + (f"\n\n{req.summary}" if req.summary else "")
        contents = [
            {"role": "user" if m["role"] == "user" else "model", "parts": [{"text": m["content"]}]}
            for m in req.history
        ]
        contents.append({"role": "user", "parts": [{"text": req.final_user}]})
        return {"model": req.model, "contents": contents,
                "config": self._config(system, req.max_tokens, req.temperature, req.options)}

    async def stream(self, req: ChatRequest) -> AsyncIterator[StreamEvent]:
        from google.genai import errors

        payload = self.render(req)
        last_meta, finish, blocked = None, None, None
        try:
            stream = await self.client.aio.models.generate_content_stream(
                model=payload["model"], contents=payload["contents"], config=payload["config"]
            )
            async for chunk in stream:
                feedback = getattr(chunk, "prompt_feedback", None)
                if feedback is not None and getattr(feedback, "block_reason", None):
                    blocked = _name(feedback.block_reason)
                if getattr(chunk, "usage_metadata", None) is not None:
                    last_meta = chunk.usage_metadata
                for candidate in (chunk.candidates or [])[:1]:
                    if candidate.finish_reason:
                        finish = _name(candidate.finish_reason)
                    content = candidate.content
                    for part in (content.parts if content and content.parts else []):
                        if part.text:
                            yield StreamEvent("thinking" if part.thought else "text", text=part.text)
        except errors.APIError as exc:
            raise _friendly_error(exc) from exc
        if blocked:
            reason, detail = "safety", f"프롬프트 차단: {blocked}"
        elif finish in SAFETY_FINISH:
            reason, detail = "safety", f"응답 차단: {finish}"
        elif finish == "MAX_TOKENS":
            reason, detail = "length", None
        else:
            reason, detail = "stop", None
        yield StreamEvent("done", usage=_usage(last_meta), finish_reason=reason, detail=detail)

    async def complete(self, system: str, user: str, model: str, max_tokens: int,
                       purpose: str = "", options: dict | None = None) -> CompleteResult:
        from google.genai import errors

        try:
            response = await self.client.aio.models.generate_content(
                model=model, contents=user, config=self._config(system, max_tokens, None, options)
            )
        except errors.APIError as exc:
            raise _friendly_error(exc) from exc
        parts = []
        for candidate in (response.candidates or [])[:1]:
            for part in (candidate.content.parts if candidate.content and candidate.content.parts else []):
                if part.text and not part.thought:
                    parts.append(part.text)
        return CompleteResult("".join(parts), _usage(response.usage_metadata))
