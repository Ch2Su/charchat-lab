"""FastAPI 앱. 실행: python run.py (또는 uvicorn app.main:create_app --factory --reload)."""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config, moderation
from .cards import CardError, export_card, parse_card_bytes
from .chat_service import ChatService, TurnError
from .db import DB
from .experiments import ExperimentRunner, stats_csv
from .providers import ProviderError, clear_mock_cache, close_all, get_provider


class LoreIn(BaseModel):
    name: str = ""
    keys: list[str] = []
    secondary_keys: list[str] = []
    content: str = ""
    enabled: bool = True
    constant: bool = False
    selective: bool = False
    case_sensitive: bool = False
    insertion_order: int = 100
    priority: int = 10


class CharacterIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = ""
    personality: str = ""
    scenario: str = ""
    first_mes: str = ""
    mes_example: str = ""
    system_prompt: str = ""
    post_history_instructions: str = ""
    creator_notes: str = ""
    alternate_greetings: list[str] = []
    tags: list[str] = []
    lore: list[LoreIn] = []


class ChatIn(BaseModel):
    character_id: int
    persona_name: str = ""
    persona_desc: str = ""
    title: str = ""


class ChatPatch(BaseModel):
    title: str | None = None
    persona_name: str | None = None
    persona_desc: str | None = None


class SendIn(BaseModel):
    content: str


class RegenerateIn(BaseModel):
    message_id: int | None = None


class SwitchIn(BaseModel):
    message_id: int


class EditIn(BaseModel):
    content: str


def _stream(generator) -> StreamingResponse:
    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def seed_characters(db: DB) -> int:
    """캐릭터가 하나도 없으면 characters/ 폴더의 샘플 카드를 넣는다."""
    if db.list_characters():
        return 0
    count = 0
    for path in sorted(config.CHARACTERS_DIR.glob("*.json")):
        try:
            character, lore = parse_card_bytes(path.read_bytes(), path.name)
        except CardError:
            continue
        db.create_character(character, lore)
        count += 1
    return count


