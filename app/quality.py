"""답변 품질 어림 지표.

README 실험 8에서 본 작은 모델의 무너짐(한자·영어가 섞임, 인물의 말투가 흔들림, 이상한 답을 따라 해서
점점 반복함)을 숫자로 만들어 모델·설정별로 비교할 수 있게 한다. 정답 판정이 아니라 글자 모양과
어미만 보는 어림 지표다. 외부 라이브러리 없이 정규식과 집합 연산만 쓰고, 모든 함수는 순수 함수다.

- 문자 체계: 글자(str.isalpha) 중 한자·가나·그 밖의 외국 문자 비율과 로마자 비율.
  로마자는 이름이나 약어로 정상적으로 쓰일 수 있어서 따로 본다.
- 말투: 대사(큰따옴표 안)의 문장 끝 어절 어미로 존댓말과 반말을 센다. 묘사(*별표*)는 "~했다"체라 뺀다.
- 반복: 직전 답변과 겹치는 글자 5-gram 비율, 같은 답 안에서 되풀이된 문장 비율.
  한국어는 어절에 조사·어미가 붙어 모양이 바뀌므로 단어 n-gram보다 글자 n-gram이 낫다.
"""
from __future__ import annotations

import re
from collections import Counter

FOREIGN_WARN = 0.01
LATIN_WARN = 0.05
REPEAT_PREV_WARN = 0.5
REPEAT_SELF_WARN = 0.3

_QUOTE = re.compile(r'"([^"]*)"|“([^”]*)”')
_ACTION = re.compile(r"\*[^*]*\*")
_SENTENCE_END = re.compile(r"[.!?…~]+|\n")
_SYLLABLES = re.compile(r"[가-힣]+")
_PUNCT = re.compile(r"[^\w\s]")
_SPACE = re.compile(r"\s+")
# 존댓말을 먼저 본다. 그래야 "합니다"의 "니다"를 반말 "다"로 잘못 세지 않는다.
# 음절 단위로 비교하므로 모음이 줄어든 반말 어미(와=오+아, 해, 봐, 줘, 돼, 워)는 따로 적는다
_POLITE = re.compile(r"(요|니다|니까|세요|죠|십시오)$")
_PLAIN = re.compile(r"(다|야|어|아|지|냐|니|자|래|게|네|군|걸|거든|잖아|해|와|봐|줘|돼|워)$")
# 한 글자짜리 대답은 어미 규칙과 반대로 읽힌다("네?"는 존댓말인데 반말 어미 "네"에 걸린다)
_ANSWER_WORDS = {"네": "polite", "예": "polite", "응": "plain"}


def script_counts(text: str) -> dict:
    """글자 수를 문자 체계별로 센다. other는 위에 없는 글자(키릴, 태국 문자 등)."""
    counts = {"hangul": 0, "han": 0, "kana": 0, "latin": 0, "other": 0}
    for ch in text:
        if "가" <= ch <= "힣" or "ㄱ" <= ch <= "ㆎ":
            counts["hangul"] += 1
        elif "一" <= ch <= "鿿" or "㐀" <= ch <= "䶿":
            counts["han"] += 1
        elif "぀" <= ch <= "ヿ":
            counts["kana"] += 1
        elif "A" <= ch <= "Z" or "a" <= ch <= "z":
            counts["latin"] += 1
        elif ch.isalpha():
            counts["other"] += 1
    return counts


def _normalize(text: str) -> str:
    """문장부호를 지우고 공백을 하나로 줄인다."""
    return _SPACE.sub(" ", _PUNCT.sub("", text)).strip()


def _sentences(text: str) -> list[str]:
    return [s for s in (_normalize(part) for part in _SENTENCE_END.split(text)) if s]


def _endings(text: str) -> list[str]:
    """문장마다 마지막 한글 어절의 끝(문장부호·기호를 뗀 음절들)."""
    quotes = [a or b for a, b in _QUOTE.findall(text)]
    source = "\n".join(quotes) if quotes else _ACTION.sub(" ", text)
    endings = []
    for sentence in _SENTENCE_END.split(source):
        words = [w for w in sentence.split() if _SYLLABLES.search(w)]
        if words:
            endings.append(_SYLLABLES.findall(words[-1])[-1])
    return endings


def speech_level(text: str) -> dict:
    polite = plain = 0
    for word in _endings(text):
        kind = _ANSWER_WORDS.get(word)
        if kind == "polite" or (kind is None and _POLITE.search(word)):
            polite += 1
        elif kind == "plain" or (kind is None and _PLAIN.search(word)):
            plain += 1
    if polite == plain == 0:
        level = None
    elif polite >= 1 and polite >= 2 * plain:
        level = "polite"
    elif plain >= 1 and plain >= 2 * polite:
        level = "plain"
    else:
        level = "mixed"
    return {"polite": polite, "plain": plain, "level": level}


def _majority(levels: list[str | None]) -> str | None:
    counts = Counter(level for level in levels if level)
    if not counts:
        return None
    (top, n), *rest = counts.most_common()
    return None if rest and rest[0][1] == n else top


def _grams(text: str, n: int = 5) -> set[str]:
    s = _normalize(text)
    return {s[i:i + n] for i in range(len(s) - n + 1)}


def repeat_prev_ratio(text: str, previous: str | None) -> float:
    """이번 답의 글자 5-gram 중 직전 답에도 있는 비율."""
    grams = _grams(text)
    if not grams or not previous:
        return 0.0
    return len(grams & _grams(previous)) / len(grams)


def repeat_self_ratio(text: str) -> float:
    """8자 이상 문장 중 같은 답 안에서 똑같이 되풀이된 비율(1 - 고유 수 / 전체 수)."""
    sentences = [s for s in _sentences(text) if len(s) >= 8]
    if not sentences:
        return 0.0
    return 1 - len(set(sentences)) / len(sentences)


def measure(text: str, previous: list[str]) -> dict:
    """previous: 같은 경로의 직전 assistant 답변들(오래된 것부터, 최대 5개)."""
    previous = previous[-5:]
    scripts = script_counts(text)
    letters = max(1, sum(scripts.values()))
    foreign = round((scripts["han"] + scripts["kana"] + scripts["other"]) / letters, 4)
    latin = round(scripts["latin"] / letters, 4)
    speech = speech_level(text)
    speech_prev = _majority([speech_level(p)["level"] for p in previous])
    speech_shift = {speech["level"], speech_prev} == {"polite", "plain"}
    repeat_prev = round(repeat_prev_ratio(text, previous[-1] if previous else None), 4)
    repeat_self = round(repeat_self_ratio(text), 4)
    flags = []
    if foreign >= FOREIGN_WARN:
        flags.append("foreign")
    if latin >= LATIN_WARN:
        flags.append("latin")
    if speech_shift:
        flags.append("speech_shift")
    if repeat_prev >= REPEAT_PREV_WARN:
        flags.append("repeat_prev")
    if repeat_self >= REPEAT_SELF_WARN:
        flags.append("repeat_self")
    return {
        "chars": len(text),
        "sentences": len(_sentences(text)),
        "scripts": scripts,
        "foreign_ratio": foreign,
        "latin_ratio": latin,
        "speech": speech,
        "speech_prev": speech_prev,
        "speech_shift": speech_shift,
        "repeat_prev": repeat_prev,
        "repeat_self": repeat_self,
        "flags": flags,
    }
