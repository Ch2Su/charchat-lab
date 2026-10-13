"""검수(모더레이션).

검사 지점: 캐릭터 카드 등록 / 사용자 입력 / 모델 출력.
방식:
  rules : moderation_rules.json의 정규식. 빠르지만 오탐·미탐이 많다.
  llm   : 규칙을 먼저 보고, 걸리지 않으면 LLM에게 판정을 맡긴다. 정교하지만 호출 한 번만큼 느려진다.
모드:
  block : 걸리면 막는다(입력은 모델에 보내지 않고, 출력은 가린다).
  log   : 기록만 하고 막지 않는다.
  off   : 검사하지 않는다.
자해·자살 신호(self_harm)가 입력에서 보이면 모드가 off가 아닌 한 캐릭터 대화를 멈추고 도움 경로를 안내한다.

LLM 판정이 실패하면(네트워크 오류, JSON 해석 실패) 통과시키고 이유를 기록한다(fail-open).
실제 서비스라면 막는 쪽(fail-closed)을 검토해야 한다.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from . import config
from .prompt import load_prompt
from .providers import Provider, ProviderError


@dataclass
class ModResult:
    flagged: bool
    category: str = "none"
    backend: str = "rules"
    matched: str | None = None
    reason: str | None = None
    latency_ms: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


class RulesChecker:
    """규칙 파일이 바뀌면 다음 검사 때 다시 읽는다."""

    def __init__(self, path: Path):
        self.path = path
        self._mtime: float | None = None
        self._rules: dict[str, list[re.Pattern]] = {}

    def _load(self) -> None:
        try:
            mtime = self.path.stat().st_mtime
        except FileNotFoundError:
            self._rules, self._mtime = {}, None
            return
        if mtime == self._mtime:
            return
        data = json.loads(self.path.read_text(encoding="utf-8"))
        rules: dict[str, list[re.Pattern]] = {}
        for category, patterns in data.items():
            if category.startswith("_") or not isinstance(patterns, list):
                continue
            compiled = []
            for pattern in patterns:
                try:
                    compiled.append(re.compile(pattern, re.I))
                except re.error:
                    continue
            rules[category] = compiled
        self._rules, self._mtime = rules, mtime

    def check(self, text: str) -> ModResult:
        started = time.perf_counter()
        self._load()
        for category, patterns in self._rules.items():
            for pattern in patterns:
                match = pattern.search(text or "")
                if match:
                    return ModResult(True, category, "rules", matched=match.group(0),
                                     latency_ms=(time.perf_counter() - started) * 1000)
        return ModResult(False, "none", "rules", latency_ms=(time.perf_counter() - started) * 1000)


RULES = RulesChecker(config.RULES_PATH)


def parse_judge(text: str) -> dict | None:
    match = re.search(r"\{.*\}", text or "", re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


async def llm_judge(provider: Provider, model: str, text: str) -> ModResult:
    started = time.perf_counter()
    try:
        result = await provider.complete(load_prompt("judge.txt"), f"<text>\n{text}\n</text>", model, 200,
                                         purpose="judge")
    except ProviderError as exc:
        return ModResult(False, "none", "llm", reason=f"판정 실패(통과 처리): {exc}",
                         latency_ms=(time.perf_counter() - started) * 1000)
    parsed = parse_judge(result.text)
    elapsed = (time.perf_counter() - started) * 1000
    if parsed is None:
        return ModResult(False, "none", "llm", reason=f"판정 결과 해석 실패(통과 처리): {result.text[:80]}",
                         latency_ms=elapsed)
    flagged = bool(parsed.get("flagged"))
    category = str(parsed.get("category") or "none")
    if flagged and category == "none":
        category = "other"
    return ModResult(flagged, category if flagged else "none", "llm", reason=parsed.get("reason"),
                     latency_ms=elapsed)


async def check(text: str, settings: dict, provider: Provider, model: str) -> ModResult:
    if settings["moderation_mode"] == "off":
        return ModResult(False, "none", "off")
    result = RULES.check(text)
    if result.flagged or settings["moderation_backend"] != "llm":
        return result
    return await llm_judge(provider, model, text)


_BOUNDARY = re.compile(r"[.!?…。！？~]+[\"”’」』)\]*]*\s+|\n+")


class SentenceGate:
    """출력을 문장 단위로 모았다가 검사를 통과한 부분만 내보낸다.

    버퍼가 클수록 안전하지만 사용자가 보는 첫 글자가 늦어진다(화면의 '첫 표시' 시간).
    """

    def __init__(self, check_fn: Callable[[str], ModResult], max_hold_chars: int = 160):
        self.check_fn = check_fn
        self.max_hold = max_hold_chars
        self.buf = ""
        self.released = ""
        self.active = True

    def feed(self, text: str) -> tuple[str, ModResult | None]:
        self.buf += text
        if not self.active:
            out, self.buf = self.buf, ""
            self.released += out
            return out, None
        cut = 0
        for match in _BOUNDARY.finditer(self.buf):
            cut = match.end()
        if cut == 0 and len(self.buf) >= self.max_hold:
            cut = len(self.buf)
        if cut == 0:
            return "", None
        candidate = self.buf[:cut]
        result = self.check_fn(self.released + candidate)
        if result.flagged:
            return "", result
        self.released += candidate
        self.buf = self.buf[cut:]
        return candidate, None

    def flush(self) -> tuple[str, ModResult | None]:
        if not self.buf:
            return "", None
        if self.active:
            result = self.check_fn(self.released + self.buf)
            if result.flagged:
                return "", result
        out, self.buf = self.buf, ""
        self.released += out
        return out, None

    def disable(self) -> str:
        """검사를 끄고 쌓인 내용을 모두 내보낸다(log 모드에서 한 번 기록한 뒤 사용)."""
        self.active = False
        out, self.buf = self.buf, ""
        self.released += out
        return out
