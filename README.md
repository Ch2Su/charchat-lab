# charchat-lab

캐릭터 채팅 서비스가 속에서 어떻게 돌아가는지 직접 만져 보는 로컬 실험실이에요.
모델을 만드는 게 아니라, 모델 API 위에 얹는 부분(프롬프트 조립, 기억 관리, 캐시, 검수)을 구현하고 계측합니다.
기본 설정은 전체이용가 기준이에요.

- **API 키 없이 시작**: mock 제공사가 답변과 Anthropic식 프롬프트 캐시, 지연 시간까지 흉내 내요.
- **실제 모델 연결**: Claude, Gemini, OpenAI 호환 서버(Ollama, LM Studio, llama.cpp, vLLM 등)를 설정만 바꿔 끼워요.
- **매 턴 계측**: 프롬프트 구성(추정 토큰), 실제 사용량과 캐시 적중, 첫 토큰 지연, 비용, 로어 주입, 대화 창 상태를 오른쪽 패널에서 봐요.

## 빠른 시작

Python 3.10 이상이 필요해요.

**Windows (PowerShell)**

```powershell
cd charchat-lab
py -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
Copy-Item .env.example .env
.venv\Scripts\python run.py
```

**macOS / Linux**

```bash
cd charchat-lab
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
.venv/bin/python run.py
```

브라우저에서 <http://127.0.0.1:8000> 을 열어요. 처음 실행하면 샘플 캐릭터 둘(고서점 주인 '하람', 정거장 정비 로봇 '에코')이 들어가 있어요.
데이터는 `data/lab.sqlite3`에 저장돼요. 처음부터 다시 하고 싶으면 서버를 끄고 이 파일을 지우면 돼요.

개발하면서 코드 수정을 바로 반영하려면 `uvicorn app.main:create_app --factory --reload`로 실행하세요.

## 실제 모델 연결

`.env`를 고치고 서버를 다시 시작한 뒤, 화면의 **설정**에서 제공사를 고르면 돼요.

| 제공사 | .env에 넣을 것 | 참고 |
|---|---|---|
| Claude | `ANTHROPIC_API_KEY`, (선택) `ANTHROPIC_MODEL` | 기본 모델 `claude-haiku-5-5`, `ANTHROPIC_EFFORT=low` |
| Gemini | `GEMINI_API_KEY`, (선택) `GEMINI_MODEL` | 기본 모델 `gemini-3.8-flash`, `GEMINI_THINKING_LEVEL=low` |
| Ollama 등 | `OPENAI_COMPAT_BASE_URL=http://localhost:11434/v1`, `OPENAI_COMPAT_MODEL` | 키 없이 동작, `<think>` 구간은 숨김 |

제공사별로 알아 둘 점 (2026년 10월 공식 문서 기준)

- **현행 Claude 모델**(Haiku/Sonnet/Opus 5.5 등)은 temperature·top_p·top_k를 기본값이 아닌 값으로 보내면 400 오류가 나요. 그래서 이 앱은 Claude에는 temperature를 보내지 않아요.
- 현행 Claude 모델은 **사고(thinking)가 기본으로 켜져** 있고, 사고 토큰도 `max_tokens`에 포함돼요. 답변이 잘리지 않게 `ANTHROPIC_THINKING_HEADROOM`(기본 2048)만큼 더해서 보내요. 첫 토큰을 빠르게 하려면 `ANTHROPIC_EFFORT=low`를 쓰거나, Haiku 5.5라면 `ANTHROPIC_THINKING=disabled`를 써 보세요.
- **Claude 캐시 최소 길이는 512토큰**(현행 모델)이에요. 그보다 짧은 앞부분은 `cache_control`을 붙여도 오류 없이 캐시되지 않아요.
- **Gemini**는 조정 가능한 안전 필터가 기본으로 꺼져 있어서, 이 앱은 `GEMINI_SAFETY_THRESHOLD=BLOCK_MEDIUM_AND_ABOVE`로 네 범주를 켜서 보내요. 사고 토큰은 `max_output_tokens`에 포함되므로 여기도 여유분을 더해요. 캐시는 자동(암묵적)으로만 동작하고, 3.8 Flash 기준 앞부분이 4,096토큰 이상 같아야 적중해요.
- Gemini API 약관상 18세 미만이 이용할 가능성이 높은 서비스에는 쓸 수 없어요. 혼자 쓰는 로컬 실험은 괜찮지만, 공개한다면 약관부터 다시 확인하세요.

