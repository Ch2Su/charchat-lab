# 개선 계획: 실험 기반 다지기

작성 2026-11-02. 구현은 다른 세션(Sonnet 5.5)이 이 문서만 보고 진행할 수 있게 썼어요.

## 목표

이 앱은 "설정을 바꿔 가며 같은 대화를 보내고 숫자를 비교하는" 실험실이에요. 그런데 지금은

- 비교 탭이 사고 설정·입력 예산·프롬프트 파일 버전을 구분하지 못해서 서로 다른 조건의 결과가 한 줄로 섞이고,
- LLM 검수와 요약 호출 비용이 집계에서 빠지고,
- README의 비교표를 손으로 15턴씩 보내서 만들어야 하고,
- 작은 모델의 답이 "무너지는" 정도(외국 문자, 말투 흔들림, 반복)를 눈으로만 판단해요.

네 단계로 이걸 고쳐요. 순서대로 진행하고, 단계마다 커밋을 하나씩 만들어요.

| 단계 | 내용 | 스키마 변경 | 주요 파일 |
|---|---|---|---|
| 1 | DB 마이그레이션 틀 | 틀만(버전 1) | `app/db.py` |
| 2 | 실행 조건 기록 + 숨은 비용 집계 | 없음(meta JSON) | `app/runinfo.py`(새), `chat_service.py`, `memory.py`, `moderation.py`, `providers/*`, `static/app.js` |
| 3 | 대본 재생 실험 실행기 | 버전 2: `experiments` 표 | `app/experiments.py`(새), `main.py`, `static/*` |
| 4 | 답변 품질 지표 | 없음(meta JSON) | `app/quality.py`(새), `chat_service.py`, `static/app.js` |

이번 범위 밖: 로어 재귀 스캔, 벡터 기억, 검수 정규화·평가 세트, 대화 트리 보기 등(README "더 해 볼 만한 것"과 맨 아래 "나중에" 참고).

---

## 구현자에게: 공통 규칙

**시작 전에 읽을 것**: `README.md`의 "구조"와 "한 번의 요청은 이렇게 생겼어요", 그리고 이 문서.

**테스트 실행** (이 저장소의 가상환경은 메인 체크아웃에 있어요)

```bash
E:/charchat-lab/.venv/Scripts/python.exe -m pytest -q
```

- 작업 시작 시점 기준선: **50 passed**.
- 단계를 마칠 때마다 전체 테스트가 통과해야 커밋해요. 기존 테스트를 고쳐야 한다면 이유를 커밋 메시지에 적어요.

**직접 띄워 보기** (메인 체크아웃의 서버가 8000번을 쓰고 있을 수 있어요)

```bash
PORT=8001 PROVIDER=mock E:/charchat-lab/.venv/Scripts/python.exe run.py
```

워크트리에는 `.env`와 `data/`가 없어서 mock 제공사와 새 DB(`./data/lab.sqlite3`)로 떠요. 메인 체크아웃의 `.env`와 `data/lab.sqlite3`는 건드리지 마세요.

**코드 스타일**

- 기존 코드처럼 모듈 맨 위 docstring에 "왜 이렇게 하는지"를 한국어로 쓰고, 코드 주석은 "~한다"체로 짧게 써요.
- 화면 문구는 해요체(예: "실험이 끝났어요")로 써요.
- 새 의존성을 추가하지 않아요. 프론트는 빌드 없는 바닐라 JS를 유지해요(`static/app.js`의 `$`, `api`, `escapeHtml`, `note`, `toast` 유틸 재사용).
- 기본값은 전체이용가 기준(검수 block 등)을 그대로 둬요.

**호환성**

- 이미 저장된 메시지의 `meta`에는 새 필드가 없어요. 읽는 쪽은 언제나 "없으면 None"으로 다뤄야 해요.
- SSE 이벤트 이름과 기존 필드(`user`, `meta`, `thinking`, `delta`, `reset`, `retract`, `notice`, `error`, `done`)는 바꾸지 않아요. 필드 추가는 괜찮아요.
- `/api/chats/{id}/stats` 응답의 기존 필드(`turns`, `avg_input`, `cache_hit_ratio`, `usd`, `has_cost`, `avg_ttft_ms` 등)는 유지해요.

**커밋**: 단계마다 한국어 커밋 메시지로 하나씩. push는 하지 않아요. README의 관련 절(API 표, 실험 가이드)도 같은 커밋에서 갱신해요.

---

## 단계 1. DB 마이그레이션 틀

### 왜

`app/db.py`는 `SCHEMA`를 `CREATE TABLE IF NOT EXISTS`로만 실행해요(`DB.__init__`, db.py:144). 그래서 표나 열을 추가하면 이미 만들어진 `lab.sqlite3`에는 반영되지 않아요. 단계 3에서 표를 추가하므로 그 전에 버전 관리 틀을 넣어요.

### 설계

`PRAGMA user_version`으로 스키마 버전을 관리해요.

