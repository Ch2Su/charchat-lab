"""대본 재생 실험.

사용자 입력 목록(대본) 하나를 여러 설정 묶음(변형)으로 처음부터 차례로 보낸다. 변형마다 새 대화를
만들고, 변형의 라벨을 실험 라벨(run_label)로 쓰므로 결과는 비교 집계(stats_for_chats)에서 바로 나뉜다.
README의 비교표를 손으로 15턴씩 보내 만들던 일을 대신한다.

- 한 번에 하나만 돌린다. mock의 흉내 캐시와 답변 난수를 모든 대화가 함께 쓰기 때문이다.
- mock은 변형마다 캐시를 비우고 난수를 다시 심어(reset_mock) 같은 조건에서 시작한다. 실제 제공사의
  캐시는 비울 수 없으니, 변형 사이에 기다려(cooldown_seconds) 앞 변형의 캐시가 만료되게 한다.
- 매 턴 요약이 끝날 때까지 기다린다(wait_summary). 요약이 늦으면 다음 턴이 잘려(overflow) 결과가 흔들린다.
- 진행 상황은 experiments 표에 남긴다. 서버를 끄면 진행 중이던 실험은 interrupted가 된다.
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import time
from contextlib import aclosing

from . import config, memory, pricing
from .chat_service import GROUP_FIELDS, ChatService, TurnError
from .config import Settings
from .db import DB
from .providers import reset_mock

MAX_INPUTS = 100
MAX_INPUT_CHARS = 2000
MAX_VARIANTS = 6
MAX_LABEL_CHARS = 40
MAX_COOLDOWN_SECONDS = 600
# 실제 제공사의 캐시는 이만큼 지나야 만료된다고 보고 경고한다(Anthropic 기본 5분)
CACHE_TTL_SECONDS = 300

CSV_FIELDS = (*GROUP_FIELDS, "turns", "avg_input", "cache_hit_ratio", "usd", "judge_calls", "judge_usd",
              "summary_calls", "summary_usd", "total_usd", "avg_ttft_ms", "avg_first_visible_ms",
              "quality_turns", "avg_foreign_ratio", "avg_latin_ratio", "avg_repeat_prev", "speech_shift_turns",
              "flagged_turns")


def _sse_error(chunk: str) -> str | None:
    """SSE 조각이 error 이벤트면 그 메시지."""
    if not chunk.startswith("event: error"):
        return None
    try:
        return json.loads(chunk.split("\ndata: ", 1)[1]).get("message")
    except (IndexError, ValueError):
        return None


def stats_csv(groups: list[dict]) -> str:
    """비교 집계를 CSV로. README 표를 만들 때 쓴다(엑셀이 한글을 알아보도록 BOM을 붙인다)."""
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(CSV_FIELDS)
    for group in groups:
        writer.writerow([
            json.dumps(group.get(f), ensure_ascii=False, sort_keys=True) if isinstance(group.get(f), dict) else group.get(f)
            for f in CSV_FIELDS
        ])
    return "﻿" + out.getvalue()


class ExperimentRunner:
    def __init__(self, db: DB, service: ChatService):
        self.db = db
        self.service = service
        self._tasks: dict[int, asyncio.Task] = {}
        self._uses_mock: dict[int, bool] = {}
        self._closing = False

    def running_id(self) -> int | None:
        return next((eid for eid, task in self._tasks.items() if not task.done()), None)

    def uses_mock(self) -> bool:
        """지금 도는 실험에 mock 변형이 있는가. mock의 캐시와 난수는 모든 대화가 함께 쓴다."""
        running = self.running_id()
        return running is not None and self._uses_mock.get(running, False)

    # ── 검증 ─────────────────────────────────────
    @staticmethod
    def _variant_values(label: str, settings) -> dict:
        if settings is None:
            return {}
        if not isinstance(settings, dict):
            raise TurnError(f"'{label}' 변형의 settings는 JSON 객체여야 해요.")
        values = {}
        for key, value in settings.items():
            if key == "run_label":
                raise TurnError(f"'{label}' 변형: run_label은 쓸 수 없어요. 변형의 라벨이 실험 라벨이 돼요.")
            if key not in config.SETTINGS_SPEC:
                raise TurnError(f"'{label}' 변형: 알 수 없는 설정이에요({key}).")
            try:
                values[key] = config.coerce_setting(key, value)
            except config.SettingsError as exc:
                raise TurnError(f"'{label}' 변형: {exc}") from None
        return values

    def validate(self, spec) -> tuple[dict, list[tuple[str, Settings]]]:
        """요청을 검사해 저장할 모양과 변형별 최종 설정을 돌려준다. 잘못되면 TurnError(400)."""
        if not isinstance(spec, dict):
            raise TurnError("요청은 JSON 객체여야 해요.")
        character_id = spec.get("character_id")
        if isinstance(character_id, bool) or not isinstance(character_id, int) or not self.db.get_character(character_id):
            raise TurnError("캐릭터를 찾을 수 없어요. character_id를 확인하세요.")

        inputs = spec.get("inputs")
        if not isinstance(inputs, list) or not 1 <= len(inputs) <= MAX_INPUTS:
            raise TurnError(f"입력 대본(inputs)은 1~{MAX_INPUTS}개여야 해요.")
        clean_inputs = []
        for n, text in enumerate(inputs, 1):
            text = text.strip() if isinstance(text, str) else ""
            if not 1 <= len(text) <= MAX_INPUT_CHARS:
                raise TurnError(f"{n}번째 입력은 1~{MAX_INPUT_CHARS}자의 글이어야 해요.")
            clean_inputs.append(text)

        variants = spec.get("variants")
        if not isinstance(variants, list) or not 1 <= len(variants) <= MAX_VARIANTS:
            raise TurnError(f"변형(variants)은 1~{MAX_VARIANTS}개여야 해요.")
        overrides = self.db.get_setting_overrides()
        clean_variants: list[dict] = []
        built: list[tuple[str, Settings]] = []
        for n, variant in enumerate(variants, 1):
            if not isinstance(variant, dict):
                raise TurnError(f'{n}번째 변형은 {{"label": "...", "settings": {{...}}}} 모양이어야 해요.')
            label = str(variant.get("label") or "").strip()
            if not 1 <= len(label) <= MAX_LABEL_CHARS:
                raise TurnError(f"{n}번째 변형의 라벨은 1~{MAX_LABEL_CHARS}자여야 해요.")
            if any(v["label"] == label for v in clean_variants):
                raise TurnError(f"변형 라벨이 겹쳐요({label}). 라벨은 비교 표의 행 이름이라 서로 달라야 해요.")
            values = self._variant_values(label, variant.get("settings"))
            merged = {**overrides, **values, "run_label": label}
            if "models" in values:  # 화면에서 설정을 저장할 때처럼 제공사별 모델은 덮어쓰지 않고 합친다
                merged["models"] = {**(overrides.get("models") or {}), **values["models"]}
            final = config.build_settings(merged)
            if not config.available_providers().get(final["provider"]):
                raise TurnError(f"'{label}' 변형: 설정되지 않은 제공사예요({final['provider']}). "
                                ".env를 확인하고 서버를 다시 시작하세요.")
            if not final.model:
                raise TurnError(f"'{label}' 변형: 모델 이름이 비어 있어요. settings의 models나 .env의 기본 모델을 넣으세요.")
            clean_variants.append({"label": label, "settings": values})
            built.append((label, final))

        cooldown = spec.get("cooldown_seconds") or 0
        if isinstance(cooldown, bool) or not isinstance(cooldown, (int, float)) \
                or not 0 <= cooldown <= MAX_COOLDOWN_SECONDS:
            raise TurnError(f"변형 사이 대기(cooldown_seconds)는 0~{MAX_COOLDOWN_SECONDS}초여야 해요.")
        seed = spec.get("seed")
        if seed is not None and (isinstance(seed, bool) or not isinstance(seed, int)):
            raise TurnError("seed는 정수이거나 비워 둬야 해요.")
        clean = {
            "name": str(spec.get("name") or "").strip()[:80] or "이름 없는 실험",
            "character_id": character_id,
            "persona_name": str(spec.get("persona_name") or "").strip()[:40],
            "persona_desc": str(spec.get("persona_desc") or "").strip(),
            "inputs": clean_inputs,
            "variants": clean_variants,
            "reset_mock": bool(spec.get("reset_mock", True)),
            "seed": seed,
            "wait_summary": bool(spec.get("wait_summary", True)),
            "cooldown_seconds": cooldown,
        }
        return clean, built

    def estimate(self, spec) -> dict:
        """시작하기 전에 변형별 호출 수와 비용 상한을 어림한다. 검증은 start와 같아서 잘못된 요청은 TurnError.

        비용은 매 턴 입력이 입력 예산을 꽉 채우고 출력도 최대치까지 나온다고 본 상한이다(캐시 할인, 요약 호출,
        사고 여유분은 뺀다). 가격표에 없는 모델은 max_usd가 None이다.
        """
        clean, variants = self.validate(spec)
        turns = len(clean["inputs"])
        rows = []
        for label, settings in variants:
            provider, model = settings["provider"], settings.model
            price = pricing.price_for(provider, model)
            max_input, max_output = turns * settings["input_token_budget"], turns * settings["max_output_tokens"]
            judged = settings["moderation_mode"] != "off" and settings["moderation_backend"] == "llm"
            rows.append({
                "label": label, "provider": provider, "model": model,
                "chat_calls": turns, "judge_calls": 2 * turns if judged else 0,
                "summary_possible": settings["window_strategy"] == "anchored",
                "max_input_tokens": max_input, "max_output_tokens": max_output,
                "max_usd": round((max_input * price["input"] + max_output * price["output"]) / 1_000_000, 6)
                if price else None,
            })
        paid = [row for row in rows if row["provider"] != "mock"]
        cooldown = clean["cooldown_seconds"]
        return {
            "turns": turns,
            "variants": rows,
            "paid_calls": sum(row["chat_calls"] + row["judge_calls"] for row in paid),
            "paid_max_usd": round(sum(row["max_usd"] or 0 for row in paid), 6),
            "unpriced": [row["label"] for row in paid if row["max_usd"] is None],
            # 실제 제공사 변형이 둘 이상인데 캐시가 만료될 만큼 기다리지 않으면 앞 변형의 캐시가 뒤 변형에 섞인다
            "cache_overlap": len(paid) > 1 and cooldown < CACHE_TTL_SECONDS,
            "cooldown_seconds": cooldown,
            "cache_ttl_seconds": CACHE_TTL_SECONDS,
        }

    # ── 시작과 멈춤 ───────────────────────────────
    def start(self, spec) -> int:
        if self.running_id() is not None:
            raise TurnError("이미 실행 중인 실험이 있어요. 끝나거나 취소한 뒤에 시작하세요.", 409)
        clean, variants = self.validate(spec)
        experiment_id = self.db.create_experiment(clean["name"], clean["character_id"], clean)
        self.db.update_experiment(
            experiment_id,
            progress={"variant": 0, "variants": len(variants), "turn": 0, "turns": len(clean["inputs"])},
            runs=[self._new_run(label) for label, _ in variants],
        )
        task = asyncio.get_running_loop().create_task(self._run(experiment_id, clean, variants))
        self._tasks[experiment_id] = task
        self._uses_mock[experiment_id] = any(settings["provider"] == "mock" for _, settings in variants)
        task.add_done_callback(lambda _task: (self._tasks.pop(experiment_id, None), self._uses_mock.pop(experiment_id, None)))
        return experiment_id

    def cancel(self, experiment_id: int) -> bool:
        task = self._tasks.get(experiment_id)
        if task is None or task.done():
            return False
        task.cancel()
        return True

    async def shutdown(self) -> None:
        """서버를 끌 때 진행 중인 실험을 멈춘다. 사용자가 취소한 것과 구분해 interrupted로 남긴다."""
        self._closing = True
        tasks = [task for task in self._tasks.values() if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # ── 실행 ─────────────────────────────────────
    @staticmethod
    def _new_run(label: str) -> dict:
        return {"label": label, "chat_id": None, "status": "pending", "turns_done": 0, "blocked": 0, "error": None}

    async def _run(self, experiment_id: int, spec: dict, variants: list[tuple[str, Settings]]) -> None:
        db, inputs = self.db, spec["inputs"]
        runs = [self._new_run(label) for label, _ in variants]
        progress = {"variant": 0, "variants": len(variants), "turn": 0, "turns": len(inputs)}

        def save(**fields) -> None:
            db.update_experiment(experiment_id, progress=progress, runs=runs, **fields)

        try:
            for i, (label, settings) in enumerate(variants):
                if i > 0 and spec["cooldown_seconds"]:
                    await asyncio.sleep(spec["cooldown_seconds"])
                if spec["reset_mock"] and settings["provider"] == "mock":
                    reset_mock(spec["seed"])
                run = runs[i]
                run["chat_id"] = db.create_chat(spec["character_id"], spec["persona_name"], spec["persona_desc"],
                                                title=f"[실험] {spec['name']} · {label}")
                run["status"] = "running"
                progress.update(variant=i + 1, turn=0)
                save()
                for j, text in enumerate(inputs):
                    error = await self._turn(run, text, settings)
                    progress["turn"] = j + 1
                    if error:  # 이 변형은 멈추고 다음 변형으로 넘어간다
                        run.update(status="error", error=error)
                        break
                    if spec["wait_summary"]:
                        await memory.wait_chat(run["chat_id"])
                    save()
                    await asyncio.sleep(0)  # 지연 없는 mock은 한 번도 양보하지 않으므로 화면 요청과 취소가 끼어들 틈을 준다
                if run["status"] == "running":
                    run["status"] = "done"
                save()
            save(status="done", finished_at=time.time())
        except asyncio.CancelledError:
            status = "interrupted" if self._closing else "cancelled"
            for run in runs:
                if run["status"] in ("pending", "running"):
                    run["status"] = status
            save(status=status, finished_at=time.time())
            raise
        except Exception as exc:  # 처리하지 못한 오류: 실험을 멈추고 이유를 남긴다
            for run in runs:
                if run["status"] == "running":
                    run["status"] = "error"
            save(status="error", error=f"{type(exc).__name__}: {exc}", finished_at=time.time())

    async def _turn(self, run: dict, text: str, settings: Settings) -> str | None:
        """입력 하나를 보내고 끝까지 받는다. 이 변형을 멈춰야 하는 오류면 그 메시지를 돌려준다."""
        chat_id = run["chat_id"]
        try:
            stream = self.service.send(chat_id, text, settings=settings)
        except TurnError as exc:
            return str(exc)
        error = None
        async with aclosing(stream) as events:
            async for chunk in events:
                error = _sse_error(chunk) or error
        chat = self.db.get_chat(chat_id)
        leaf = self.db.get_message(chat["active_leaf_id"]) if chat and chat["active_leaf_id"] else None
        if leaf is None:
            return "실험 중에 대화가 지워졌어요."
        if leaf["role"] == "notice":  # 입력이 검수에 막힘: 세기만 하고 계속한다
            run["blocked"] += 1
        elif leaf["role"] != "assistant":  # 답이 저장되지 않은 오류
            return error or "답변이 저장되지 않았어요."
        elif leaf["meta"].get("status") == "error":  # 일부만 받고 끊긴 오류
            return leaf["meta"].get("error") or error or "답변 도중 오류가 났어요."
        run["turns_done"] += 1
        return None