## 화면

- **왼쪽**: 캐릭터와 대화 목록. 캐릭터 카드(JSON, PNG)를 가져오거나 내보낼 수 있어요. SillyTavern 등에서 쓰는 Character Card V2/V3 형식이에요.
- **가운데**: 대화. 답변마다 `‹ 1/2 ›`로 다시 생성한 버전이나 고쳐 쓴 가지를 넘겨 볼 수 있어요. **멈춤**을 누르면 화면에 보인 만큼 저장돼요.
- **오른쪽 패널**
  - **이번 턴**: 프롬프트 구성 막대(고정/요약/대화/마지막 입력), 실제 입력 토큰과 캐시 읽기·쓰기, 추정 대비 실측 비율, 비용, 지연 시간, 대화 창, 로어북, 보낸 요청 원문
  - **비교**: 같은 대화에서 설정(모델, 창 전략, 캐시 방식)별로 평균 입력, 캐시 적중률, 비용, 첫 토큰을 나란히
  - **요약**: 지금 쓰이는 요약과 수동 요약 버튼
  - **검수 로그**: 어디서(입력/출력/카드), 무엇에, 어떻게(막음/기록/가림/위기 안내) 걸렸는지

mock 제공사에서는 입력에 테스트 명령을 넣을 수 있어요: `/long`(긴 답변), `/mod-test-output`(출력 검수 테스트), `/error`(스트리밍 도중 오류), `/slow`(느리게).

## 실험 가이드

mock으로도 대부분 해 볼 수 있어요. mock의 숫자는 흉내 낸 값이니, 경향을 확인한 뒤 실제 모델로 검증해 보세요.
설정을 비교하기 전에는 **비교** 탭의 **mock 캐시 비우기**를 눌러 앞선 실험의 캐시를 지우세요.

### 1. 프롬프트 캐시는 어디서 적중하나

1. 하람과 새 대화를 열고 서너 번 주고받아요. 오른쪽 **실제 입력 토큰**에서 '캐시에서 읽음' 비율을 봐요. 첫 턴은 0%(쓰기만), 다음 턴부터 오르는 게 정상이에요.
2. **다시 생성**을 눌러요. 같은 프롬프트라서 '캐시 안 됨'이 0에 가까워져요(마지막 입력 끝의 중단점 덕분).
3. 설정에서 **캐시 방식 → 자동**으로 바꾸고 몇 턴 더 보내 보세요. 읽기가 0에 머물고, 쓰기 비용(1.25배) 때문에 캐시를 끈 것보다 오히려 비싸져요.

왜 그럴까요? 이 앱은 매 턴 마지막 사용자 메시지에 로어와 지시(시스템 메모)를 붙여요. 자동 캐시는 마지막 블록에만 중단점을 두는데, 다음 턴에는 그 메시지가 메모 없이 다시 들어가서 서버가 저장해 둔 앞부분과 내용이 달라져요.
명시적 방식은 메모가 붙기 전인 '직전 대화 끝'에도 중단점을 둬서 이 문제를 피해요. 자동 캐시를 쓰려면 지난 턴의 메시지를 고치지 않는(append-only) 구조여야 해요.

### 2. 창 전략 비교: full, sliding, anchored

1. 설정에서 **입력 예산을 3000**으로 줄여요(차이가 빨리 보여요).
2. 창 전략을 하나 고르고 새 대화에서 15턴쯤 보낸 뒤, 전략을 바꿔 새 대화에서 같은 걸 반복해요.
3. **비교** 탭과 답변 아래의 캐시 비율을 봐요.

