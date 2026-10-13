"""프롬프트 조립기.

요청은 아래 순서로 만든다. 잘 안 바뀌는 것을 앞에, 자주 바뀌는 것을 뒤에 둬야
프롬프트 캐시(제공사가 앞부분이 같으면 재사용하는 기능)가 오래 살아남는다.

  system  [고정]  규칙 + 캐릭터 카드 + 페르소나 + 상시 로어 + 예시 대화   ← 거의 안 바뀜
          [요약]  지금까지의 줄거리                                       ← 가끔 바뀜
  messages        최근 대화(창)                                           ← 한 턴씩 늘어남
                  마지막 사용자 메시지 = 입력 + 시스템 메모(로어·지시)   ← 매 턴 바뀜

창 전략
  full      : 전체 대화를 다 보낸다. 앞부분이 늘 같아 캐시는 잘 되지만 비용이 계속 커진다.
  sliding   : 예산에 맞게 오래된 메시지부터 버린다. 매 턴 앞부분이 바뀌어 대화 쪽 캐시가 깨진다.
  anchored  : 마지막 요약 지점부터 다 보낸다. 예산에 가까워지면 오래된 부분을 덩어리로 요약해
              시작점을 한 번에 옮긴다. 시작점이 고정되는 동안은 캐시가 유지된다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import config
from .cards import apply_macros
from .lore import LoreSelection, select_lore
from .tokens import MESSAGE_OVERHEAD, estimate_message, estimate_tokens

DEFAULT_PERSONA = "지우"
PLACEHOLDER_START = "(이야기 시작)"
PLACEHOLDER_AFTER_SUMMARY = "(이전 줄거리에 이어서)"


def load_prompt(name: str) -> str:
    """prompts/ 폴더의 템플릿을 매번 다시 읽는다. 서버를 재시작하지 않아도 수정이 바로 반영된다."""
    return (config.PROMPTS_DIR / name).read_text(encoding="utf-8").strip()


def fill(template: str, values: dict[str, str]) -> str:
    """{name} 자리표시자를 한 번에 치환한다. 넣는 값 안의 중괄호는 다시 치환하지 않는다."""
    pattern = re.compile(r"\{(" + "|".join(map(re.escape, values)) + r")\}")
    return pattern.sub(lambda m: values[m.group(1)], template)


@dataclass
class Msg:
    role: str
    content: str
    id: int | None = None
    tokens: int = 0


@dataclass
class Assembled:
    fixed_system: str
    summary_block: str | None
    history: list[Msg]
    final_user: str
    user_input: str
    lore: LoreSelection
    summary: dict | None
    since: list[Msg]
    est: dict
    window: dict
    warnings: list[str] = field(default_factory=list)

    def request_history(self) -> list[dict]:
        return [{"role": m.role, "content": m.content} for m in self.history]

    def diagnostics(self) -> dict:
        return {
            "est": self.est,
            "window": self.window,
            "lore": self.lore.summary(),
            "summary": (
                {
                    "id": self.summary["id"],
                    "covers_until_msg_id": self.summary["covers_until_msg_id"],
                    "tokens": self.est["summary"],
                }
                if self.summary
                else None
            ),
            "warnings": self.warnings,
        }


def persona_name(chat: dict) -> str:
    return (chat.get("persona_name") or "").strip() or DEFAULT_PERSONA


def format_examples(mes_example: str) -> str:
    blocks = [b.strip() for b in re.split(r"<START>", mes_example or "", flags=re.I) if b.strip()]
    return "\n\n".join(f"### 예시 {i + 1}\n{block}" for i, block in enumerate(blocks))


def build_fixed(character: dict, user: str, persona_desc: str, constant: list[dict]) -> str:
    main = load_prompt("main.txt")
    custom = (character.get("system_prompt") or "").strip()
    sections = [custom.replace("{{original}}", main) if custom else main]

    def add(title: str, body: str | None) -> None:
        if body and body.strip():
            sections.append(f"## {title}\n{body.strip()}")

    add("{{char}} 설정", character.get("description"))
    add("성격", character.get("personality"))
    add("현재 상황", character.get("scenario"))
    add("{{user}}에 대하여", f"이름: {user}\n{persona_desc}".strip())
    if constant:
        add("늘 적용되는 설정", "\n\n".join(f"[{e['name']}]\n{e['content'].strip()}" for e in constant))
    examples = format_examples(character.get("mes_example", ""))
    if examples:
        add("대화 예시", "(말투를 보여 주기 위한 예시이며 실제로 있었던 일이 아니다.)\n\n" + examples)
    return apply_macros("\n\n".join(sections), character["name"], user)


def build_final_user(character: dict, user: str, user_input: str, selection: LoreSelection) -> str:
    char = character["name"]
    lore_text = ""
    if selection.hits:
        lines = "\n".join(f"- {h.entry['name']}: {h.entry['content'].strip()}" for h in selection.hits)
        lore_text = f"[참고 설정]\n{lines}\n\n"
    default_post = load_prompt("post_history.txt")
    custom_post = (character.get("post_history_instructions") or "").strip()
    instructions = custom_post.replace("{{original}}", default_post) if custom_post else default_post
    template = apply_macros(load_prompt("final_user.txt"), char, user)
    return fill(template, {
        "lore": apply_macros(lore_text, char, user),
        "instructions": apply_macros(f"[진행 지시]\n{instructions}", char, user),
        "input": user_input,
    }).strip()


def merge_consecutive(messages: list[Msg]) -> list[Msg]:
    """같은 역할이 연달아 오면 합친다. 로컬 모델의 채팅 템플릿 중에는 역할 교대를 강제하는 것이 많다."""
    merged: list[Msg] = []
    for m in messages:
        if merged and merged[-1].role == m.role:
            prev = merged[-1]
            content = f"{prev.content}\n\n{m.content}"
            merged[-1] = Msg(m.role, content, m.id, estimate_message(content))
        else:
            merged.append(m)
    return merged


def assemble(character: dict, chat: dict, prior: list[dict], user_input: str, settings: dict,
             summary: dict | None) -> Assembled:
    """prior: 이번 사용자 메시지 직전까지의 경로(루트부터)."""
    user = persona_name(chat)
    strategy = settings["window_strategy"]
    warnings: list[str] = []

    eligible = [
        Msg(m["role"], m["content"], m["id"], estimate_message(m["content"]))
        for m in prior
        if m["role"] in ("user", "assistant") and not m.get("excluded")
    ]

    # 요약 지점 이후만 '창 후보'가 된다(anchored일 때만 요약을 쓴다)
    since = eligible
    if strategy != "anchored":
        summary = None
    if summary is not None:
        ids = [m.id for m in eligible]
        if summary["covers_until_msg_id"] in ids:
            since = eligible[ids.index(summary["covers_until_msg_id"]) + 1:]
        else:
            summary = None

    # 로어북: 최근 메시지 N개 + 이번 입력에서 키를 찾는다
    book = character.get("book_meta") or {}
    scan_depth = int(book.get("scan_depth") or settings["lore_scan_depth"])
    lore_budget = int(book.get("token_budget") or settings["lore_token_budget"])
    scan_text = "\n".join([m.content for m in eligible[-scan_depth:]] + [user_input])
    selection = select_lore(character.get("lore", []), scan_text, lore_budget)

    fixed = build_fixed(character, user, chat.get("persona_desc", ""), selection.constant)
    summary_block = f"## 지금까지의 줄거리\n{summary['content'].strip()}" if summary else None
    final_user = build_final_user(character, user, user_input, selection)

    fixed_t = estimate_tokens(fixed)
    summary_t = estimate_tokens(summary_block) if summary_block else 0
    final_t = estimate_message(final_user)
    history_budget = settings["input_token_budget"] - fixed_t - summary_t - final_t - MESSAGE_OVERHEAD * 2
    if history_budget <= 0:
        warnings.append("고정 프롬프트와 입력만으로 입력 예산을 넘었습니다. 예산을 늘리거나 카드를 줄이세요.")

    since_tokens = sum(m.tokens for m in since)
    if strategy == "full" or since_tokens <= history_budget:
        window = list(since)
    else:
        window, used = [], 0
        for m in reversed(since):
            if window and used + m.tokens > history_budget:
                break
            window.insert(0, m)
            used += m.tokens
        if used > history_budget:
            warnings.append("마지막 메시지 하나만으로도 대화 예산을 넘었습니다.")
    dropped = since[: len(since) - len(window)]

    window = merge_consecutive(window)
    # 직전 답변이 가려졌거나 실패해 사용자 메시지로 끝나면 이번 입력과 합친다
    if window and window[-1].role == "user":
        tail = window.pop()
        final_user = f"{tail.content}\n\n{final_user}"
        final_t = estimate_message(final_user)
    placeholder = False
    if window and window[0].role == "assistant":
        text = PLACEHOLDER_AFTER_SUMMARY if summary else PLACEHOLDER_START
        window.insert(0, Msg("user", text, None, estimate_message(text)))
        placeholder = True

    history_t = sum(m.tokens for m in window)
    est = {
        "fixed": fixed_t,
        "summary": summary_t,
        "history": history_t,
        "final": final_t,
        "total": fixed_t + summary_t + history_t + final_t,
    }
    window_info = {
        "strategy": strategy,
        "history_budget": history_budget,
        "since_count": len(since),
        "since_tokens": since_tokens,
        "included": len([m for m in window if m.id is not None]),
        "dropped": len(dropped),
        "dropped_tokens": sum(m.tokens for m in dropped),
        "overflow": strategy == "anchored" and bool(dropped),
        "placeholder": placeholder,
        "first_id": next((m.id for m in window if m.id is not None), None),
    }
    return Assembled(
        fixed_system=fixed,
        summary_block=summary_block,
        history=window,
        final_user=final_user,
        user_input=user_input,
        lore=selection,
        summary=summary,
        since=since,
        est=est,
        window=window_info,
        warnings=warnings,
    )
