"""제공사 레지스트리. SDK는 해당 제공사를 처음 쓸 때만 불러온다."""
from __future__ import annotations

from .. import config
from .base import ChatRequest, CompleteResult, Provider, ProviderError, StreamEvent, Usage

_instances: dict[str, Provider] = {}


def get_provider(name: str) -> Provider:
    if name not in config.PROVIDERS:
        raise ProviderError(f"알 수 없는 제공사: {name}")
    if not config.available_providers().get(name):
        raise ProviderError(f"'{name}' 제공사가 설정되지 않았어요. .env에 키나 주소를 넣고 서버를 다시 시작하세요.")
    if name not in _instances:
        if name == "mock":
            from .mock import MockProvider
            _instances[name] = MockProvider()
        elif name == "anthropic":
            from .anthropic_provider import AnthropicProvider
            _instances[name] = AnthropicProvider()
        elif name == "gemini":
            from .gemini_provider import GeminiProvider
            _instances[name] = GeminiProvider()
        elif name == "openai_compat":
            from .openai_compat import OpenAICompatProvider
            _instances[name] = OpenAICompatProvider()
    return _instances[name]


def clear_mock_cache() -> int:
    """mock의 흉내 캐시를 비운다. 지운 항목 수를 돌려준다."""
    provider = _instances.get("mock")
    if provider is None:
        return 0
    count = len(provider.cache.entries)
    provider.cache.entries.clear()
    return count


def reset_mock(seed: int | None = None) -> int:
    """대본 재생 실험의 변형마다 mock을 같은 상태에서 시작하게 한다.

    흉내 캐시를 비우고, seed가 있으면 답변 난수를 다시 심는다. 변형마다 같은 답변 순서가 나와야
    설정 차이만 비교할 수 있다. 지운 캐시 항목 수를 돌려준다.
    """
    provider = get_provider("mock")
    count = len(provider.cache.entries)
    provider.cache.entries.clear()
    if seed is not None:
        provider.rng.seed(seed)
    return count


async def close_all() -> None:
    for provider in list(_instances.values()):
        try:
            await provider.aclose()
        except Exception:  # pragma: no cover - 종료 중 오류는 무시
            pass
    _instances.clear()


__all__ = [
    "ChatRequest", "CompleteResult", "Provider", "ProviderError", "StreamEvent", "Usage",
    "get_provider", "close_all", "clear_mock_cache", "reset_mock",
]