mock으로 하람과 16턴을 보냈을 때의 예(입력 예산 3000, 명시적 캐시, 실험마다 mock 캐시를 비움). 같은 조건에서 캐시 방식만 자동으로 바꾸면 적중 0%에 비용은 캐시를 끈 것보다 20% 높았어요.

| 전략 | 평균 입력 | 캐시 적중 | 턴별 모습 |
|---|---|---|---|
| full | 2,475 | 77% | 앞부분이 늘 같아서 적중률이 높지만 입력이 계속 늘어요 |
| sliding | 2,400 | 61% | 창이 밀리기 시작하면 매 턴 대화 앞부분이 바뀌어 40%대로 떨어져요 |
| anchored | 2,258 | 71% | 요약 직후 한 턴만 50%로 떨어졌다가 바로 70%대로 돌아와요 |

대화가 짧으면 full + 캐시가 가장 싸게 나올 수도 있어요. 대화가 길어질수록 full은 입력이 끝없이 늘고, anchored는 입력이 일정 범위에 머물러요.
입력 예산을 고정 프롬프트 크기에 너무 가깝게 잡으면 요약이 자주 일어나서(요약이 바뀔 때마다 그 뒤 캐시가 깨짐) 오히려 손해예요.

### 3. 요약 메모리가 무엇을 잊나

1. anchored에서 입력 예산을 3000으로 두고, 초반에 구체적인 사실(이름, 약속, 물건)을 몇 개 말해 둬요.
2. 대화를 이어 가다 요약이 생기면(**요약** 탭) 그 사실을 물어봐요.
3. `prompts/summary_system.txt`의 '[고정 사실]' 지시를 빼고 다시 해 보며 차이를 봐요.

요약은 답변이 끝난 뒤 백그라운드에서 만들어지고, 대화 가지마다 따로 적용돼요(요약이 어느 메시지까지 덮는지 기록해 두기 때문).

### 4. 로어북

- 하람에게 "저 고양이 이름이 뭐예요?"라고 물으면 '먹물' 항목이 마지막 입력에 들어가요(패널의 로어북 칩).
- '움직이는 문장'은 보조 키가 필요한 항목이에요. "자정에 문 닫으세요?"로는 안 걸리고, "자정이 되면 책 글자가 움직인다던데"처럼 보조 키(문장, 글자, 종이 위)가 최근 대화에 함께 나와야 걸려요.
- 키워드는 부분 문자열로 찾아요. 그래서 보조 키를 '종이'로 두면 인사말의 "종이 울린다"(종 + 조사 '이')에도 걸려요. 조사가 붙는 한국어에서 흔한 오탐이라 샘플에서는 '종이 위'로 좁혀 뒀어요. `/종이(?! 울)/`처럼 슬래시로 감싸면 정규식 키로 쓸 수 있어요.
- 설정에서 **로어 예산**을 150으로 줄이고 여러 키워드를 한꺼번에 말하면, 우선순위가 낮은 항목부터 빠지는 걸 볼 수 있어요.

### 5. 검수: 안전과 속도의 교환

- **출력 버퍼**를 '문장 단위'와 '바로 표시'로 바꿔 가며 `/mod-test-output`을 보내 보세요. 문장 단위는 문제 문장이 화면에 나가기 전에 멈추고, 바로 표시는 다 보여 준 뒤에 가려요. 대신 패널의 **첫 표시** 시간이 문장 단위 쪽이 늦어요.
- **검수 방식 → llm**은 규칙에 걸리지 않은 글을 LLM이 한 번 더 판정해요. 입력 검수 지연이 그만큼 늘어나는 걸 확인해 보세요. 판정 프롬프트는 `prompts/judge.txt`예요.
- 검수에 막힌 입력은 다음 턴 프롬프트에도 들어가지 않아요(막아 놓고 다음 턴에 슬쩍 들어가는 구멍을 막기 위해).
- 자해·자살 신호가 입력에서 보이면, 검수 모드가 off가 아닌 한 캐릭터 대화를 멈추고 상담 번호를 안내해요(`prompts/crisis.txt`).
- `moderation_rules.json`의 규칙은 일부러 단순해요. "배고파 죽겠다" 같은 과장 표현이 걸리는지(오탐), 돌려 말한 표현이 빠지는지(미탐) 검수 로그로 확인하며 다듬어 보세요. 파일을 저장하면 바로 반영돼요.

