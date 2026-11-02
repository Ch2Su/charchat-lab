"""API 키 없이 돌아가는 가짜 제공사.

- 답변은 정해진 문장 조각을 이어 붙인 것이다. 대화 품질이 아니라 파이프라인을 보는 용도다.
- Anthropic 방식의 프롬프트 캐시를 흉내 낸다. 중단점마다 '그 위치까지의 앞부분' 해시를 저장하고,
  다음 요청에서 중단점부터 최대 20블록 뒤로 거슬러 올라가며 같은 해시를 찾는다.
  최소 길이(MOCK_CACHE_MIN_TOKENS)보다 짧은 앞부분은 캐시하지 않고, 5분이 지나면 만료된다.
- 첫 토큰 지연도 '캐시되지 않은 입력 토큰 수'에 비례하게 흉내 낸다.

입력에 넣을 수 있는 테스트 명령
  /long              긴 답변
  /mod-test-output   답변 중간에 검수 테스트 문구([[검수테스트]])를 섞는다
  /error             스트리밍 도중 오류를 낸다
  /slow              5배 느리게
  /think             답변 전에 사고(화면에는 안 보임)를 흉내 낸다
  /think-leak        사고가 답변으로 새어 나왔다가 지워지는 경우(짝 없는 </think>)를 흉내 낸다
  /quality-test      답변 끝에 한자·영어가 섞인 대사와 똑같은 문장 세 번을 붙인다(답변 품질 지표 확인용)

답변 끝의 안내 줄은 한글로만 쓴다. 로마자가 섞이면 짧은 mock 답변마다 '영문 섞임' 깃발이 붙어서
/quality-test와 구분되지 않는다.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
import re
import time
from typing import AsyncIterator

from .. import config
from ..tokens import MESSAGE_OVERHEAD, estimate_tokens
from .anthropic_format import build_anthropic_payload
from .base import ChatRequest, CompleteResult, Provider, ProviderError, StreamEvent, Usage

ACTIONS = [
    "고개를 살짝 기울인다", "잠시 생각에 잠긴다", "작게 웃는다", "손에 든 것을 내려놓는다",
    "창밖을 한 번 바라본다", "눈을 가늘게 뜬다", "자세를 고쳐 앉는다",
]
LINES = [
    "그 얘기, 조금 더 자세히 들려줄래요?",
    "흥미롭네요. 그럼 다음엔 어떻게 할 생각이에요?",
    "음, 그건 생각해 본 적 없는 이야기예요.",
    "좋아요. 그렇게 해 봐요.",
    "정말요? 그럼 저도 하나 말해 줄게요.",
    "잠깐만요, 방금 뭔가 들리지 않았어요?",
]
SECOND = [
    "*{char}의 시선이 {user}에게 머문다. 대답을 기다리는 눈치다.*",
    "*주변이 잠시 조용해진다. {char}(이)가 다음 말을 고르는 사이, 멀리서 작은 소리가 난다.*",
    "*{char}(이)가 무언가 말하려다 멈추고, 대신 {user}의 반응을 살핀다.*",
]
LONG = (
    "*시간이 조금 흐른다. {char}(이)가 천천히 주변을 둘러보며 지금까지 있었던 일을 하나씩 떠올린다.* "
    "\"그러니까, 정리해 보면 이렇게 되는 거죠.\" *손가락을 하나씩 접으며 말을 잇는다.*"
)
THINKING = [
    "상대가 방금 한 말을 다시 읽어 본다. ", "캐릭터 설정과 어긋나지 않는지 확인한다. ",
    "장면을 한 걸음만 진행하기로 한다. ", "말투는 지금까지와 같게 유지한다. ",
]
LEAKED_DRAFT = "(초안: 사고가 답변으로 새어 나온 부분이라 곧 지워져요.) "
QUALITY_TEST = [
    "\"这个故事 정말 interesting해요. 我们一起 끝까지 읽어 봐요.\"",  # 한자 8자와 영어 한 단어
    " ".join(["\"같은 말을 자꾸 되풀이하게 되네요.\""] * 3),            # 8자 넘는 같은 문장 세 번
]


class CacheSimulator:
    def __init__(self, min_tokens: int, ttl: float, lookback: int = 20):
        self.min_tokens = min_tokens
        self.ttl = ttl
        self.lookback = lookback
        self.entries: dict[str, float] = {}

    def simulate(self, payload: dict) -> dict:
        blocks: list[tuple[str, str, int, bool]] = []
        for block in payload.get("system", []):
            blocks.append(("system", block["text"], estimate_tokens(block["text"]), "cache_control" in block))
        for message in payload.get("messages", []):
            for i, block in enumerate(message["content"]):
                tokens = estimate_tokens(block["text"]) + (MESSAGE_OVERHEAD if i == 0 else 0)
                blocks.append((message["role"], block["text"], tokens, "cache_control" in block))
        if payload.get("cache_control") and blocks:
            role, text, tokens, _ = blocks[-1]
            blocks[-1] = (role, text, tokens, True)

        digest = hashlib.sha256()
        hashes, cumulative, total = [], [], 0
        for role, text, tokens, _ in blocks:
            digest.update(role.encode() + b"\x00" + text.encode("utf-8") + b"\x01")
            hashes.append(digest.copy().hexdigest())
            total += tokens
            cumulative.append(total)

        now = time.monotonic()
        self.entries = {h: exp for h, exp in self.entries.items() if exp > now}
        breakpoints = [i for i, b in enumerate(blocks) if b[3]]

        read_pos = -1
        for bp in breakpoints:
            for pos in range(bp, max(-1, bp - self.lookback), -1):
                if pos in range(len(hashes)) and self.entries.get(hashes[pos], 0) > now:
                    read_pos = max(read_pos, pos)
                    break
        cache_read = cumulative[read_pos] if read_pos >= 0 else 0
        if read_pos >= 0:
            self.entries[hashes[read_pos]] = now + self.ttl

        written_until = read_pos
        skipped_short = 0
        for bp in breakpoints:
            if bp <= read_pos:
                continue
            if cumulative[bp] < self.min_tokens:
                skipped_short += 1
                continue
            self.entries[hashes[bp]] = now + self.ttl
            written_until = max(written_until, bp)
        cache_write = cumulative[written_until] - cache_read if written_until > read_pos else 0
        return {
            "input_total": total,
            "cache_read": cache_read,
            "cache_write": cache_write,
            "uncached": total - cache_read - cache_write,
            "breakpoints": len(breakpoints),
            "below_minimum": skipped_short,
        }


class MockProvider(Provider):
    name = "mock"

    def __init__(self) -> None:
        seed = os.getenv("MOCK_SEED")
        self.rng = random.Random(int(seed)) if seed else random.Random()
        self.cache = CacheSimulator(config.MOCK_CACHE_MIN_TOKENS, config.MOCK_CACHE_TTL_SECONDS)

    def render(self, req: ChatRequest) -> dict:
        payload = build_anthropic_payload(req, req.max_tokens)
        payload["_note"] = "mock 제공사: 실제로 전송되지 않으며, Anthropic 형식으로 캐시를 흉내 낸다."
        return payload

    def options(self) -> dict:
        return {
            "delay_scale": config.MOCK_DELAY_SCALE,
            "cache_min_tokens": self.cache.min_tokens,
            "cache_ttl": self.cache.ttl,
        }

    def _reply(self, req: ChatRequest) -> str:
        hints = req.hints or {}
        char, user = hints.get("char", "캐릭터"), hints.get("user", "사용자")
        text = hints.get("input", "")
        parts = [f"*{char}(이)가 {self.rng.choice(ACTIONS)}.* \"{self.rng.choice(LINES)}\""]
        if "/mod-test-output" in text:
            parts.append("\"그러고 보니 이 문장은 [[검수테스트]] 표시를 포함하고 있어요.\"")
        parts.append(self.rng.choice(SECOND).format(char=char, user=user))
        if "/long" in text:
            parts.extend(LONG.format(char=char) for _ in range(5))
        lore = hints.get("lore") or []
        lore_text = ", ".join(lore) if lore else "없음"
        parts.append(f"(흉내 낸 응답이에요. 받은 대화 {len(req.history)}개, 반영한 로어: {lore_text})")
        if "/quality-test" in text:
            parts.extend(QUALITY_TEST)
        return "\n\n".join(parts)

    async def stream(self, req: ChatRequest) -> AsyncIterator[StreamEvent]:
        payload = build_anthropic_payload(req, req.max_tokens)
        sim = self.cache.simulate(payload)
        text_in = (req.hints or {}).get("input", "")
        scale = config.MOCK_DELAY_SCALE * (5 if "/slow" in text_in else 1)
        ttft = 0.2 + sim["uncached"] * 0.00004 + sim["cache_read"] * 0.000004 + sim["cache_write"] * 0.00005
        if scale:
            await asyncio.sleep(ttft * scale)
        reply = self._reply(req)
        max_chars = req.max_tokens * 2  # 대략적인 길이 제한 흉내
        finish = "stop"
        if len(reply) > max_chars:
            reply, finish = reply[:max_chars], "length"
        thought = ""
        if "/think" in text_in:  # 사고 흉내. /think-leak은 사고가 답변으로 샌 뒤 되돌리는 경우까지
            for piece in THINKING:
                thought += piece
                yield StreamEvent("thinking", text=piece)
                if scale:
                    await asyncio.sleep(0.4 * scale)
            if "/think-leak" in text_in:
                for i in range(0, len(LEAKED_DRAFT), 4):
                    yield StreamEvent("text", text=LEAKED_DRAFT[i:i + 4])
                    if scale:
                        await asyncio.sleep(0.03 * scale)
                thought += LEAKED_DRAFT
                yield StreamEvent("reset")
        for i in range(0, len(reply), 4):
            if "/error" in text_in and i >= 8:
                raise ProviderError("mock 오류 테스트: 스트리밍 도중 연결이 끊긴 상황을 흉내 냈어요.")
            yield StreamEvent("text", text=reply[i:i + 4])
            if scale:
                await asyncio.sleep(0.012 * scale)
        usage = Usage(
            input_total=sim["input_total"],
            cache_read=sim["cache_read"],
            cache_write=sim["cache_write"],
            output=estimate_tokens(reply) + estimate_tokens(thought),
            thinking=estimate_tokens(thought),
            raw={"simulated": sim},
        )
        yield StreamEvent("done", usage=usage, finish_reason=finish)

    async def complete(self, system: str, user: str, model: str, max_tokens: int,
                       purpose: str = "") -> CompleteResult:
        if config.MOCK_DELAY_SCALE:
            await asyncio.sleep(0.1 * config.MOCK_DELAY_SCALE)
        if purpose == "judge":
            body = re.search(r"<text>(.*)</text>", user, re.S)
            text = body.group(1) if body else user
            flagged = "[[LLM검수테스트]]" in text
            result = {"flagged": flagged, "category": "test" if flagged else "none", "reason": "mock 판정"}
            out = json.dumps(result, ensure_ascii=False)
        elif purpose == "summary":
            previous = re.search(r"\[이전 요약\]\n(.*?)\n\n\[이어서 요약할 대화\]", user, re.S)
            dialogue = re.search(r"\[이어서 요약할 대화\]\n(.*?)\n\n\[이전 요약\]과", user, re.S)
            lines = [ln.strip() for ln in (dialogue.group(1) if dialogue else "").splitlines()
                     if re.match(r"^[^:\n]{1,40}: ", ln.strip())]
            prev = (previous.group(1).strip() if previous else "")
            prev = "" if prev == "(없음)" else prev[-300:]  # mock 요약이 끝없이 길어지지 않게 자른다
            shown = lines[:6]
            digest = "\n".join(f"- {ln[:30]}{'…' if len(ln) > 30 else ''}" for ln in shown)
            more = f"\n- 외 {len(lines) - len(shown)}개" if len(lines) > len(shown) else ""
            out = (f"{prev}\n\n" if prev else "") + f"(mock 요약: 메시지 {len(lines)}개)\n{digest}{more}"
        else:
            out = "mock 응답"
        tokens_in = estimate_tokens(system) + estimate_tokens(user)
        return CompleteResult(out, Usage(input_total=tokens_in, output=estimate_tokens(out)))