```python
SCHEMA_VERSION_BASE = 1  # 지금의 SCHEMA가 버전 1이다

# (버전, [SQL 문장, ...]). 버전은 2부터 1씩 올린다. 이미 배포된 항목은 고치지 않고 새 항목을 덧붙인다.
MIGRATIONS: list[tuple[int, list[str]]] = []
```

`DB.__init__`에서 `executescript(SCHEMA)` 다음에 `self._migrate()`를 불러요.

```
_migrate():
  current = PRAGMA user_version
  latest = max(버전 in MIGRATIONS, 기본 SCHEMA_VERSION_BASE)
  if current > latest: RuntimeError("이 DB는 더 새로운 버전의 코드로 만들어졌어요 (DB {current}, 코드 {latest}).")
  if current == 0: current = 1, PRAGMA user_version = 1   # 새 DB이거나 마이그레이션 도입 전의 DB
  for version, statements in MIGRATIONS (version 오름차순):
      if version <= current: continue
      with self.tx():
          for sql in statements: self.conn.execute(sql)
          self.conn.execute(f"PRAGMA user_version = {version}")
```

주의할 점

- **`executescript`를 트랜잭션 안에서 쓰면 안 돼요.** sqlite3 모듈이 실행 전에 암묵적으로 COMMIT을 해서 롤백이 깨져요. 문장 하나씩 `execute`해요.
- 연결이 `isolation_level=None`(autocommit)이라 `self.tx()`의 `BEGIN/COMMIT`이 그대로 동작해요. 실패하면 `user_version`도 함께 롤백되는지 테스트로 확인해요.
- `:memory:` DB에서도 동작해야 해요.
- 지금 `MIGRATIONS`는 빈 목록이에요. 실제 첫 항목(버전 2)은 단계 3에서 추가해요.

### 테스트: `tests/test_db_migrations.py` (새 파일)

1. 새 DB를 열면 `user_version == 1`이에요(MIGRATIONS가 비어 있을 때).
2. `monkeypatch`로 `app.db.MIGRATIONS = [(2, ["CREATE TABLE t2(x INTEGER)"])]`로 바꾸고, 버전 1의 DB 파일을 다시 열면 `t2`가 생기고 `user_version == 2`예요. 한 번 더 열어도 오류가 없어요.
3. 실패하는 마이그레이션(`[(2, ["CREATE TABLE t2(x)", "SYNTAX ERROR"])]`)은 예외를 던지고, `t2`가 없고, `user_version`이 1 그대로예요.
4. `user_version`을 99로 올린 DB를 열면 RuntimeError가 나요.

### 완료 조건

- 기존 50개 + 새 테스트 통과.
- 메인 체크아웃의 `data/lab.sqlite3`를 **복사본으로** 열어 봤을 때 `user_version`이 1이 되고 데이터가 그대로예요(원본은 건드리지 않아요).

---

## 단계 2. 실행 조건 기록 + 숨은 비용 집계

### 왜

- 비교 탭은 `(provider, model, strategy, cache_mode)` 네 값으로만 행을 나눠요(`ChatService.stats`, chat_service.py:470). 실험 2처럼 입력 예산을 바꾼 결과나 실험 8처럼 사고를 켰다 끈 결과가 한 행에 섞여요.
- `.env`로만 바꾸는 사고 설정(`ANTHROPIC_EFFORT`, `ANTHROPIC_THINKING`, `GEMINI_THINKING_LEVEL`, `OPENAI_COMPAT_REASONING_EFFORT`, `OPENAI_COMPAT_TOP_P` 등)은 턴 기록(`meta.run`, chat_service.py:213)에 남지 않아요.
- `prompts/*.txt`를 고친 전후도 구분되지 않아요.
- LLM 검수(`moderation.llm_judge`)는 호출 사용량을 버리고(moderation.py:102), 요약 비용은 `summaries.meta`에만 있어서 비교 탭의 비용에 빠져요.

### 2-1. 실행 정보 모으기: `app/runinfo.py` (새 파일)

`chat_service`와 `memory`가 함께 쓰므로 순환 import를 피해 별도 모듈로 둬요.

```python
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
        "options": provider.options(),          # .env로만 바꾸는 제공사 옵션
        "prompt_version": prompts_fingerprint(),
    }
```

- `_generate`의 `run` 딕셔너리(chat_service.py:213)를 `run_info(settings, provider)`로 바꿔요. 기존 키는 모두 남아요.
- **`Provider.options()`**: `providers/base.py`에 기본 구현 `return {}`을 두고, 각 어댑터가 요청을 만들 때 실제로 읽는 config 값을 돌려줘요. 각 어댑터의 `render()`/`stream()`이 읽는 값과 같은지 직접 확인하세요.
  - anthropic: `effort`, `thinking`, `thinking_headroom`
  - gemini: `thinking_level`, `thinking_headroom`, `safety_threshold`
  - openai_compat: `reasoning_effort`, `top_p`, `max_tokens_param`
  - mock: `delay_scale`, `cache_min_tokens`, `cache_ttl`