### 6. 토큰 추정 보정

예산 계산은 추정치로 해요(제공사마다 토크나이저가 달라서). 실제 모델로 몇 턴 보낸 뒤 패널의 **추정 대비 실측** 값을 보고 `.env`의 `TOKEN_HANGUL_RATIO`를 맞춰 보세요. 같은 한국어 문장도 모델마다 토큰 수가 꽤 달라요.

### 7. 로컬 모델의 첫 토큰 시간

Ollama 같은 로컬 서버는 사용량에 캐시 토큰을 보고하지 않는 경우가 많지만, 앞부분이 같으면 KV 캐시를 재사용해서 첫 토큰이 빨라져요.
anchored와 sliding을 번갈아 써 보며 **첫 토큰** 시간이 어떻게 달라지는지 비교해 보세요.

## 구조

```
charchat-lab/
├─ run.py                   실행 진입점
├─ app/
│  ├─ main.py               FastAPI 라우트
│  ├─ chat_service.py       한 턴의 흐름: 입력 검수 → 조립 → 스트리밍 → 사후 검수 → 저장 → 요약 예약
│  ├─ prompt.py             프롬프트 조립과 창 전략(full/sliding/anchored)
│  ├─ memory.py             요약 메모리(덩어리 요약, 백그라운드 실행)
│  ├─ lore.py               로어북 매칭(키, 보조 키, 정규식, 예산)
│  ├─ moderation.py         규칙/LLM 검수, 문장 단위 출력 버퍼
│  ├─ cards.py              캐릭터 카드 V2/V3 JSON·PNG 가져오기, 내보내기, 매크로
│  ├─ db.py                 SQLite. 메시지는 트리(parent_id)로 저장
│  ├─ tokens.py             토큰 추정
│  ├─ pricing.py            비용 계산(Claude 가격 내장, pricing.json으로 추가)
│  └─ providers/            mock, anthropic, gemini, openai_compat 어댑터
├─ prompts/                 프롬프트 템플릿(저장하면 다음 요청부터 반영)
├─ characters/              처음 실행할 때 넣는 샘플 카드
├─ moderation_rules.json    검수 규칙
├─ static/                  화면(빌드 없는 HTML/CSS/JS)
└─ tests/                   pytest
```

한 번의 요청은 이렇게 생겼어요. 잘 안 바뀌는 것을 앞에, 자주 바뀌는 것을 뒤에 둬요.

```
system    [고정] 규칙 + 캐릭터 카드 + 페르소나 + 상시 로어 + 예시 대화   ← 캐시 중단점 ①
          [요약] 지금까지의 줄거리                                       ← 캐시 중단점 ②
messages  (대화 시작 자리표시자) + 최근 대화                             ← 직전 대화 끝에 중단점 ③
          마지막 사용자 메시지 = 입력 + <system-memo>(로어, 진행 지시)    ← 중단점 ④
```

메시지는 트리로 저장돼요. 다시 생성이나 수정은 같은 부모 밑에 형제를 하나 더 만들고, 채팅의 `active_leaf_id`에서 루트까지 거슬러 올라간 경로가 지금 보이는 대화예요.

## 고쳐 보기 좋은 곳

