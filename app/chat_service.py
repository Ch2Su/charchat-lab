"""한 턴의 흐름을 조율한다.

  입력 검수 → 프롬프트 조립 → 스트리밍(+출력 문장 버퍼 검수) → 사후 검수 → 저장 → 요약 예약

결과는 SSE(Server-Sent Events) 문자열로 흘려보낸다.
  user     저장된 사용자 메시지
  meta     조립 결과(토큰 추정, 창, 로어, 요약)와 실행 설정
  thinking 사고 진행 상황(지금까지의 글자 수, 시간). 너무 잦지 않게 묶어서 보낸다
  delta    화면에 붙일 텍스트 조각
  reset    지금까지 보낸 답변이 사실 사고였으니 지움(짝 없는 </think>)
  retract  검수에 걸려 지금까지 보낸 답변을 가리는 문구로 바꿈
  notice   입력이 막혔거나 위기 안내를 띄움
  error    제공사 오류
  done     최종 저장된 메시지

턴마다 meta.run에 실행 조건(runinfo.run_info)을, meta.aux에 대화 밖 보조 호출(LLM 검수 판정)의
사용량과 비용을 남긴다. 집계(stats_for_chats)는 실행 조건이 하나라도 다르면 행을 나누고,
본 대화 비용에 검수·요약 비용을 더해 '숨은 비용'까지 보여 준다.
"""
from __future__ import annotations

import asyncio
import json
import time
from contextlib import aclosing
from typing import AsyncIterator, Callable

from . import config, memory, moderation, pricing, quality
from .db import DB
from .moderation import ModResult
from .prompt import assemble, load_prompt, memo_markers, persona_name
from .providers import ChatRequest, Provider, ProviderError, get_provider
from .runinfo import run_info

BLOCK_NOTICE = "입력이 검수 기준(분류: {category})에 걸려 모델에 보내지 않았어요. 오른쪽 검수 로그에서 자세히 볼 수 있어요."
RETRACT_NOTICE = "(검수 기준에 걸려 가려진 응답이에요. 분류: {category})"
PROVIDER_BLOCK_NOTICE = "(제공사 안전 장치가 응답을 막았어요: {detail})"
THINKING_ONLY_NOTICE = {
    "length": "(사고하다가 최대 출력 토큰을 다 써서 답변이 비었어요. 설정에서 최대 출력을 늘리거나 .env에서 사고를 줄여 보세요.)",
    "other": "(사고만 하고 답변 없이 끝났어요. 다시 생성해 보세요.)",
}
THINKING_SSE_INTERVAL_MS = 300

# 비교 탭에서 행을 나누는 실행 조건(meta.run의 키). moderation_mode는 비용·지연에 영향이 없어 뺐다.
GROUP_FIELDS = ("label", "provider", "model", "strategy", "cache_mode", "input_token_budget",
                "max_output_tokens", "temperature", "moderation_backend", "output_buffer",
                "options", "prompt_version")


