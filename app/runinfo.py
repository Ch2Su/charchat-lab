"""턴과 요약에 남기는 실행 조건.

비교 탭은 이 값으로 행을 나눈다. 화면의 설정만 남기면 .env로만 바꾸는 사고 설정이나
prompts/*.txt를 고친 전후가 한 행에 섞이므로, 제공사 옵션과 프롬프트 파일 지문까지 함께 남긴다.
chat_service와 memory가 함께 쓰므로 순환 import를 피해 따로 둔다.
"""
from __future__ import annotations

from .config import Settings
from .prompt import prompts_fingerprint
from .providers import Provider


def run_info(settings: Settings, provider: Provider) -> dict:
    return {
        "label": settings["run_label"] or None,
        "provider": provider.name,
        "model": settings.model,
        "strategy": settings["window_strategy"],
        "cache_mode": settings["cache_mode"],
        "input_token_budget": settings["input_token_budget"],
        "max_output_tokens": settings["max_output_tokens"],
        "temperature": settings["temperature"],
        "moderation_mode": settings["moderation_mode"],
        "moderation_backend": settings["moderation_backend"],
        "output_buffer": settings["output_buffer"],
        "options": provider.options(),  # .env로만 바꾸는 제공사 옵션
        "prompt_version": prompts_fingerprint(),
    }