- **`prompts_fingerprint()`**: `app/prompt.py`에 추가해요. `prompts/*.txt`를 이름순으로 읽어 `이름 + "\0" + 내용`을 이어 붙인 sha1의 앞 8자예요. `load_prompt`가 매번 파일을 다시 읽는 것과 같은 이유로 캐시하지 않아요(파일이 몇 개 안 되고 작아요).
- (선택) `main.py`의 `get_config`가 제공사별 옵션을 손으로 나열하는 부분(main.py:151-162)은 그대로 둬도 돼요.

### 2-2. 실험 라벨 설정: `run_label`

- `config.SETTINGS_SPEC`에 `"run_label": {"type": "text", "max_len": 40, "default": ""}`를 추가해요.
- `coerce_setting`에 `text` 형식을 추가해요: `None`이면 `""`, 아니면 `str(value).strip()[:max_len]`.
- 화면: `static/app.js`의 `settingField`(app.js:1018)에 `spec.type === 'text'`이면 `<input type="text" maxlength=...>`를 그리는 분기를 추가해요. `FIELD_INFO`에 `run_label: { label: '실험 라벨', help: '비교 탭에서 이 이름으로 행을 나눠요. 비우면 라벨 없이 기록해요.' }`, `SETTING_GROUPS`의 적당한 묶음에 넣어요. `submitSettings`가 빈 문자열도 보내는지 확인하세요.

### 2-3. 보조 호출(검수 판정) 비용 기록

- `ModResult`(moderation.py:31)에 `usage: dict | None = None`, `provider: str | None = None`, `model: str | None = None`을 추가해요. `llm_judge`가 성공하면 `usage=result.usage.to_dict()`, `provider=provider.name`, `model=model`을 채워요. 실패(fail-open)하면 `None`이에요.
- `chat_service.py`에 도우미를 둬요.

```python
def aux_entry(purpose: str, check: ModResult | None) -> dict | None:
    """LLM 판정처럼 대화 밖에서 부른 호출의 사용량과 비용. 판정을 안 했으면 None."""
    if not check or check.backend != "llm" or not check.usage:
        return None
    return {"purpose": purpose, "provider": check.provider, "model": check.model,
            "usage": check.usage, "cost": pricing.cost(check.usage, check.provider, check.model),
            "latency_ms": round(check.latency_ms, 1)}
```

- `_generate`의 `base_meta`에 `"aux": [...]`를 넣어요. 들어갈 수 있는 건 입력 판정 `judge_input`(`input_check`)과 출력 판정 `judge_output`(`output_check`)이에요. 출력 판정은 저장 직전에 정해지므로 최종 `meta`를 만들 때 덧붙여요. `None`은 빼요.
- 입력이 막힌 경우(`_send_flow`의 notice 경로, chat_service.py:169-178)에는 assistant 메시지가 없어요. notice 메시지의 `meta`에 `"run": run_info(...)`와 `"aux": [...]`를 함께 저장해요. 집계는 2-5에서 notice도 읽어요.
- 카드 등록 검수(`main.py`의 `moderate_card`)는 대화에 속하지 않으니 기록하지 않아요.

### 2-4. 요약 비용 기록

`memory.summarize`(memory.py:72)의 summary `meta`에 두 개를 추가해요.

- `"run": run_info(settings, provider)`: 어떤 조건의 대화에서 생긴 요약인지 알려 줘요. 여기서 `model`은 대화 모델이에요.
- `"cost": pricing.cost(result.usage.to_dict(), provider.name, model)`: 실제 요약 모델(`SUMMARY_MODEL`이 있으면 그것) 기준의 비용이에요.

### 2-5. 집계 다시 쓰기

`ChatService.stats(chat_id)`를 `stats_for_chats(chat_ids: list[int])`로 일반화하고, 기존 `stats(chat_id)`는 `stats_for_chats([chat_id])`를 부르게 해요(단계 3에서 재사용).

행을 나누는 기준:

```python
GROUP_FIELDS = ("label", "provider", "model", "strategy", "cache_mode", "input_token_budget",
                "max_output_tokens", "temperature", "moderation_backend", "output_buffer",
                "options", "prompt_version")
```

- 키는 `tuple(json.dumps(run.get(f), sort_keys=True, ensure_ascii=False) for f in GROUP_FIELDS)`처럼 만들어요(options가 dict이라서).
- 옛 메시지에는 일부 필드가 없어서 `None`으로 묶여요. 그래도 괜찮아요.

읽는 것:

- `role IN ('assistant', 'notice')`이고 `meta.run`이 있는 메시지.
  - assistant: 지금처럼 턴, 사용량, 비용, 지연을 더하고, `meta.aux`를 보조 비용에 더해요.
  - notice: `meta.aux`만 더해요. 턴 수에는 넣지 않아요.
- 해당 대화들의 `summaries` 중 `meta.run`이 있는 것은 그 run의 그룹에 요약 비용을 더해요. `meta.run`이 없는 옛 요약은 건너뛰어요.

그룹마다 추가할 필드:

