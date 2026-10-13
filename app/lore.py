"""로어북(월드 인포): 최근 대화에 키워드가 나오면 해당 설정을 프롬프트에 끼워 넣는다.

- constant 항목은 항상 들어가며, 고정 프롬프트(캐시되는 앞부분)에 둔다.
- 나머지는 최근 메시지 N개 + 이번 입력에서 키를 찾는다. 트리거된 항목은 매 턴
  바뀌므로 프롬프트 맨 뒤(마지막 사용자 메시지)에 둬서 앞부분 캐시를 깨지 않는다.
- selective 항목은 keys 중 하나와 secondary_keys 중 하나가 함께 나와야 한다.
- 토큰 예산을 넘으면 priority가 낮은 항목부터 뺀다(V2 카드 규칙).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .tokens import estimate_tokens


@dataclass
class LoreHit:
    entry: dict
    key: str
    tokens: int


@dataclass
class LoreSelection:
    constant: list[dict] = field(default_factory=list)
    hits: list[LoreHit] = field(default_factory=list)
    dropped: list[LoreHit] = field(default_factory=list)

    def summary(self) -> dict:
        return {
            "constant": [e["name"] for e in self.constant],
            "hits": [{"name": h.entry["name"], "key": h.key, "tokens": h.tokens} for h in self.hits],
            "dropped": [{"name": h.entry["name"], "key": h.key, "tokens": h.tokens} for h in self.dropped],
            "tokens": sum(h.tokens for h in self.hits),
        }


def key_matches(key: str, text: str, case_sensitive: bool) -> bool:
    key = key.strip()
    if not key:
        return False
    # /패턴/플래그 형태면 정규식으로 본다
    if len(key) > 2 and key.startswith("/") and key.rfind("/") > 0:
        last = key.rfind("/")
        pattern, flags = key[1:last], key[last + 1:]
        if not flags or set(flags) <= set("imsux"):
            re_flags = re.I if ("i" in flags or not case_sensitive) else 0
            try:
                return re.search(pattern, text, re_flags) is not None
            except re.error:
                return False
    if case_sensitive:
        return key in text
    return key.lower() in text.lower()


def select_lore(entries: list[dict], scan_text: str, token_budget: int) -> LoreSelection:
    selection = LoreSelection()
    for entry in entries:
        if not entry.get("enabled", True):
            continue
        if entry.get("constant"):
            selection.constant.append(entry)
            continue
        case_sensitive = bool(entry.get("case_sensitive"))
        hit_key = next((k for k in entry.get("keys", []) if key_matches(k, scan_text, case_sensitive)), None)
        if hit_key is None:
            continue
        if entry.get("selective") and entry.get("secondary_keys"):
            if not any(key_matches(k, scan_text, case_sensitive) for k in entry["secondary_keys"]):
                continue
        selection.hits.append(LoreHit(entry, hit_key, estimate_tokens(entry.get("content", ""))))

    # 예산 적용: 우선순위가 높은 항목부터 채운다
    kept, used = [], 0
    for hit in sorted(selection.hits, key=lambda h: (-h.entry.get("priority", 10), h.entry.get("insertion_order", 100))):
        if used + hit.tokens <= token_budget:
            kept.append(hit)
            used += hit.tokens
        else:
            selection.dropped.append(hit)
    selection.hits = sorted(kept, key=lambda h: (h.entry.get("insertion_order", 100), h.entry.get("id", 0)))
    selection.constant.sort(key=lambda e: (e.get("insertion_order", 100), e.get("id", 0)))
    return selection