def create_app(db_path: str | Path | None = None, seed: bool = True) -> FastAPI:
    db = DB(db_path or config.DB_PATH)

    def get_settings():
        return config.build_settings(db.get_setting_overrides())

    service = ChatService(db, get_settings)
    runner = ExperimentRunner(db, service)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if seed:
            seed_characters(db)
        db.interrupt_running_experiments()  # 서버가 꺼지면서 끊긴 실험
        yield
        await runner.shutdown()  # 제공사 연결을 닫기 전에 진행 중인 실험부터 멈춘다
        await close_all()

    app = FastAPI(title="charchat-lab", lifespan=lifespan)
    app.state.db = db
    app.state.service = service
    app.state.experiments = runner

    @app.exception_handler(TurnError)
    async def _turn_error(_request, exc: TurnError):
        return JSONResponse({"detail": str(exc)}, status_code=exc.status)

    @app.exception_handler(CardError)
    async def _card_error(_request, exc: CardError):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(config.SettingsError)
    async def _settings_error(_request, exc: config.SettingsError):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    # ── 화면 ─────────────────────────────────────
    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(config.STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    app.mount("/static", StaticFiles(directory=config.STATIC_DIR), name="static")

    # ── 설정 ─────────────────────────────────────
    @app.get("/api/config")
    async def get_config():
        settings = get_settings()
        return {
            "providers": config.available_providers(),
            "default_models": config.DEFAULT_MODELS,
            "settings": settings,
            "model": settings.model,
            "spec": {
                key: {k: v for k, v in spec.items() if k != "default"}
                for key, spec in config.SETTINGS_SPEC.items()
            },
            "anthropic": {
                "effort": config.ANTHROPIC_EFFORT,
                "thinking": config.ANTHROPIC_THINKING,
            },
            "gemini": {
                "thinking_level": config.GEMINI_THINKING_LEVEL,
                "safety_threshold": config.GEMINI_SAFETY_THRESHOLD,
            },
            "openai_compat": {
                "reasoning_effort": config.OPENAI_COMPAT_REASONING_EFFORT,
                "top_p": config.OPENAI_COMPAT_TOP_P,
            },
        }

    @app.put("/api/settings")
    async def put_settings(body: dict):
        current = get_settings()
        for key, value in body.items():
            if key == "models":
                merged = {**(current.get("models") or {}), **(value or {})}
                db.set_setting("models", config.coerce_setting("models", merged))
                continue
            coerced = config.coerce_setting(key, value)
            if key == "provider" and not config.available_providers().get(coerced):
                raise HTTPException(400, f"'{coerced}' 제공사가 설정되지 않았어요. .env를 확인하고 서버를 다시 시작하세요.")
            db.set_setting(key, coerced)
        settings = get_settings()
        return {"settings": settings, "model": settings.model}

    @app.post("/api/mock/cache/clear")
    async def mock_cache_clear():
        return {"cleared": clear_mock_cache()}

    @app.post("/api/settings/reset")
    async def reset_settings():
        db.clear_settings()
        settings = get_settings()
        return {"settings": settings, "model": settings.model}

    # ── 캐릭터 ───────────────────────────────────
    async def moderate_card(data: dict, lore: list[dict], character_id: int | None = None) -> None:
        settings = get_settings()
        if settings["moderation_mode"] == "off":
            return
        fields = ("description", "personality", "scenario", "first_mes", "mes_example", "system_prompt",
                  "post_history_instructions")
        text = "\n".join(
            [data.get("name", "")]
            + [str(data.get(f, "")) for f in fields]
            + [str(g) for g in data.get("alternate_greetings", [])]
            + [str(e.get("content", "")) for e in lore]
        )
        try:
            provider = get_provider(settings["provider"])
            result = await moderation.check(text, settings, provider, config.MODERATION_MODEL or settings.model)
        except ProviderError:
            result = moderation.RULES.check(text)
        if not result.flagged:
            return
        action = "blocked" if settings["moderation_mode"] == "block" else "logged"
        db.add_mod_event(stage="card", backend=result.backend, category=result.category, action=action,
                         excerpt=result.matched or text[:200], detail=result.to_dict(), character_id=character_id)
        if action == "blocked":
            hint = f" (걸린 표현: '{result.matched}')" if result.matched else ""
            raise TurnError(f"카드 내용이 검수 기준(분류: {result.category})에 걸려 저장하지 않았어요{hint}.")

    @app.get("/api/characters")
    async def list_characters():
        return db.list_characters()

    @app.post("/api/characters")
    async def create_character(body: CharacterIn):
        data = body.model_dump()
        lore = data.pop("lore")
        await moderate_card(data, lore)
        return db.get_character(db.create_character(data, lore))

    @app.post("/api/characters/import")
    async def import_character(file: UploadFile = File(...)):
        blob = await file.read()
        if len(blob) > 20 * 1024 * 1024:
            raise HTTPException(400, "파일이 너무 커요(20MB 제한).")
        character, lore = parse_card_bytes(blob, file.filename or "")
        await moderate_card(character, lore)
        return db.get_character(db.create_character(character, lore))

    @app.get("/api/characters/{character_id}")
    async def get_character(character_id: int):
        character = db.get_character(character_id)
        if not character:
            raise HTTPException(404, "캐릭터를 찾을 수 없어요.")
        return character

    @app.put("/api/characters/{character_id}")
    async def update_character(character_id: int, body: CharacterIn):
        if not db.get_character(character_id):
            raise HTTPException(404, "캐릭터를 찾을 수 없어요.")
        data = body.model_dump()
        lore = data.pop("lore")
        await moderate_card(data, lore, character_id)
        db.update_character(character_id, data, lore)
        return db.get_character(character_id)

    @app.delete("/api/characters/{character_id}")
    async def delete_character(character_id: int):
        db.delete_character(character_id)
        return {"ok": True}

    @app.get("/api/characters/{character_id}/export")
    async def export_character(character_id: int):
        character = db.get_character(character_id)
        if not character:
            raise HTTPException(404, "캐릭터를 찾을 수 없어요.")
        import json

        body = json.dumps(export_card(character), ensure_ascii=False, indent=2)
        filename = quote(f"{character['name']}.json")
        return Response(
            body,
            media_type="application/json; charset=utf-8",
            headers={"Content-Disposition": f"attachment; filename=\"character.json\"; filename*=UTF-8''{filename}"},
        )

    # ── 대화 ─────────────────────────────────────
    @app.get("/api/chats")
    async def list_chats(character_id: int | None = None):
        return db.list_chats(character_id)

    @app.post("/api/chats")
    async def create_chat(body: ChatIn):
        if not db.get_character(body.character_id):
            raise HTTPException(404, "캐릭터를 찾을 수 없어요.")
        chat_id = db.create_chat(body.character_id, body.persona_name.strip(), body.persona_desc.strip(),
                                 body.title.strip())
        return service.chat_view(chat_id)

    @app.get("/api/chats/{chat_id}")
    async def get_chat(chat_id: int):
        return service.chat_view(chat_id)

    @app.patch("/api/chats/{chat_id}")
    async def patch_chat(chat_id: int, body: ChatPatch):
        service.chat_view(chat_id)  # 존재 확인
        db.update_chat(chat_id, **{k: v for k, v in body.model_dump().items() if v is not None})
        return service.chat_view(chat_id)

    @app.delete("/api/chats/{chat_id}")
    async def delete_chat(chat_id: int):
        db.delete_chat(chat_id)
        return {"ok": True}

    def reject_mock_during_experiment() -> None:
        """mock을 쓰는 실험이 도는 동안 mock으로 다른 대화를 보내면 캐시와 난수 순서가 섞여 실험 결과가 흔들린다."""
        if runner.uses_mock() and get_settings()["provider"] == "mock":
            raise TurnError("mock을 쓰는 실험이 실행 중이에요. 캐시와 난수를 함께 써서 결과가 섞이니, "
                            "실험이 끝나거나 취소한 뒤에 보내세요.", 409)

    @app.post("/api/chats/{chat_id}/messages")
    async def send_message(chat_id: int, body: SendIn):
        reject_mock_during_experiment()
        return _stream(service.send(chat_id, body.content))

    @app.post("/api/chats/{chat_id}/regenerate")
    async def regenerate(chat_id: int, body: RegenerateIn):
        reject_mock_during_experiment()
        return _stream(service.regenerate(chat_id, body.message_id))

    @app.post("/api/chats/{chat_id}/switch")
    async def switch(chat_id: int, body: SwitchIn):
        service.switch(chat_id, body.message_id)
        return service.chat_view(chat_id)

    @app.post("/api/chats/{chat_id}/summarize")
    async def summarize(chat_id: int):
        return await service.summarize_now(chat_id)

    @app.get("/api/chats/{chat_id}/summaries")
    async def summaries(chat_id: int):
        return db.summaries_for_chat(chat_id)

    @app.get("/api/chats/{chat_id}/stats")
    async def stats(chat_id: int):
        return service.stats(chat_id)

    @app.get("/api/chats/{chat_id}/mod-events")
    async def chat_mod_events(chat_id: int):
        return db.list_mod_events(chat_id)

    @app.get("/api/mod-events")
    async def all_mod_events():
        return db.list_mod_events()

    @app.post("/api/messages/{message_id}/edit")
    async def edit_message(message_id: int, body: EditIn):
        message = db.get_message(message_id)
        if not message:
            raise HTTPException(404, "메시지를 찾을 수 없어요.")
        if message["role"] == "user":
            reject_mock_during_experiment()
            return _stream(service.edit_user(message_id, body.content))
        return service.edit_assistant(message_id, body.content)

    @app.get("/api/messages/{message_id}/prompt")
    async def message_prompt(message_id: int):
        log = db.prompt_log_for_message(message_id)
        if not log:
            raise HTTPException(404, "이 메시지의 프롬프트 기록이 없어요.")
        return log

    # ── 대본 재생 실험 ───────────────────────────
    def experiment_or_404(experiment_id: int) -> dict:
        experiment = db.get_experiment(experiment_id)
        if not experiment:
            raise HTTPException(404, "실험을 찾을 수 없어요.")
        return experiment

    def experiment_stats(experiment: dict) -> dict:
        return service.stats_for_chats([run["chat_id"] for run in experiment["runs"] if run.get("chat_id")])

    @app.post("/api/experiments")
    async def start_experiment(body: dict):
        return {"id": runner.start(body)}

    @app.post("/api/experiments/estimate")
    async def estimate_experiment(body: dict):
        return runner.estimate(body)

    @app.get("/api/experiments")
    async def list_experiments():
        rows = db.list_experiments()
        for row in rows:  # 목록에는 대본 본문 대신 개수만
            row["spec"]["input_count"] = len(row["spec"].pop("inputs", []))
        return rows

    @app.get("/api/experiments/{experiment_id}")
    async def get_experiment(experiment_id: int):
        experiment = experiment_or_404(experiment_id)
        return {**experiment, "stats": experiment_stats(experiment)}

    @app.post("/api/experiments/{experiment_id}/cancel")
    async def cancel_experiment(experiment_id: int):
        experiment_or_404(experiment_id)
        if not runner.cancel(experiment_id):
            raise HTTPException(409, "실행 중인 실험이 아니에요.")
        return {"ok": True}

    @app.delete("/api/experiments/{experiment_id}")
    async def delete_experiment(experiment_id: int):
        experiment_or_404(experiment_id)
        if runner.running_id() == experiment_id:
            raise HTTPException(409, "실행 중인 실험은 먼저 취소하세요.")
        db.delete_experiment(experiment_id)  # 기록만 지운다. 만들어진 대화는 남긴다
        return {"ok": True}

    @app.get("/api/experiments/{experiment_id}/export.csv")
    async def export_experiment(experiment_id: int):
        experiment = experiment_or_404(experiment_id)
        filename = quote(f"{experiment['name'] or '실험'}.csv")
        return Response(
            stats_csv(experiment_stats(experiment)["groups"]),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f"attachment; filename=\"experiment.csv\"; filename*=UTF-8''{filename}"},
        )

    return app