| 필드 | 뜻 |
|---|---|
| GROUP_FIELDS의 각 값 | 그 그룹의 조건(`options`는 dict 그대로) |
| `judge_calls`, `judge_usd` | LLM 검수 판정 호출 수와 비용 합 |
| `summary_calls`, `summary_usd` | 요약 호출 수와 비용 합 |
| `total_usd` | `usd + judge_usd + summary_usd` |
| `avg_first_visible_ms` | 첫 표시 평균(실험 5의 출력 버퍼 비교용) |

응답 맨 위에 `"varying": [...]`를 추가해요. GROUP_FIELDS 중 그룹마다 값이 다른 필드 이름 목록이에요. 화면은 이 필드만 보여 줘요.

`has_cost`는 본 대화·검수·요약 중 하나라도 비용이 계산되면 true예요.

### 2-6. 화면

- **비교 탭**(`renderCompareTab`, app.js:865)
  - 표 그리기를 `compareTable(groups, varying)` 함수로 떼어 내요(단계 3에서 재사용).
  - "설정" 칸: 라벨이 있으면 굵게 먼저, 그다음 모델, 그다음 `varying`에 든 필드만 `이름 값`으로 나열해요. 필드 이름은 `FIELD_INFO`의 label을 쓰고, `options`는 `effort low`처럼 펼쳐요. `prompt_version`은 "프롬프트 a1b2c3d4"로 보여 줘요.
  - "비용" 칸은 `total_usd`를 보여 주고, 검수나 요약 비용이 있으면 아래에 작게 `대화 $… · 요약 $… · 검수 $…`를 붙여요.
  - "첫 표시" 칸(`avg_first_visible_ms`)을 추가해요.
  - 안내 문구(app.js:894-895)를 새 기준에 맞게 고쳐요.
- **이번 턴 탭**의 비용 블록(app.js:773 근처): `meta.aux`가 있으면 "검수 판정" 줄에 비용과 지연을 보여 줘요.

### 테스트

`tests/test_api.py`의 기존 `test_manual_summary_and_stats`는 그대로 통과해야 해요. 새 테스트(같은 파일 또는 `tests/test_stats.py`):

1. 같은 대화에서 `input_token_budget`만 바꿔 두 번씩 보내면 stats 그룹이 둘이고, `varying == ["input_token_budget"]`이에요.
2. `run_label="A"`로 두 턴, `"B"`로 두 턴 보내면 그룹이 라벨별로 나뉘어요.
3. `moderation_backend="llm"`으로 보내면 `meta.aux`에 `judge_input`과 `judge_output`이 있고, mock 가격표(`mock:*`) 덕분에 `cost`가 계산돼요. stats 그룹의 `judge_calls == 2 × 턴 수`이고 `total_usd > usd`예요.
4. 수동 요약(`POST /api/chats/{id}/summarize`) 뒤 stats 그룹의 `summary_calls == 1`, `summary_usd > 0`이에요.
5. `prompts_fingerprint()`는 프롬프트 폴더를 임시 폴더로 바꿔(monkeypatch `config.PROMPTS_DIR`) 파일 하나를 고치면 값이 달라져요.
6. `coerce_setting("run_label", "  x  ")`은 `"x"`, 41자 이상은 40자로 잘려요.
7. `meta.run`이 없는 옛 메시지(직접 `db.add_message`로 넣기)가 섞여 있어도 stats가 오류 없이 동작해요.

### 완료 조건

- 테스트 통과.
- mock으로 띄워 같은 대화에서 입력 예산을 바꿔 보내면 비교 탭이 두 행으로 나뉘고, "설정" 칸에 입력 예산이 보여요.
- 검수 방식을 llm으로 두고 보내면 비교 탭에 검수 비용이 따로 보여요.
- README: 비교 탭 설명, 실험 2·5·8 안내에 "실험 라벨" 사용법을 한 줄씩 추가해요.

---

## 단계 3. 대본 재생 실험 실행기

### 왜

README 실험 2의 표는 "설정 바꾸기 → mock 캐시 비우기 → 새 대화 → 16턴 손으로 보내기"를 전략마다 반복해서 만들었어요. 사용자 입력 목록 하나를 여러 설정(변형)으로 자동 재생하고, 결과를 단계 2의 비교 표로 바로 보여 줘요.

### 3-1. 스키마(마이그레이션 버전 2)

```sql
CREATE TABLE experiments(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL DEFAULT '',
  character_id INTEGER REFERENCES characters(id) ON DELETE SET NULL,
  spec TEXT NOT NULL,                  -- 요청 원본(아래 형식)
  status TEXT NOT NULL,                -- running | done | error | cancelled | interrupted
  progress TEXT NOT NULL DEFAULT '{}', -- {"variant": i, "variants": n, "turn": j, "turns": m}
  runs TEXT NOT NULL DEFAULT '[]',     -- [{"label", "chat_id", "status", "turns_done", "blocked", "error"}]
  error TEXT,
  created_at REAL NOT NULL,
  finished_at REAL
)
```

`db.py`에 `create_experiment`, `get_experiment`, `list_experiments`, `update_experiment(**fields)`, `delete_experiment` 메서드를 추가해요. JSON 열은 기존 `_dumps`/`_loads`를 써요.

### 3-2. 요청 형식

