"""요약 메모리.

anchored 전략에서 '요약 지점 이후 대화'가 예산의 trigger 비율(기본 85%)을 넘으면,
오래된 메시지들을 덩어리로 요약해서 남은 대화가 keep 비율(기본 50%) 아래로 내려가게 한다.

- 덩어리로 하는 이유: 요약이 바뀔 때마다 요약 블록부터 뒤쪽의 캐시가 전부 깨진다.
  매 턴 조금씩 요약하면 캐시가 매 턴 깨지고 요약 호출 비용도 계속 든다.
- 답변이 끝난 뒤 백그라운드에서 돌린다. 사용자가 다음 말을 입력하는 동안 끝나는 경우가 많다.
  아직 안 끝났는데 다음 턴이 오면, 그 턴은 오래된 메시지를 잘라 예산을 맞춘다(overflow).
- 요약은 '어느 메시지까지 덮는지'(covers_until_msg_id)를 기록한다. 대화 가지가 갈라져도
  지금 경로 위의 메시지를 덮는 요약만 쓰인다.
"""
from __future__ import annotations

import asyncio
import time

from . import config, pricing
from .cards import apply_macros
from .db import DB
from .prompt import Msg, fill, load_prompt, persona_name
from .providers import ProviderError, get_provider
from .runinfo import run_info

MIN_AUTO_CHUNK = 4

_running: set[int] = set()
_errors: dict[int, str] = {}
_tasks: set[asyncio.Task] = set()


def is_running(chat_id: int) -> bool:
    return chat_id in _running


def last_error(chat_id: int) -> str | None:
    return _errors.get(chat_id)


def plan_chunk(since: list[Msg], history_budget: int, settings: dict, force: bool = False) -> list[Msg] | None:
    """요약할 앞부분 메시지 목록을 고른다. 요약할 필요가 없으면 None."""
    if len(since) < 4:
        return None
    budget = max(history_budget, 0)
    if force:
        keep_from = len(since) - 4  # 수동 요약: 마지막 두 번의 주고받기만 남긴다
    else:
        if sum(m.tokens for m in since) <= settings["summary_trigger_ratio"] * budget:
            return None
        keep_ratio = min(settings["summary_keep_ratio"], settings["summary_trigger_ratio"] - 0.05)
        target = keep_ratio * budget
        keep_from, kept = len(since), 0
        for i in range(len(since) - 1, -1, -1):
            if kept + since[i].tokens > target:
                break
            kept += since[i].tokens
            keep_from = i
        keep_from = min(keep_from, len(since) - 2)
    # 남기는 부분이 사용자 메시지로 시작하도록 경계를 뒤로 민다(가능하면)
    j = keep_from
    while j < len(since) - 2 and since[j].role != "user":
        j += 1
    if 0 <= j < len(since) and since[j].role == "user":
        keep_from = j
    if keep_from <= 0:
        return None
    # 자동 요약은 최소 두 번의 주고받기(4개)가 쌓였을 때만 한다. 너무 잘게 요약하면 캐시가 매번 깨진다.
    if not force and keep_from < MIN_AUTO_CHUNK:
        return None
    return since[:keep_from]


async def summarize(db: DB, chat_id: int, chunk: list[Msg], previous: dict | None, character: dict, chat: dict,
                    settings: dict) -> int:
    provider = get_provider(settings["provider"])
    model = config.SUMMARY_MODEL or settings.model
    char, user = character["name"], persona_name(chat)
    speaker = {"user": user, "assistant": char}
    dialogue = "\n".join(f"{speaker[m.role]}: {m.content}" for m in chunk)
    system = apply_macros(load_prompt("summary_system.txt"), char, user)
    prompt = fill(load_prompt("summary_user.txt"), {
        "previous": previous["content"] if previous else "(없음)",
        "dialogue": dialogue,
    })
    started = time.perf_counter()
    result = await provider.complete(system, prompt, model, 900, purpose="summary")
    text = result.text.strip()
    if not text:
        raise ProviderError("요약 결과가 비어 있어요.")
    usage = result.usage.to_dict()
    summary_id = db.add_summary(chat_id, chunk[-1].id, text, meta={
        "provider": provider.name,
        "model": model,
        "messages": len(chunk),
        "covered_tokens_est": sum(m.tokens for m in chunk),
        "previous_id": previous["id"] if previous else None,
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "usage": usage,
        # 어느 조건의 대화에서 생긴 요약인지(run의 model은 대화 모델), 비용은 실제 요약 모델 기준
        "run": run_info(settings, provider),
        "cost": pricing.cost(usage, provider.name, model),
    })
    db.add_prompt_log(chat_id, None, "summary", {
        "provider": provider.name, "model": model, "system": system, "user": prompt, "output": text,
    })
    return summary_id


def schedule(db: DB, chat_id: int, chunk: list[Msg], previous: dict | None, character: dict, chat: dict,
             settings: dict) -> bool:
    if chat_id in _running:
        return False
    _running.add(chat_id)

    async def job() -> None:
        try:
            await summarize(db, chat_id, chunk, previous, character, chat, settings)
            _errors.pop(chat_id, None)
        except Exception as exc:  # 백그라운드 작업이라 오류를 기록만 한다
            _errors[chat_id] = str(exc)
        finally:
            _running.discard(chat_id)

    task = asyncio.get_running_loop().create_task(job())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return True


async def wait_idle(timeout: float = 5.0) -> None:
    """테스트용: 진행 중인 요약 작업이 끝날 때까지 기다린다."""
    if _tasks:
        await asyncio.wait(list(_tasks), timeout=timeout)
