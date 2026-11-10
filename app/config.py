"""환경 변수(.env)와 런타임 설정.

- 정적 설정: API 키, 엔드포인트, 제공사별 세부 옵션. .env에서만 바꾼다.
- 런타임 설정: 화면의 '설정'에서 바꿀 수 있는 값. DB의 settings 테이블에 저장되고
  .env 값은 처음 기본값으로만 쓰인다.
- 실험용 덮어쓰기: 제공사 옵션(provider_options)과 프롬프트 파일 내용(prompt_overrides)도 런타임 설정이라
  대본 재생 실험의 변형마다 바꿀 수 있다. 설정 화면에는 나오지 않는다.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env", encoding="utf-8")


def env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def env_int(name: str, default: int) -> int:
    value = env(name)
    try:
        return int(value) if value is not None else default
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    value = env(name)
    try:
        return float(value) if value is not None else default
    except ValueError:
        return default


def env_float_or_none(name: str) -> float | None:
    value = env(name)
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


def env_bool(name: str, default: bool) -> bool:
    value = env(name)
    if value is None:
        return default
    return value.lower() in ("1", "true", "yes", "on")


# ── 경로 ──────────────────────────────────────────────
DATA_DIR = Path(env("DATA_DIR", str(ROOT / "data")))
DB_PATH = Path(env("DB_PATH", str(DATA_DIR / "lab.sqlite3")))
PROMPTS_DIR = ROOT / "prompts"
CHARACTERS_DIR = ROOT / "characters"
STATIC_DIR = ROOT / "static"
RULES_PATH = ROOT / "moderation_rules.json"
PRICING_OVERRIDE_PATH = ROOT / "pricing.json"

HOST = env("HOST", "127.0.0.1")
PORT = env_int("PORT", 8000)

# ── 제공사 ────────────────────────────────────────────
PROVIDERS = ("mock", "anthropic", "gemini", "openai_compat")

ANTHROPIC_API_KEY = env("ANTHROPIC_API_KEY")
ANTHROPIC_BASE_URL = env("ANTHROPIC_BASE_URL")
# 현행 Claude 모델은 사고(thinking)가 기본으로 켜져 있다. effort를 낮추면 덜 생각하고 더 빨리 답한다.
ANTHROPIC_EFFORT = env("ANTHROPIC_EFFORT", "low")
# 비우면 모델 기본값. Haiku 5.5는 "disabled", Sonnet 5.5는 "between_tools"로 사고를 끌 수 있다.
ANTHROPIC_THINKING = env("ANTHROPIC_THINKING")
# 사고 토큰도 max_tokens에 포함되므로 답변 길이와 별도로 여유분을 더한다.
ANTHROPIC_THINKING_HEADROOM = env_int("ANTHROPIC_THINKING_HEADROOM", 2048)

GEMINI_API_KEY = env("GEMINI_API_KEY") or env("GOOGLE_API_KEY")
GEMINI_BASE_URL = env("GEMINI_BASE_URL")
GEMINI_THINKING_LEVEL = env("GEMINI_THINKING_LEVEL")  # minimal|low|medium|high (모델마다 지원 범위가 다름)
GEMINI_THINKING_HEADROOM = env_int("GEMINI_THINKING_HEADROOM", 2048)
# Gemini 2.5·3 계열은 조정 가능한 안전 필터가 기본으로 꺼져 있다. 전체이용가 실험이므로 기본으로 켠다.
GEMINI_SAFETY_THRESHOLD = env("GEMINI_SAFETY_THRESHOLD", "BLOCK_MEDIUM_AND_ABOVE")

OPENAI_COMPAT_BASE_URL = env("OPENAI_COMPAT_BASE_URL")
OPENAI_COMPAT_API_KEY = env("OPENAI_COMPAT_API_KEY")
OPENAI_COMPAT_MAX_TOKENS_PARAM = env("OPENAI_COMPAT_MAX_TOKENS_PARAM", "max_tokens")
OPENAI_COMPAT_STREAM_USAGE = env_bool("OPENAI_COMPAT_STREAM_USAGE", True)
# 추론형 모델의 사고 수준(none이면 끔). 비우면 보내지 않아 모델 기본값을 따른다.
OPENAI_COMPAT_REASONING_EFFORT = env("OPENAI_COMPAT_REASONING_EFFORT")
# 비우면 보내지 않는다. Ollama의 /v1은 top_p가 없으면 모델 기본값 대신 1.0을 쓴다.
OPENAI_COMPAT_TOP_P = env_float_or_none("OPENAI_COMPAT_TOP_P")

DEFAULT_MODELS = {
    "mock": env("MOCK_MODEL", "mock-1"),
    "anthropic": env("ANTHROPIC_MODEL", "claude-haiku-5-5"),
    "gemini": env("GEMINI_MODEL", "gemini-3.8-flash"),
    "openai_compat": env("OPENAI_COMPAT_MODEL", ""),
}

# 요약·검수용 모델(비우면 대화 모델을 그대로 쓴다)
SUMMARY_MODEL = env("SUMMARY_MODEL")
MODERATION_MODEL = env("MODERATION_MODEL")

# ── mock 제공사 ───────────────────────────────────────
MOCK_DELAY_SCALE = env_float("MOCK_DELAY_SCALE", 1.0)
MOCK_CACHE_MIN_TOKENS = env_int("MOCK_CACHE_MIN_TOKENS", 512)
MOCK_CACHE_TTL_SECONDS = env_int("MOCK_CACHE_TTL_SECONDS", 300)

# ── 토큰 추정 ─────────────────────────────────────────
TOKEN_HANGUL_RATIO = env_float("TOKEN_HANGUL_RATIO", 1.0)


def available_providers() -> dict[str, bool]:
    return {
        "mock": True,
        "anthropic": bool(ANTHROPIC_API_KEY),
        "gemini": bool(GEMINI_API_KEY),
        "openai_compat": bool(OPENAI_COMPAT_BASE_URL),
    }


# ── 런타임 설정 명세 ──────────────────────────────────
def _initial_provider() -> str:
    wanted = env("PROVIDER", "mock")
    return wanted if wanted in PROVIDERS else "mock"


SETTINGS_SPEC: dict[str, dict] = {
    "provider": {"type": "choice", "choices": list(PROVIDERS), "default": _initial_provider()},
    "models": {"type": "models", "default": {}},
    "temperature": {"type": "float_or_none", "min": 0.0, "max": 2.0, "default": env_float_or_none("TEMPERATURE")},
    "max_output_tokens": {"type": "int", "min": 50, "max": 8192, "default": env_int("MAX_OUTPUT_TOKENS", 700)},
    "input_token_budget": {"type": "int", "min": 500, "max": 2_000_000, "default": env_int("INPUT_TOKEN_BUDGET", 6000)},
    "window_strategy": {"type": "choice", "choices": ["anchored", "sliding", "full"], "default": env("WINDOW_STRATEGY", "anchored")},
    "summary_trigger_ratio": {"type": "float", "min": 0.3, "max": 1.0, "default": env_float("SUMMARY_TRIGGER_RATIO", 0.85)},
    "summary_keep_ratio": {"type": "float", "min": 0.1, "max": 0.9, "default": env_float("SUMMARY_KEEP_RATIO", 0.5)},
    "cache_mode": {"type": "choice", "choices": ["explicit", "auto", "off"], "default": env("CACHE_MODE", "explicit")},
    "lore_scan_depth": {"type": "int", "min": 1, "max": 50, "default": env_int("LORE_SCAN_DEPTH", 4)},
    "lore_token_budget": {"type": "int", "min": 0, "max": 20000, "default": env_int("LORE_TOKEN_BUDGET", 400)},
    "moderation_mode": {"type": "choice", "choices": ["block", "log", "off"], "default": env("MODERATION_MODE", "block")},
    "moderation_backend": {"type": "choice", "choices": ["rules", "llm"], "default": env("MODERATION_BACKEND", "rules")},
    "output_buffer": {"type": "choice", "choices": ["sentence", "none"], "default": env("OUTPUT_BUFFER", "sentence")},
    # 실험 라벨: 비교 탭에서 이 이름으로 행을 나눈다
    "run_label": {"type": "text", "max_len": 40, "default": ""},
    # 실험용 덮어쓰기(설정 화면에는 없다). 비우면 .env 값과 prompts/*.txt를 그대로 쓴다
    "provider_options": {"type": "provider_options", "default": {}},
    "prompt_overrides": {"type": "prompt_overrides", "default": {}},
}

# 변형마다 바꿔 볼 수 있는 제공사 옵션과 값의 종류. 키가 없으면 .env 값을 쓰고, 값이 None이면 .env에 값이
# 있어도 보내지 않는다(모델 기본값). word는 영문 소문자·밑줄로 된 값(API가 받는지는 API가 판단), unit은 0~1 숫자
PROVIDER_OPTION_SPEC: dict[str, dict[str, str]] = {
    "anthropic": {"effort": "word", "thinking": "word"},
    "gemini": {"thinking_level": "word"},
    "openai_compat": {"reasoning_effort": "word", "top_p": "unit"},
}
_OPTION_WORD = re.compile(r"[a-z_]{1,24}")

# 덮어쓴 프롬프트에 꼭 있어야 하는 자리표시자. 빠지면 사용자의 입력이나 요약할 대화가 요청에서 사라진다
REQUIRED_PLACEHOLDERS = {"final_user.txt": ("{input}",), "summary_user.txt": ("{dialogue}",)}
PROMPT_OVERRIDE_MAX_CHARS = 20000
MERGED_SETTINGS = ("models", "provider_options", "prompt_overrides")


def prompt_file_name(name) -> str:
    """'final_user'와 'final_user.txt'를 같은 파일 이름으로 맞춘다."""
    name = str(name).strip()
    return name if name.endswith(".txt") else f"{name}.txt"


def provider_overrides(settings: dict, provider: str) -> dict:
    """설정에 적힌 그 제공사의 옵션 덮어쓰기. 없으면 빈 dict(.env 값을 그대로 쓴다)."""
    return dict((settings.get("provider_options") or {}).get(provider) or {})


def _coerce_option(path: str, kind: str, value):
    if value is None or value == "":
        return None
    if kind == "word":
        text = str(value).strip().lower()
        if not _OPTION_WORD.fullmatch(text):
            raise SettingsError(f"{path}: 영문 소문자와 밑줄로 된 값이어야 합니다(보내지 않으려면 null)")
        return text
    if isinstance(value, bool):
        raise SettingsError(f"{path}: 0~1 사이 숫자여야 합니다(보내지 않으려면 null)")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise SettingsError(f"{path}: 0~1 사이 숫자여야 합니다(보내지 않으려면 null)") from None
    if not 0 <= number <= 1:
        raise SettingsError(f"{path}: 0~1 사이 숫자여야 합니다(보내지 않으려면 null)")
    return number


def _coerce_provider_options(value) -> dict:
    if not isinstance(value, dict):
        raise SettingsError('provider_options: {"제공사": {"옵션": 값}} 형태여야 합니다')
    result = {}
    for provider, options in value.items():
        allowed = PROVIDER_OPTION_SPEC.get(provider)
        if allowed is None:
            raise SettingsError(f"provider_options: 옵션을 바꿀 수 없는 제공사예요({provider}). "
                                f"{', '.join(PROVIDER_OPTION_SPEC)} 중 하나여야 합니다")
        if options is None:  # 그 제공사의 덮어쓰기를 모두 지운다
            continue
        if not isinstance(options, dict):
            raise SettingsError(f"provider_options.{provider}: {{옵션: 값}} 형태여야 합니다")
        clean = {}
        for name, option in options.items():
            if name not in allowed:
                raise SettingsError(f"provider_options.{provider}: 알 수 없는 옵션이에요({name}). "
                                    f"{', '.join(allowed)} 중 하나여야 합니다")
            clean[name] = _coerce_option(f"provider_options.{provider}.{name}", allowed[name], option)
        if clean:
            result[provider] = clean
    return result


def _coerce_prompt_overrides(value) -> dict:
    if not isinstance(value, dict):
        raise SettingsError('prompt_overrides: {"파일 이름": "내용"} 형태여야 합니다')
    known = sorted(path.name for path in PROMPTS_DIR.glob("*.txt"))
    result = {}
    for raw_name, text in value.items():
        name = prompt_file_name(raw_name)
        if name not in known:
            raise SettingsError(f"prompt_overrides: prompts 폴더에 없는 파일이에요({name}). {', '.join(known)} 중 하나여야 합니다")
        if text is None:  # 덮어쓰기를 지운다
            continue
        if not isinstance(text, str) or not text.strip():
            raise SettingsError(f"prompt_overrides.{name}: 비어 있지 않은 글이어야 합니다(지우려면 null)")
        text = text.replace("\r\n", "\n").strip()  # load_prompt가 파일을 읽는 모양과 맞춘다
        if len(text) > PROMPT_OVERRIDE_MAX_CHARS:
            raise SettingsError(f"prompt_overrides.{name}: {PROMPT_OVERRIDE_MAX_CHARS}자를 넘을 수 없어요")
        missing = [p for p in REQUIRED_PLACEHOLDERS.get(name, ()) if p not in text]
        if missing:
            raise SettingsError(f"prompt_overrides.{name}: {', '.join(missing)} 자리표시자가 있어야 해요")
        result[name] = text
    return result


class SettingsError(ValueError):
    pass


def coerce_setting(key: str, value):
    spec = SETTINGS_SPEC.get(key)
    if spec is None:
        raise SettingsError(f"알 수 없는 설정: {key}")
    kind = spec["type"]
    if kind == "choice":
        if value not in spec["choices"]:
            raise SettingsError(f"{key}: {spec['choices']} 중 하나여야 합니다")
        return value
    if kind == "int":
        try:
            number = int(value)
        except (TypeError, ValueError):
            raise SettingsError(f"{key}: 정수여야 합니다") from None
        return max(spec["min"], min(spec["max"], number))
    if kind in ("float", "float_or_none"):
        if kind == "float_or_none" and (value is None or value == ""):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise SettingsError(f"{key}: 숫자여야 합니다") from None
        return max(spec["min"], min(spec["max"], number))
    if kind == "models":
        if not isinstance(value, dict):
            raise SettingsError("models: {제공사: 모델} 형태여야 합니다")
        return {k: str(v).strip() for k, v in value.items() if k in PROVIDERS}
    if kind == "text":
        return "" if value is None else str(value).strip()[: spec["max_len"]]
    if kind == "provider_options":
        return _coerce_provider_options(value)
    if kind == "prompt_overrides":
        return _coerce_prompt_overrides(value)
    raise SettingsError(f"{key}: 지원하지 않는 형식")


def merge_setting(key: str, current, value):
    """저장된 값에 새 값을 얹는다. 제공사별 모델·옵션과 프롬프트 덮어쓰기는 통째로 바꾸지 않고 합친다.

    합치는 설정에서 항목 값이 None이면 그 항목(제공사의 옵션 묶음, 프롬프트 파일)을 지운다. 제공사 옵션
    하나의 None은 '보내지 않음'이라는 값이라 지우지 않고 남긴다.
    """
    kind = SETTINGS_SPEC.get(key, {}).get("type")
    if key not in MERGED_SETTINGS or not isinstance(value, dict):
        return coerce_setting(key, value)
    merged = dict(current or {})
    for name, item in value.items():
        if kind == "prompt_overrides":
            name = prompt_file_name(name)
        if kind == "provider_options" and isinstance(item, dict) and isinstance(merged.get(name), dict):
            item = {**merged[name], **item}
        merged[name] = item
    return coerce_setting(key, merged)


class Settings(dict):
    """dict이면서 속성으로도 읽을 수 있는 설정 묶음."""

    def __getattr__(self, item):
        try:
            return self[item]
        except KeyError as exc:  # pragma: no cover - 방어 코드
            raise AttributeError(item) from exc

    @property
    def model(self) -> str:
        return model_for(self["provider"], self)


def model_for(provider: str, settings: dict) -> str:
    chosen = (settings.get("models") or {}).get(provider, "")
    return chosen or DEFAULT_MODELS.get(provider, "")


def build_settings(overrides: dict) -> Settings:
    merged = {}
    for key, spec in SETTINGS_SPEC.items():
        value = overrides.get(key, spec["default"])
        try:
            merged[key] = coerce_setting(key, value)
        except SettingsError:
            merged[key] = spec["default"]
    return Settings(merged)