```json
{
  "name": "창 전략 비교",
  "character_id": 2,
  "persona_name": "민준",
  "persona_desc": "",
  "inputs": ["안녕하세요", "책 구경해도 돼요?", "..."],
  "variants": [
    {"label": "full", "settings": {"window_strategy": "full"}},
    {"label": "anchored", "settings": {"window_strategy": "anchored"}}
  ],
  "reset_mock": true,
  "seed": 7,
  "wait_summary": true,
  "cooldown_seconds": 0
}
```

검증 실패는 400과 이유를 돌려줘요(`TurnError` 재사용).

- `inputs`: 1~100개, 각각 앞뒤 공백을 잘라 1~2000자.
- `variants`: 1~6개. `label`은 1~40자이고 서로 달라야 해요.
- `settings`의 키는 `SETTINGS_SPEC`에 있어야 하고 `run_label`은 쓸 수 없어요(label이 대신해요). 값은 `config.coerce_setting`으로 검사해요.
- 변형마다 최종 설정은 `config.build_settings({**db.get_setting_overrides(), **variant["settings"], "run_label": label})`이에요. 그 제공사가 `available_providers()`에 있어야 하고 `settings.model`이 비어 있지 않아야 해요.
- `cooldown_seconds`: 0~600. 실제 제공사는 캐시를 비울 수 없으니(Anthropic 캐시 TTL 기본 5분), 변형 사이에 기다려서 앞 변형의 캐시 영향을 없앨 때 써요.
- 이미 실행 중인 실험이 있으면 409예요. mock 캐시를 함께 쓰므로 한 번에 하나만 돌려요.

### 3-3. 실행기: `app/experiments.py` (새 파일)

```python
class ExperimentRunner:
    def __init__(self, db: DB, service: ChatService): ...
    def start(self, spec: dict) -> int            # 검증 → 행 만들기 → asyncio 작업 시작 → id
    def cancel(self, experiment_id: int) -> bool
    async def shutdown(self) -> None              # 서버 종료 시 진행 중인 작업 취소
    async def _run(self, experiment_id: int, spec: dict, variants: list[tuple[str, Settings]]) -> None
```

`_run`의 흐름:

```
for i, (label, settings) in enumerate(variants):
    if i > 0 and cooldown: await asyncio.sleep(cooldown)
    if reset_mock and settings["provider"] == "mock": providers.reset_mock(seed)
    chat_id = db.create_chat(character_id, persona_name, persona_desc, title=f"[실험] {name} · {label}")
    runs[i] = {"label", "chat_id", "status": "running", "turns_done": 0, "blocked": 0, "error": None}
    for j, text in enumerate(inputs):
        async with aclosing(service.send(chat_id, text, settings=settings)) as stream:
            async for _ in stream: pass
        leaf = db.get_message(db.get_chat(chat_id)["active_leaf_id"])
        notice → blocked += 1 하고 계속
        user(답이 저장되지 않은 오류) 또는 assistant인데 meta.status == "error"
          → runs[i].status = "error", error 기록, 이 변형은 멈추고 다음 변형으로
        if wait_summary: await memory.wait_chat(chat_id)
        progress 갱신(db.update_experiment)
    runs[i].status = "done"(오류가 없었으면)
status = "done". 처리하지 못한 예외는 status="error"와 error 메시지로 남겨요.
asyncio.CancelledError → status = "cancelled", finished_at 기록 후 다시 raise
```

필요한 주변 변경:

- **`ChatService.send(chat_id, content, settings: Settings | None = None)`**: `settings`가 오면 `self.get_settings()` 대신 그것을 써요. `_send_flow`, `_generate`, `_maybe_summarize`, `memory.schedule`은 이미 settings를 인자로 넘기므로 그대로 변형 설정을 따라가요.
- **`memory.wait_chat(chat_id, timeout=120.0)`**: `is_running(chat_id)`이 거짓이 될 때까지 `asyncio.sleep(0.05)`로 기다려요. 시간이 넘으면 그냥 돌아와요.
- **`providers.reset_mock(seed: int | None)`**: `get_provider("mock")`으로 인스턴스를 얻어 캐시를 비우고, seed가 있으면 `provider.rng.seed(seed)`로 다시 심어요. 변형마다 같은 mock 답변 순서가 나와야 비교가 공정해요. 기존 `clear_mock_cache`는 그대로 둬요.
- 작업은 `memory.schedule`처럼 `asyncio.get_running_loop().create_task`로 만들고 `dict[int, asyncio.Task]`에 보관해요.

### 3-4. API (`app/main.py`)

| 메서드 | 경로 | 설명 |
|---|---|---|
| POST | `/api/experiments` | 시작. `{"id": ...}` |
| GET | `/api/experiments` | 목록(최근 순, spec의 inputs는 빼고 개수만) |
| GET | `/api/experiments/{id}` | 행 전체 + `"stats": service.stats_for_chats([runs의 chat_id들])` |
| POST | `/api/experiments/{id}/cancel` | 취소 |
| DELETE | `/api/experiments/{id}` | 기록만 지워요. 만들어진 대화는 남겨요 |