class TurnError(Exception):
    """스트리밍을 시작하기 전에 알 수 있는 오류(HTTP 400/404로 응답)."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _ms(started: float) -> int:
    return round((time.perf_counter() - started) * 1000)


def aux_entry(purpose: str, check: ModResult | None) -> dict | None:
    """LLM 판정처럼 대화 밖에서 부른 호출의 사용량과 비용. 판정을 안 했으면 None."""
    if not check or check.backend != "llm" or not check.usage:
        return None
    return {"purpose": purpose, "provider": check.provider, "model": check.model,
            "usage": check.usage, "cost": pricing.cost(check.usage, check.provider, check.model),
            "latency_ms": round(check.latency_ms, 1)}


class ChatService:
    def __init__(self, db: DB, get_settings: Callable[[], dict]):
        self.db = db
        self.get_settings = get_settings

    # ── 공통 확인 ────────────────────────────────
    def _chat(self, chat_id: int) -> dict:
        chat = self.db.get_chat(chat_id)
        if not chat:
            raise TurnError("대화를 찾을 수 없어요.", 404)
        return chat

    def _message(self, message_id: int) -> dict:
        message = self.db.get_message(message_id)
        if not message:
            raise TurnError("메시지를 찾을 수 없어요.", 404)
        return message

    def _provider(self, settings: dict) -> Provider:
        try:
            provider = get_provider(settings["provider"])
        except ProviderError as exc:
            raise TurnError(str(exc)) from exc
        if not settings.model:
            raise TurnError("모델 이름이 비어 있어요. 설정에서 모델을 입력하거나 .env에 기본 모델을 넣으세요.")
        return provider

    def _log_mod(self, stage: str, result: ModResult, action: str, excerpt: str, *, chat: dict,
                 message_id: int | None = None, extra: dict | None = None) -> int:
        return self.db.add_mod_event(
            stage=stage, backend=result.backend, category=result.category, action=action, excerpt=excerpt,
            detail={**result.to_dict(), **(extra or {})}, chat_id=chat["id"], message_id=message_id,
            character_id=chat["character_id"],
        )

    # ── 진입점 ───────────────────────────────────
    def send(self, chat_id: int, content: str, settings: dict | None = None) -> AsyncIterator[str]:
        """settings를 주면 저장된 설정 대신 그것으로 보낸다(대본 재생 실험의 변형). 요약 예약도 그 설정을 따른다."""
        chat = self._chat(chat_id)
        content = (content or "").strip()
        if not content:
            raise TurnError("보낼 내용이 비어 있어요.")
        if settings is None:
            settings = self.get_settings()
        provider = self._provider(settings)
        return self._send_flow(chat, chat["active_leaf_id"], content, settings, provider)

    def edit_user(self, message_id: int, content: str) -> AsyncIterator[str]:
        message = self._message(message_id)
        if message["role"] != "user":
            raise TurnError("사용자 메시지만 이 방식으로 수정할 수 있어요.")
        content = (content or "").strip()
        if not content:
            raise TurnError("내용이 비어 있어요.")
        chat = self._chat(message["chat_id"])
        settings = self.get_settings()
        provider = self._provider(settings)
        return self._send_flow(chat, message["parent_id"], content, settings, provider, edited_from=message_id)

    def regenerate(self, chat_id: int, message_id: int | None = None) -> AsyncIterator[str]:
        chat = self._chat(chat_id)
        target_id = message_id or chat["active_leaf_id"]
        if target_id is None:
            raise TurnError("다시 생성할 메시지가 없어요.")
        target = self._message(target_id)
        if target["chat_id"] != chat_id:
            raise TurnError("다른 대화의 메시지예요.")
        if target["role"] == "assistant":
            if target["parent_id"] is None:
                raise TurnError("인사말은 다시 생성할 수 없어요. 화살표로 다른 인사말을 고를 수 있어요.")
            parent = self._message(target["parent_id"])
        elif target["role"] == "user":
            parent = target
        else:
            raise TurnError("이 메시지에서는 다시 생성할 수 없어요.")
        if parent["role"] != "user" or parent["excluded"]:
            raise TurnError("검수로 막힌 입력에는 답변을 만들 수 없어요.")
        settings = self.get_settings()
        provider = self._provider(settings)
        return self._generate(chat, parent, settings, provider)

    def edit_assistant(self, message_id: int, content: str) -> dict:
        message = self._message(message_id)
        if message["role"] != "assistant":
            raise TurnError("캐릭터 메시지만 이 방식으로 수정할 수 있어요.")
        chat = self._chat(message["chat_id"])
        settings = self.get_settings()
        # 직접 고친 캐릭터 대사도 다음 턴부터 맥락에 들어가므로 규칙 검수를 거친다
        if settings["moderation_mode"] != "off":
            result = moderation.RULES.check(content)
            if result.flagged:
                action = "blocked" if settings["moderation_mode"] == "block" else "logged"
                self._log_mod("edit", result, action, content, chat=chat, message_id=message_id)
                if action == "blocked":
                    raise TurnError(f"수정한 내용이 검수 기준(분류: {result.category})에 걸려 저장하지 않았어요.")
        new_id = self.db.add_message(chat["id"], message["parent_id"], "assistant", content,
                                     meta={"edited_from": message_id, "manual": True})
        self.db.set_active_leaf(chat["id"], new_id)
        return self.db.get_message(new_id)

    def switch(self, chat_id: int, message_id: int) -> None:
        message = self._message(message_id)
        if message["chat_id"] != chat_id:
            raise TurnError("다른 대화의 메시지예요.")
        self.db.set_active_leaf(chat_id, self.db.latest_leaf(message_id))

    # ── 흐름 ─────────────────────────────────────
    async def _send_flow(self, chat: dict, parent_id: int | None, content: str, settings: dict,
                         provider: Provider, edited_from: int | None = None) -> AsyncIterator[str]:
        mod_model = config.MODERATION_MODEL or settings.model
        result = await moderation.check(content, settings, provider, mod_model)
        meta = {"edited_from": edited_from} if edited_from else {}
        if result.flagged:
            meta["moderation"] = result.to_dict()
            crisis = result.category == "self_harm"
            if crisis or settings["moderation_mode"] == "block":
                user_id = self.db.add_message(chat["id"], parent_id, "user", content, meta=meta, excluded=True)
                kind = "crisis" if crisis else "blocked"
                text = (load_prompt("crisis.txt", settings.get("prompt_overrides")) if crisis
                        else BLOCK_NOTICE.format(category=result.category))
                # 답변이 없어도 입력 판정 비용은 들었으므로 안내 메시지에 실행 조건과 함께 남긴다
                aux = [e for e in [aux_entry("judge_input", result)] if e]
                notice_id = self.db.add_message(chat["id"], user_id, "notice", text,
                                                meta={"kind": kind, "run": run_info(settings, provider), "aux": aux})
                self.db.set_active_leaf(chat["id"], notice_id)
                self._log_mod("input", result, kind, content, chat=chat, message_id=user_id)
                yield sse("notice", {"kind": kind, "text": text, "user_message_id": user_id, "notice_id": notice_id})
                yield sse("done", {"blocked": True, "message": self.db.get_message(notice_id)})
                return
        user_id = self.db.add_message(chat["id"], parent_id, "user", content, meta=meta)
        if result.flagged:
            self._log_mod("input", result, "logged", content, chat=chat, message_id=user_id)
        self.db.set_active_leaf(chat["id"], user_id)
        yield sse("user", {"message": self.db.get_message(user_id)})
        async for chunk in self._generate(chat, self.db.get_message(user_id), settings, provider, input_check=result):
            yield chunk

    async def _generate(self, chat: dict, user_msg: dict, settings: dict, provider: Provider,
                        input_check: ModResult | None = None) -> AsyncIterator[str]:
        db = self.db
        character = db.get_character(chat["character_id"])
        prior = db.path_to(user_msg["id"])[:-1]
        summary = db.latest_summary_on_path(chat["id"], prior) if settings["window_strategy"] == "anchored" else None
        asm = assemble(character, chat, prior, user_msg["content"], settings, summary)
        model = settings.model
        req = ChatRequest(
            model=model,
            fixed_system=asm.fixed_system,
            summary=asm.summary_block,
            history=asm.request_history(),
            final_user=asm.final_user,
            max_tokens=settings["max_output_tokens"],
            temperature=settings["temperature"],
            cache_mode=settings["cache_mode"],
            hints={
                "char": character["name"],
                "user": persona_name(chat),
                "lore": [h.entry["name"] for h in asm.lore.hits],
                "input": user_msg["content"],
            },
            options=config.provider_overrides(settings, provider.name),
        )
        payload = provider.render(req)
        diagnostics = asm.diagnostics()
        run = run_info(settings, provider)
        aux = [e for e in [aux_entry("judge_input", input_check)] if e]
        yield sse("meta", {"user_message_id": user_msg["id"], "diagnostics": diagnostics, "run": run})

        mode = settings["moderation_mode"]
        mod_model = config.MODERATION_MODEL or model
        gate = (moderation.SentenceGate(moderation.RULES.check)
                if mode != "off" and settings["output_buffer"] == "sentence" else None)
        started = time.perf_counter()
        ttft: int | None = None
        first_visible: int | None = None
        raw_parts: list[str] = []
        shown_parts: list[str] = []
        usage = None
        finish: str | None = None
        detail: str | None = None
        error: str | None = None
        retracted: ModResult | None = None
        event_ids: list[int] = []
        thinking = {"chars": 0, "first_ms": None, "last_ms": None}
        thinking_sent_ms = -THINKING_SSE_INTERVAL_MS

        def note_thinking(chars: int, first_ms: int | None = None) -> int:
            now = _ms(started)
            if thinking["first_ms"] is None:
                thinking["first_ms"] = first_ms if first_ms is not None else now
            thinking["last_ms"] = now
            thinking["chars"] += chars
            return now

        def thinking_meta() -> dict | None:
            if thinking["first_ms"] is None:
                return None
            return {"chars": thinking["chars"], "ms": thinking["last_ms"] - thinking["first_ms"]}

        def show(text: str) -> str | None:
            nonlocal first_visible
            if not text:
                return None
            if first_visible is None:
                first_visible = _ms(started)
            shown_parts.append(text)
            return sse("delta", {"text": text})

        def handle_gate_flag(flag: ModResult) -> str | None:
            """출력 문장 검수에 걸렸을 때. block이면 중단, log면 기록하고 계속."""
            nonlocal retracted
            if mode == "block":
                retracted = flag
                return None
            event_ids.append(self._log_mod("output", flag, "logged", "".join(raw_parts), chat=chat))
            return gate.disable()

        def base_meta(status: str) -> dict:
            usage_dict = usage.to_dict() if usage else None
            return {
                "run": run,
                "diagnostics": diagnostics,
                "usage": usage_dict,
                "cost": pricing.cost(usage_dict, provider.name, model) if usage_dict else None,
                "latency": {
                    "ttft_ms": ttft,
                    "first_visible_ms": first_visible,
                    "total_ms": _ms(started),
                    "input_moderation_ms": round(input_check.latency_ms, 1) if input_check else None,
                },
                "thinking": thinking_meta(),
                "status": status,
                "finish_reason": finish,
                "finish_detail": detail,
                "error": error,
                "aux": list(aux),
            }

        try:
            async with aclosing(provider.stream(req)) as events:
                async for event in events:
                    if event.type == "thinking":
                        now = note_thinking(len(event.text))
                        if now - thinking_sent_ms >= THINKING_SSE_INTERVAL_MS:
                            thinking_sent_ms = now
                            yield sse("thinking", thinking_meta())
                    elif event.type == "reset":
                        # 답변으로 받은 글이 사실 사고였다. 받은 만큼 지우고 답변을 처음부터 다시 받는다.
                        note_thinking(len("".join(raw_parts)), first_ms=ttft)
                        raw_parts.clear()
                        shown_parts.clear()
                        ttft = first_visible = None
                        if gate is not None:
                            gate = moderation.SentenceGate(moderation.RULES.check)
                        yield sse("reset", thinking_meta())
                    elif event.type == "text":
                        if ttft is None:
                            ttft = _ms(started)
                        raw_parts.append(event.text)
                        if gate is None:
                            out = show(event.text)
                        else:
                            released, flag = gate.feed(event.text)
                            if flag:
                                released = handle_gate_flag(flag)
                                if retracted:
                                    break
                            out = show(released or "")
                        if out:
                            yield out
                    elif event.type == "done":
                        usage, finish, detail = event.usage, event.finish_reason, event.detail
            if gate is not None and retracted is None:
                released, flag = gate.flush()
                if flag:
                    released = handle_gate_flag(flag)
                out = show(released or "") if not retracted else None
                if out:
                    yield out
        except ProviderError as exc:
            error = str(exc)
            finish = "error"
        except (asyncio.CancelledError, GeneratorExit):
            # 사용자가 '멈춤'을 눌렀거나 연결이 끊겼다. 화면에 보인 만큼 저장하고 끝낸다.
            shown = "".join(shown_parts)
            if shown:
                meta = base_meta("aborted")
                meta["finish_reason"] = "aborted"
                message_id = db.add_message(chat["id"], user_msg["id"], "assistant", shown, meta=meta)
                db.set_active_leaf(chat["id"], message_id)
                db.add_prompt_log(chat["id"], message_id, "chat", payload)
            else:  # 보인 글이 없어 답이 저장되지 않아도 이미 쓴 입력 판정 비용은 남긴다
                db.add_prompt_log(chat["id"], None, "chat", {"request": payload, "aborted": True, "run": run, "aux": aux})
            raise

        raw_text = "".join(raw_parts)
        shown_text = "".join(shown_parts)

        # 사후 검수: 버퍼 없이 흘려보냈다면 규칙으로, llm 방식이면 전체 답변을 LLM으로 한 번 더 본다
        output_check: ModResult | None = None
        if retracted is None and error is None and mode != "off" and raw_text:
            if gate is None:
                output_check = moderation.RULES.check(raw_text)
            if (output_check is None or not output_check.flagged) and settings["moderation_backend"] == "llm":
                output_check = await moderation.llm_judge(
                    provider, mod_model, raw_text, options=config.provider_overrides(settings, provider.name),
                    prompts=settings.get("prompt_overrides"))
            if output_check and output_check.flagged:
                if mode == "block":
                    retracted = output_check
                else:
                    event_ids.append(self._log_mod("output", output_check, "logged", raw_text, chat=chat))

        if error and not shown_text:
            # 답변이 저장되지 않는 턴이라, 입력 판정 비용(aux)은 프롬프트 기록에 남겨 비교 집계가 읽게 한다
            db.add_prompt_log(chat["id"], None, "chat", {"request": payload, "error": error, "run": run, "aux": aux})
            yield sse("error", {"message": error, "user_message_id": user_msg["id"]})
            return

        meta = base_meta("error" if error else "complete")
        meta["latency"]["output_moderation_ms"] = round(output_check.latency_ms, 1) if output_check else None
        judged = aux_entry("judge_output", output_check)
        if judged:
            meta["aux"].append(judged)
        content, excluded = shown_text, False
        if retracted:
            content, excluded = RETRACT_NOTICE.format(category=retracted.category), True
            meta["retracted"] = retracted.to_dict()
            event_ids.append(self._log_mod("output", retracted, "retracted", raw_text, chat=chat,
                                           extra={"full_text": raw_text}))
            yield sse("retract", {"text": content, "category": retracted.category})
        elif finish in ("safety", "refusal"):
            event_ids.append(self.db.add_mod_event(
                stage="provider", backend="provider", category=finish, action="logged", excerpt=raw_text,
                detail={"detail": detail}, chat_id=chat["id"], character_id=chat["character_id"],
            ))
            if not raw_text:
                content, excluded = PROVIDER_BLOCK_NOTICE.format(detail=detail or finish), True
        elif not raw_text and not error and thinking_meta():
            content, excluded = THINKING_ONLY_NOTICE["length" if finish == "length" else "other"], True

        # 답변 품질 어림 지표. 가려진 답, 안내문, 오류로 잘린 답은 재지 않는다(멈춤으로 잘린 답처럼 비교가 안 된다)
        if not excluded and not error and content.strip():
            previous = [m["content"] for m in prior if m["role"] == "assistant" and not m.get("excluded")][-5:]
            # 마지막 메시지 템플릿의 <태그>가 답에 나오면 프롬프트가 샌 것이다. 덮어쓴 템플릿의 태그도 따라간다
            meta["quality"] = quality.measure(content, previous, markers=memo_markers(settings.get("prompt_overrides")))

        message_id = db.add_message(chat["id"], user_msg["id"], "assistant", content, meta=meta, excluded=excluded)
        db.set_active_leaf(chat["id"], message_id)
        db.add_prompt_log(chat["id"], message_id, "chat", payload)
        db.attach_mod_events(event_ids, message_id)
        if error:  # 일부만 받은 뒤 끊긴 경우: 받은 만큼 저장하고 오류도 알린다
            yield sse("error", {"message": error, "user_message_id": user_msg["id"], "partial": True})
        scheduled = self._maybe_summarize(chat["id"], character, settings) if not error else False
        yield sse("done", {"message": db.get_message(message_id), "summary_scheduled": scheduled})

    # ── 요약 ─────────────────────────────────────
    def _summary_plan(self, chat_id: int, character: dict, settings: dict, force: bool):
        chat = self.db.get_chat(chat_id)
        path = self.db.active_path(chat_id)
        summary = self.db.latest_summary_on_path(chat_id, path)
        asm = assemble(character, chat, path, "", settings, summary)
        chunk = memory.plan_chunk(asm.since, asm.window["history_budget"], settings, force=force)
        return chat, chunk, asm.summary

    def _maybe_summarize(self, chat_id: int, character: dict, settings: dict) -> bool:
        if settings["window_strategy"] != "anchored":
            return False
        chat, chunk, previous = self._summary_plan(chat_id, character, settings, force=False)
        if not chunk:
            return False
        return memory.schedule(self.db, chat_id, chunk, previous, character, chat, settings)

    async def summarize_now(self, chat_id: int) -> dict:
        chat = self._chat(chat_id)
        settings = self.get_settings()
        if settings["window_strategy"] != "anchored":
            raise TurnError("요약은 창 전략이 anchored일 때만 쓰여요. 설정에서 바꿔 주세요.")
        if memory.is_running(chat_id):
            raise TurnError("이미 요약 중이에요.", 409)
        self._provider(settings)
        character = self.db.get_character(chat["character_id"])
        chat, chunk, previous = self._summary_plan(chat_id, character, settings, force=True)
        if not chunk:
            raise TurnError("요약할 만큼 대화가 쌓이지 않았어요(요약 지점 이후 메시지가 5개 이상 필요).")
        memory._running.add(chat_id)
        try:
            summary_id = await memory.summarize(self.db, chat_id, chunk, previous, character, chat, settings)
        except ProviderError as exc:
            raise TurnError(f"요약 실패: {exc}") from exc
        finally:
            memory._running.discard(chat_id)
        return next(s for s in self.db.summaries_for_chat(chat_id) if s["id"] == summary_id)

    # ── 조회 ─────────────────────────────────────
    def chat_view(self, chat_id: int) -> dict:
        chat = self._chat(chat_id)
        character = self.db.get_character(chat["character_id"])
        path = self.db.active_path(chat_id)
        messages = []
        for message in path:
            siblings = self.db.sibling_ids(chat_id, message["parent_id"])
            messages.append({
                **message,
                "sibling_ids": siblings,
                "sibling_index": siblings.index(message["id"]),
                "sibling_count": len(siblings),
            })
        return {
            "chat": chat,
            "character": {"id": character["id"], "name": character["name"]},
            "persona_name": persona_name(chat),
            "messages": messages,
            "summary": self.db.latest_summary_on_path(chat_id, path),
            "summaries": len(self.db.summaries_for_chat(chat_id)),
            "summarizing": memory.is_running(chat_id),
            "summary_error": memory.last_error(chat_id),
        }

    def stats(self, chat_id: int) -> dict:
        self._chat(chat_id)
        return self.stats_for_chats([chat_id])

    def stats_for_chats(self, chat_ids: list[int]) -> dict:
        """대화들의 턴을 실행 조건(GROUP_FIELDS)별로 묶어 사용량, 비용, 지연을 더한다.

        본 대화 비용(usd)에 LLM 검수 판정(meta.aux)과 요약(summaries.meta) 비용을 따로 더해
        total_usd로 보여 준다. 옛 메시지처럼 run에 없는 필드는 None으로 묶인다.
        """
        groups: dict[tuple, dict] = {}

        def group_for(run: dict) -> dict:
            key = tuple(json.dumps(run.get(f), sort_keys=True, ensure_ascii=False) for f in GROUP_FIELDS)
            if key not in groups:
                groups[key] = {
                    **{f: run.get(f) for f in GROUP_FIELDS},
                    "turns": 0, "input_total": 0, "cache_read": 0, "cache_write": 0, "output": 0,
                    "usd": 0.0, "no_cache_usd": 0.0, "has_cost": False,
                    "judge_calls": 0, "judge_usd": 0.0, "summary_calls": 0, "summary_usd": 0.0,
                    "quality_turns": 0, "foreign_turns": 0, "leak_turns": 0, "speech_shift_turns": 0, "flagged_turns": 0,
                    "_ttft": [], "_first_visible": [], "_foreign": 0.0, "_latin": 0.0, "_repeat_prev": 0.0,
                }
            return groups[key]

        def add_judges(group: dict, judges: list[dict]) -> None:
            for entry in judges:
                group["judge_calls"] += 1
                if entry.get("cost"):
                    group["has_cost"] = True
                    group["judge_usd"] += entry["cost"]["usd"]

        ids = [int(i) for i in chat_ids]
        marks = ",".join("?" for _ in ids)
        rows = self.db.q(
            f"SELECT role, meta FROM messages WHERE chat_id IN ({marks}) AND role IN ('assistant', 'notice') "
            "ORDER BY id", ids) if ids else []
        for row in rows:
            meta = json.loads(row["meta"] or "{}")
            run, usage = meta.get("run"), meta.get("usage")
            judges = [e for e in meta.get("aux") or [] if str(e.get("purpose", "")).startswith("judge")]
            # 안내(notice)는 검수 비용만 더한다. 더할 것이 없는 메시지로는 빈 행을 만들지 않는다
            is_turn = row["role"] == "assistant" and bool(usage)
            if not run or not (is_turn or judges):
                continue
            group = group_for(run)
            add_judges(group, judges)
            if not is_turn:
                continue
            group["turns"] += 1
            for field in ("input_total", "cache_read", "cache_write", "output"):
                group[field] += usage.get(field, 0) or 0
            if meta.get("cost"):
                group["has_cost"] = True
                group["usd"] += meta["cost"]["usd"]
                group["no_cache_usd"] += meta["cost"]["no_cache_usd"]
            latency = meta.get("latency") or {}
            if latency.get("ttft_ms") is not None:
                group["_ttft"].append(latency["ttft_ms"])
            if latency.get("first_visible_ms") is not None:
                group["_first_visible"].append(latency["first_visible_ms"])
            measured = meta.get("quality")  # 품질 지표를 남기기 전의 턴에는 없다
            if measured:
                group["quality_turns"] += 1
                group["_foreign"] += measured.get("foreign_ratio") or 0
                group["foreign_turns"] += bool(measured.get("foreign_ratio"))  # 비율이 낮아도 한 글자라도 낀 턴
                group["_latin"] += measured.get("latin_ratio") or 0
                group["_repeat_prev"] += measured.get("repeat_prev") or 0
                group["leak_turns"] += bool(measured.get("leak"))
                group["speech_shift_turns"] += bool(measured.get("speech_shift"))
                group["flagged_turns"] += bool(measured.get("flags"))

        # 답이 저장되지 않은 턴(첫 글자 전 오류, 보인 글 없이 멈춤)의 판정 비용은 프롬프트 기록에 있다
        unsaved = self.db.q(
            "SELECT json_extract(payload, '$.run') AS run, json_extract(payload, '$.aux') AS aux FROM prompt_logs "
            f"WHERE chat_id IN ({marks}) AND kind='chat' AND message_id IS NULL "
            "AND json_extract(payload, '$.aux') IS NOT NULL", ids) if ids else []
        for row in unsaved:
            run, judges = json.loads(row["run"] or "null"), json.loads(row["aux"] or "[]")
            judges = [e for e in judges if str(e.get("purpose", "")).startswith("judge")]
            if not run or not judges:
                continue
            add_judges(group_for(run), judges)

        summaries = self.db.q(f"SELECT meta FROM summaries WHERE chat_id IN ({marks}) ORDER BY id", ids) if ids else []
        for row in summaries:
            meta = json.loads(row["meta"] or "{}")
            if not meta.get("run"):  # 실행 조건을 남기기 전의 옛 요약은 어느 행인지 알 수 없다
                continue
            group = group_for(meta["run"])
            group["summary_calls"] += 1
            if meta.get("cost"):
                group["has_cost"] = True
                group["summary_usd"] += meta["cost"]["usd"]

        def average(values: list) -> int | None:
            return round(sum(values) / len(values)) if values else None

        def median(values: list) -> int | None:
            if not values:
                return None
            ordered, mid = sorted(values), len(values) // 2
            return round(ordered[mid] if len(values) % 2 else (ordered[mid - 1] + ordered[mid]) / 2)

        result = []
        for group in groups.values():
            # 로컬 모델은 첫 턴에 모델을 올리는 시간이 섞여 평균이 치우치므로 중앙값도 함께 낸다
            group["median_ttft_ms"] = median(group["_ttft"])
            group["median_first_visible_ms"] = median(group["_first_visible"])
            group["avg_ttft_ms"] = average(group.pop("_ttft"))
            group["avg_first_visible_ms"] = average(group.pop("_first_visible"))
            group["avg_input"] = round(group["input_total"] / group["turns"]) if group["turns"] else 0
            group["cache_hit_ratio"] = round(group["cache_read"] / group["input_total"], 4) if group["input_total"] else 0
            for field in ("usd", "no_cache_usd", "judge_usd", "summary_usd"):
                group[field] = round(group[field], 8)
            group["total_usd"] = round(group["usd"] + group["judge_usd"] + group["summary_usd"], 8)
            measured = group["quality_turns"]
            for source, target in (("_foreign", "avg_foreign_ratio"), ("_latin", "avg_latin_ratio"),
                                   ("_repeat_prev", "avg_repeat_prev")):
                total = group.pop(source)
                group[target] = round(total / measured, 4) if measured else None
            result.append(group)
        varying = [f for i, f in enumerate(GROUP_FIELDS) if len({key[i] for key in groups}) > 1]
        return {"varying": varying, "groups": result}
