"""비용 계산(USD, 100만 토큰당 가격).

Claude 가격은 공식 문서에서 2026-10-12에 확인한 값이다. 가격은 바뀔 수 있으니 실제 청구와 비교할
때는 문서를 다시 확인하자.
  https://platform.claude.com/docs/en/models/overview
  https://platform.claude.com/docs/en/build-with-claude/prompt-caching
캐시 배율: 5분 캐시 쓰기 1.25배, 읽기 기본 0.1배(Opus 5.5·Sonnet 5.5는 0.05배, Fable 5.1은 0.025배).
Haiku 5.5는 문서에 'From $0.10'으로 표기되어 있어 최저가 기준이다.

다른 모델은 프로젝트 루트에 pricing.json을 만들어 추가한다(README 참고).
mock은 Haiku 5.5 가격으로 '흉내 낸' 비용을 보여 준다.
"""
from __future__ import annotations

import json

from . import config

BUILTIN = {
    "anthropic:claude-fable-5-1": {"input": 10.0, "output": 50.0, "cache_write_mult": 1.25, "cache_read_mult": 0.025},
    "anthropic:claude-opus-5-5": {"input": 4.0, "output": 20.0, "cache_write_mult": 1.25, "cache_read_mult": 0.05},
    "anthropic:claude-sonnet-5-5": {"input": 2.0, "output": 10.0, "cache_write_mult": 1.25, "cache_read_mult": 0.05},
    "anthropic:claude-haiku-5-5": {"input": 0.10, "output": 0.50, "cache_write_mult": 1.25, "cache_read_mult": 0.10},
    "mock:*": {"input": 0.10, "output": 0.50, "cache_write_mult": 1.25, "cache_read_mult": 0.10},
}


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _overrides() -> dict:
    path = config.PRICING_OVERRIDE_PATH
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {}
    # 가격이 숫자가 아닌 항목(예시 파일의 null 자리표시자)은 없는 셈 친다. 0으로 읽혀 무료처럼 보이면 안 된다
    return {k: v for k, v in data.items()
            if isinstance(v, dict) and not k.startswith("_") and _is_number(v.get("input")) and _is_number(v.get("output"))}


def price_for(provider: str, model: str) -> dict | None:
    table = {**BUILTIN, **_overrides()}
    return table.get(f"{provider}:{model}") or table.get(f"{provider}:*")


def cost(usage: dict, provider: str, model: str) -> dict | None:
    price = price_for(provider, model)
    if not price or not usage:
        return None
    million = 1_000_000
    total_in = usage.get("input_total", 0)
    read = usage.get("cache_read", 0)
    write = usage.get("cache_write", 0)
    uncached = max(0, total_in - read - write)
    output = usage.get("output", 0)
    if provider == "gemini":  # Gemini는 사고 토큰을 출력과 따로 보고하지만 출력 가격으로 청구된다
        output += usage.get("thinking", 0)
    unit_in, unit_out = price["input"], price["output"]
    actual = (
        uncached * unit_in
        + write * unit_in * price.get("cache_write_mult", 1.0)
        + read * unit_in * price.get("cache_read_mult", 1.0)
        + output * unit_out
    ) / million
    baseline = (total_in * unit_in + output * unit_out) / million
    return {"usd": round(actual, 8), "no_cache_usd": round(baseline, 8)}
