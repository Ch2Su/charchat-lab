"""토큰 수 추정.

제공사마다 토크나이저가 달라 정확한 값은 응답의 usage로만 알 수 있다.
예산 계산에는 이 추정치를 쓰고, 화면에서 '추정 대비 실측' 비율을 보여 줘서
TOKEN_HANGUL_RATIO를 조정할 수 있게 한다.
"""
from __future__ import annotations

import math
import re

from . import config

_HANGUL = re.compile(r"[가-힣ㄱ-ㆎ]")
_CJK = re.compile(r"[぀-ヿ一-鿿]")
MESSAGE_OVERHEAD = 4  # 역할 표시 등 메시지 하나당 붙는 형식 토큰(대략값)


def estimate_tokens(text: str | None) -> int:
    if not text:
        return 0
    hangul = len(_HANGUL.findall(text))
    cjk = len(_CJK.findall(text))
    rest = len(text) - hangul - cjk
    return max(1, math.ceil(hangul * config.TOKEN_HANGUL_RATIO + cjk + rest / 4))


def estimate_message(text: str | None) -> int:
    return estimate_tokens(text) + MESSAGE_OVERHEAD