`create_app`에서 `runner = ExperimentRunner(db, service)`를 만들어 `app.state.experiments`에 두고, `lifespan`을 이렇게 바꿔요.

- 시작할 때: `UPDATE experiments SET status='interrupted', finished_at=? WHERE status='running'`(서버가 꺼지면서 끊긴 실험).
- 끝날 때: `await runner.shutdown()`을 `close_all()`보다 먼저 불러요.

(선택) `GET /api/experiments/{id}/export.csv`: stats 그룹을 CSV로 내려받아 README 표를 만들 때 써요.

### 3-5. 화면

- **오른쪽 패널에 "실험" 탭**을 추가해요(`index.html`의 탭 목록과 `tab-experiments` 본문, app.js의 탭 전환과 `renderExperimentsTab`). 비교 탭은 대화 하나 기준이고 실험은 여러 대화에 걸치므로 탭을 따로 둬요. 대화를 고르지 않아도 보여야 해요.
  - 위쪽: "새 실험" 버튼, 그 아래 최근 실험 목록(이름, 상태, 진행 `변형 1/2 · 턴 5/16`).
  - 실행 중인 실험이 있으면 1초마다 다시 불러와요(`pollSummary`처럼 탭이 보일 때만).
  - 끝난 실험을 누르면 단계 2의 `compareTable(stats.groups, stats.varying)`로 결과 표를 보여 주고, 변형마다 "대화 열기" 링크(`openChat(chat_id)`, 캐릭터 선택이 필요하면 `selectCharacter`를 먼저)를 둬요. 실행 중이면 "취소", 끝났으면 "기록 삭제" 버튼을 둬요.
- **새 실험 대화상자** `dlg-experiment`(`dlg-settings`와 같은 `modal wide` 모양)
  - 이름, 캐릭터(목록에서 선택), 페르소나 이름.
  - 입력 대본: textarea, 한 줄에 하나(빈 줄 무시). 아래에 "입력 N개 × 변형 M개 = 턴 N×M개"를 실시간으로 보여 줘요.
  - 변형 목록: 줄마다 라벨 입력 + 설정 JSON textarea(예: `{"window_strategy": "full"}`) + 삭제 버튼, 그리고 "변형 추가". 처음에는 빈 변형 하나(`{}`)를 둬요.
  - 체크박스 "변형마다 mock 캐시 비우고 난수 다시 심기"(기본 켬), "매 턴 요약이 끝날 때까지 기다리기"(기본 켬), 숫자 "변형 사이 대기(초)".
  - 지금 제공사나 변형 설정의 제공사가 mock이 아니면, 보내기 전에 `confirm("실제 API를 N번 호출해요. 계속할까요?")`로 한 번 물어요.
  - JSON 해석 오류는 그 변형 줄 아래에 표시하고 보내지 않아요. 서버 400 메시지는 `toast`로 보여 줘요.

### 테스트: `tests/test_experiments.py` (새 파일)

conftest의 `client`는 `MOCK_DELAY_SCALE=0`이라 빨라요. 결과는 `test_summary_runs_in_background`처럼 짧게 폴링해요.

1. 변형 둘(`full`, `anchored`, 공통 `input_token_budget=1200`) × 입력 6개 → `done`, runs 둘 다 `turns_done == 6`, 대화 두 개가 생기고 각 대화의 user 메시지가 6개예요. stats 그룹은 라벨별로 둘이고 각 `turns == 6`이에요.
2. 재현성: 같은 변형을 라벨만 달리해 두 번(`reset_mock=true`, 같은 seed) 돌리면 두 대화의 assistant 답변 내용과 `cache_read` 순서가 같아요.
3. 검증: 빈 inputs, 변형 7개, 중복 라벨, 모르는 설정 키, `run_label` 키, 설정되지 않은 제공사(`anthropic`)는 모두 400이에요.
4. 실행 중 두 번째 시작은 409예요. 입력을 많이 주고 `cancel`을 부르면 상태가 `cancelled`가 돼요. 0초 지연이라 너무 빨리 끝나면 `monkeypatch`로 `ChatService.send`를 느리게 감싸요.
5. 검수에 막히는 입력(`[[검수테스트]]`)이 섞여 있으면 `blocked == 1`이고 실험은 계속돼요.
6. 시작 시 정리: DB에 `status='running'` 행을 직접 넣고 `create_app`을 다시 만들면 `interrupted`가 돼요.

### 완료 조건

- 테스트 통과.
- mock으로 띄워 하람과 README 실험 2의 조건(입력 예산 3000, 16턴, full/sliding/anchored)으로 실험을 돌리면 실험 탭에 세 행이 나오고, 캐시 적중률이 README 표(77% / 61% / 71%)와 비슷한 경향이에요. mock 답변 내용이 README 작성 때와 달라서 숫자가 똑같을 필요는 없어요.
- README: "실험 가이드" 앞에 "대본 재생 실험" 절을 추가하고, 실험 2를 실행기로 하는 방법을 적어요. API 표에 새 경로를 추가해요.

