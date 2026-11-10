"""제공사 공통 인터페이스.

각 어댑터는 같은 ChatRequest를 받아 자기 API 형식으로 바꾸고(render),
스트리밍 결과를 공통 StreamEvent로 돌려준다(stream). 사용량도 공통 Usage로 맞춘다.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import AsyncIterator


class ProviderError(RuntimeError):
    """사용자에게 그대로 보여 줄 수 있는 오류."""


@dataclass
class Usage:
    input_total: int = 0   # 캐시 포함 전체 입력 토큰
    cache_read: int = 0    # 캐시에서 읽은 입력 토큰
    cache_write: int = 0   # 이번에 캐시에 쓴 입력 토큰(Anthropic만 보고)
    output: int = 0        # 출력 토큰(사고 토큰 포함 여부는 제공사마다 다름 → raw 참고)
    thinking: int = 0      # 사고(thinking) 토큰. 보고하는 제공사만
    raw: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class StreamEvent:
    # "text"     답변 조각
    # "thinking" 사고 조각(화면에 보이지 않음. 내용을 안 주는 제공사는 빈 문자열로 시점만 알린다)
    # "reset"    지금까지 보낸 text가 사실 사고였으니 지운다(짝 없는 </think>)
    # "done"     끝. usage와 finish_reason을 담는다
    type: str
    text: str = ""
    usage: Usage | None = None
    finish_reason: str | None = None  # stop | length | safety | refusal | error
    detail: str | None = None


@dataclass
class ChatRequest:
    model: str
    fixed_system: str
    summary: str | None
    history: list[dict]
    final_user: str
    max_tokens: int
    temperature: float | None
    cache_mode: str = "explicit"      # explicit | auto | off
    hints: dict = field(default_factory=dict)  # mock 제공사만 쓰는 힌트(캐릭터 이름 등)
    options: dict = field(default_factory=dict)  # 설정의 제공사 옵션 덮어쓰기(.env 값 위에 얹는다)


@dataclass
class CompleteResult:
    text: str
    usage: Usage


class Provider:
    name = "base"

    def render(self, req: ChatRequest) -> dict:
        """실제로 보낼 요청 본문. 화면의 '프롬프트 원문'에 그대로 보여 준다."""
        raise NotImplementedError

    def stream(self, req: ChatRequest) -> AsyncIterator[StreamEvent]:
        raise NotImplementedError

    async def complete(self, system: str, user: str, model: str, max_tokens: int,
                       purpose: str = "", options: dict | None = None) -> CompleteResult:
        """요약·검수용 단발 호출. options는 ChatRequest.options와 같은 덮어쓰기다."""
        raise NotImplementedError

    def options(self, overrides: dict | None = None) -> dict:
        """요청을 만들 때 쓰는 제공사 옵션(사고 수준 등): .env 값에 설정의 덮어쓰기를 얹은 것.
        실행 조건으로 턴마다 기록한다."""
        return {}

    async def aclose(self) -> None:
        return None