- `prompts/main.txt`: 기본 규칙. 캐릭터 카드의 시스템 프롬프트에 `{{original}}`을 쓰면 이 내용이 그 자리에 들어가요.
- `prompts/post_history.txt`: 매 턴 마지막 메시지에 붙는 지시
- `prompts/final_user.txt`: 마지막 메시지의 모양(입력과 시스템 메모의 배치)
- `prompts/summary_*.txt`, `prompts/judge.txt`: 요약과 LLM 검수 프롬프트
- `moderation_rules.json`: 검수 규칙
- `pricing.example.json`을 `pricing.json`으로 복사해서 다른 모델 가격 추가

## API

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/api/config` | 제공사 상태, 설정, 설정 명세 |
| PUT | `/api/settings` | 런타임 설정 변경 |
| GET/POST | `/api/characters` | 캐릭터 목록, 만들기 |
| POST | `/api/characters/import` | 카드 파일(JSON, PNG) 가져오기 |
| GET | `/api/characters/{id}/export` | V2 카드 JSON 내보내기 |
| POST | `/api/chats` | 대화 만들기 |
| GET | `/api/chats/{id}` | 지금 경로의 메시지와 형제 정보, 요약 상태 |
| POST | `/api/chats/{id}/messages` | 보내기(SSE 스트림) |
| POST | `/api/chats/{id}/regenerate` | 다시 생성(SSE) |
| POST | `/api/chats/{id}/switch` | 다른 가지로 이동 |
| POST | `/api/messages/{id}/edit` | 수정(사용자 메시지는 SSE, 캐릭터 메시지는 JSON) |
| POST | `/api/chats/{id}/summarize` | 지금 요약 |
| GET | `/api/chats/{id}/stats` | 설정별 사용량 비교 |
| GET | `/api/messages/{id}/prompt` | 그 턴에 보낸 요청 원문 |

SSE 이벤트: `user`, `meta`(조립 결과), `delta`, `retract`(검수로 가림), `notice`(입력 차단, 위기 안내), `error`, `done`.

## 테스트

```bash
.venv/bin/pip install -r requirements-dev.txt     # Windows: .venv\Scripts\python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

API 키 없이 돌아가요. 제공사 어댑터는 실제 SDK(anthropic, google-genai, openai)를 가짜 HTTP 서버에 붙여서, 보내는 요청 형식과 스트림 해석을 검증해요.

## 주의할 점

- 서버는 기본으로 `127.0.0.1`에서만 열려요. API 키가 서버에 있으니 외부에 열지 마세요.
- mock의 캐시, 지연, 비용은 흉내 낸 값이에요. 비용표의 Claude 가격은 2026-10-12에 공식 문서에서 확인한 값이라 바뀔 수 있어요.
- 기본 설정은 전체이용가 기준(검수 block, Gemini 안전 필터 켬)이에요.

## 더 해 볼 만한 것

- 로어 재귀 스캔(주입된 로어의 내용이 다른 로어를 부르기)
- 벡터 검색 기억(sqlite-vec나 pgvector로 오래된 장면 찾아 넣기)
- 1시간 캐시 TTL(`{"type": "ephemeral", "ttl": "1h"}`)과 5분 TTL 비용 비교
- 요약을 '줄거리'와 '고정 사실'로 나눠 따로 관리하기
- 같은 대화를 여러 모델에 동시에 보내 답변과 비용을 나란히 보기

## 참고 문서

- Claude 프롬프트 캐시: <https://platform.claude.com/docs/en/build-with-claude/prompt-caching>
- Claude 사고(thinking)와 샘플링 파라미터: <https://platform.claude.com/docs/en/build-with-claude/thinking>
- Claude effort: <https://platform.claude.com/docs/en/build-with-claude/effort>
- Claude 모델과 가격: <https://platform.claude.com/docs/en/models/overview>
- Gemini 안전 설정: <https://ai.google.dev/gemini-api/docs/safety-settings>
- Gemini 사고: <https://ai.google.dev/gemini-api/docs/thinking>
- Gemini 캐시: <https://ai.google.dev/gemini-api/docs/caching>
- Gemini API 약관: <https://ai.google.dev/gemini-api/terms>