---

## 단계 4. 답변 품질 지표

### 왜

README 실험 8의 관찰("한자·영어가 섞이고 인물의 성별·말투가 흔들림", "이상한 답이 대화 기록에 한 번 들어가면 따라 해서 점점 무너져요")을 숫자로 만들어, 비교 표와 실험 결과에서 모델·설정별로 비교할 수 있게 해요. 정답 판정이 아니라 어림 지표예요. 화면에도 그렇게 밝혀요.

### 4-1. `app/quality.py` (새 파일)

외부 라이브러리 없이 정규식과 집합 연산만 써요. 모든 함수는 순수 함수예요.

**문자 체계**

```python
def script_counts(text: str) -> dict:
    # hangul: 가-힣, ㄱ-ㆎ / han: 一-鿿, 㐀-䶿 / kana: ぀-ヿ / latin: A-Za-z
    # other: 위에 없고 str.isalpha()인 글자(키릴, 태국 문자 등)
```

- `foreign_ratio = (han + kana + other) / max(1, 전체 글자 수)`
- `latin_ratio = latin / max(1, 전체 글자 수)`: 이름이나 약어로 정상적으로 쓰일 수 있어서 따로 봐요.

**말투(존댓말/반말)**

이 앱의 기본 규칙(`prompts/main.txt`)은 "행동과 묘사는 *별표* 안에, 대사는 큰따옴표 안에"예요. 묘사는 "~했다"체라서 말투 판단에서 빼야 해요.

1. 큰따옴표(`"…"`, `“…”`) 안의 대사만 모아요. 대사가 없으면 `*…*` 부분을 지운 나머지를 써요.
2. 문장 끝(`[.!?…~]+`, 줄바꿈)으로 나누고, 각 문장의 마지막 한글 어절에서 끝의 문장부호와 기호를 떼요.
3. 존댓말 어미 `(요|니다|니까|세요|죠|십시오)$`를 **먼저** 보고(그래야 "니다"를 "다"로 잘못 세지 않아요), 아니면 반말 어미 `(다|야|어|아|지|냐|니|자|래|게|네|군|걸|거든|잖아)$`를 봐요.
4. `level`: 존댓말이 반말의 2배 이상이고 1개 이상이면 `"polite"`, 반말이 존댓말의 2배 이상이고 1개 이상이면 `"plain"`, 둘 다 0이면 `None`, 나머지는 `"mixed"`.

`speech_prev`는 직전 assistant 답변 최대 5개의 `level` 중 다수(동률이면 `None`)예요. `speech_shift`는 이번 `level`과 `speech_prev`가 하나는 polite, 하나는 plain일 때만 참이에요. mixed나 None은 변화로 보지 않아요.

**반복**

- `repeat_prev`: 공백을 하나로 줄이고 문장부호를 지운 뒤 글자 5-gram 집합을 만들어, 이번 답변의 5-gram 중 직전 assistant 답변에도 있는 비율이에요. 한국어는 어절이 붙어 바뀌어서 단어 n-gram보다 글자 n-gram이 나아요.
- `repeat_self`: 8자 이상 문장 중 같은 답변 안에서 정규화 후 똑같이 되풀이된 문장의 비율이에요(`1 - 고유 수 / 전체 수`).

**한데 모으기**

```python
FOREIGN_WARN = 0.01
LATIN_WARN = 0.05
REPEAT_PREV_WARN = 0.5
REPEAT_SELF_WARN = 0.3

def measure(text: str, previous: list[str]) -> dict:
    """previous: 같은 경로의 직전 assistant 답변들(오래된 것부터, 최대 5개)."""
    return {
        "chars": ..., "sentences": ...,
        "scripts": {...}, "foreign_ratio": ..., "latin_ratio": ...,
        "speech": {"polite": n, "plain": n, "level": ...}, "speech_prev": ..., "speech_shift": bool,
        "repeat_prev": ..., "repeat_self": ...,
        "flags": [...],  # "foreign" | "latin" | "speech_shift" | "repeat_prev" | "repeat_self"
    }
```

비율은 소수 넷째 자리까지 반올림해요.

### 4-2. 기록

`_generate`에서 최종 `content`와 `excluded`가 정해진 뒤, `add_message` 전에 넣어요(chat_service.py:389 근처).

```python
if not excluded and content.strip():
    previous = [m["content"] for m in prior if m["role"] == "assistant" and not m.get("excluded")][-5:]
    meta["quality"] = quality.measure(content, previous)
```

- `prior`는 `_generate` 앞부분(chat_service.py:191)에서 이미 구해 둔 값이에요. 인사말도 직전 답변으로 쳐요.
- 멈춤으로 일부만 저장되는 경로(aborted)와 직접 고친 답변(`edit_assistant`)에는 넣지 않아요.

### 4-3. 집계

`stats_for_chats`의 그룹에 추가해요. `meta.quality`가 있는 턴만 대상이에요.

- `quality_turns`: 품질이 기록된 턴 수
- `avg_foreign_ratio`, `avg_latin_ratio`, `avg_repeat_prev`
- `speech_shift_turns`: `speech_shift`가 참인 턴 수
- `flagged_turns`: `flags`가 하나라도 있는 턴 수

### 4-4. 화면

- **답변 아래 지표**(`metricSpans`, app.js:387): `meta.quality.flags`가 있으면 기존 `flag()` 모양으로 `외국 문자`, `영문 섞임`, `말투 변화`, `반복`을 붙여요.
- **이번 턴 탭**(`turnHtml`): "답변 품질(어림)" 블록에 외국 문자 비율, 영문 비율, 말투(이번/직전), 직전 답과 겹침, 답 안 반복을 보여 주고, `note('글자 모양과 어미만 보는 어림 지표예요. 판단은 직접 읽고 하세요.')`를 붙여요.
- **비교 표**(`compareTable`): "외국 문자", "반복", "말투 변화" 칸을 추가해요. `quality_turns`가 0인 그룹은 `–`로 보여 줘요. 실험 탭 결과에도 그대로 나와요.

### 4-5. mock 시험 명령

`providers/mock.py`의 `_reply`에 `/quality-test` 명령을 추가해요. 답변 끝에 한자 6자 이상과 영어가 섞인 대사 한 줄, 그리고 8자 이상의 똑같은 문장 세 번을 덧붙여서, 화면에서 깃발이 뜨는지 손으로 확인할 수 있게 해요. README의 mock 명령 목록에도 추가해요.

### 테스트: `tests/test_quality.py` (새 파일)

1. `script_counts`: "안녕 世界 hello"의 각 개수.
2. 말투: `'*그가 웃었다.* "어서 오세요. 천천히 보세요."'`는 묘사의 "웃었다"를 빼고 polite이고, `'"왔어? 앉아."'`는 plain이에요.
3. "합니다"가 plain이 아니라 polite로 세어져요(어미 순서 확인).
4. `speech_shift`: 직전 답변들이 polite인데 이번이 plain이면 참이고, 이번이 mixed면 거짓이에요.
5. `repeat_prev`: 같은 글이면 1.0, 전혀 다르면 0.0, 빈 직전 목록이면 0.0이에요.
6. `repeat_self`: 같은 문장 두 번 + 다른 문장 하나 → 1/3 근처.
7. API: mock으로 `/quality-test`를 보내면 `done` 메시지의 `meta.quality.flags`에 `foreign`이 있고 `repeat_self > 0`이에요(`repeat_self` 깃발은 mock 본문 문장 수에 따라 문턱을 넘지 않을 수 있어서 값만 확인해요). stats 그룹의 `flagged_turns >= 1`이에요.
8. 막힌 답(retract)이나 사고만 하고 빈 답(`/think` + 아주 작은 `max_output_tokens`로 재현이 어려우면 생략 가능)에는 `meta.quality`가 없어요. retract는 `test_output_retract_with_sentence_buffer`의 입력을 재사용해요.

### 완료 조건

- 테스트 통과.
- mock으로 `/quality-test`를 보내면 답변 아래에 깃발이 뜨고, 이번 턴 탭과 비교 탭에 숫자가 나와요.
- README 실험 8에 "비교 탭(또는 실험 실행기)의 외국 문자·말투 변화·반복 칸으로 모델과 사고 설정을 비교해 보세요"를 추가해요.
- (선택) 실제 Ollama 모델로 실험 8을 실행기로 돌려 본 결과가 있다면 README 표 옆에 지표 열을 추가해요. 없으면 생략해요.

---

## 단계별 확인표

| 단계 | 새 테스트 파일 | 화면에서 확인 | README |
|---|---|---|---|
| 1 | `test_db_migrations.py` | 없음 | 없음 |
| 2 | `test_stats.py`(또는 test_api.py에 추가) | 비교 탭 행 분리, 검수·요약 비용, 실험 라벨 설정 | 비교 탭 설명, 실험 2·5·8 |
| 3 | `test_experiments.py` | 실험 탭, 새 실험 대화상자, 취소, 대화 열기 | 대본 재생 실험 절, API 표 |
| 4 | `test_quality.py` | 깃발, 품질 블록, 비교 칸 | mock 명령, 실험 8 |

## 나중에 (이번 범위 밖)

- 검수: 한국어 우회 표현 정규화(띄어쓰기·기호 끼워 넣기, 초성, 전각), 라벨 붙인 평가 세트로 정밀도·재현율 측정, 최근 입력 여러 개를 합친 맥락 검수, fail-open/closed 설정.
- 로어북: 제외 키(NOT 조건), 재귀 스캔.
- 실험: 캐시 깨짐 지점 표시(직전 요청과 공통 앞부분 비교), 지시 삽입 깊이 옵션, 동적 매크로(`{{time}}`, `{{random::a::b}}`), 토큰 추정 자동 보정.
- 화면: 대화 트리 보기, 대화 내보내기·가져오기(JSONL), 페르소나 저장, 하루 비용 상한.
- README "더 해 볼 만한 것"의 항목들(벡터 기억, 1시간 캐시 TTL, 요약 분리, 여러 모델 동시 비교).
