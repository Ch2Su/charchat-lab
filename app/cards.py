"""캐릭터 카드(Character Card V2/V3) 가져오기·내보내기와 매크로 치환.

카드 JSON은 {"spec": "chara_card_v2", "data": {...}} 형태가 사실상 표준이다.
PNG 카드는 tEXt 청크에 'chara'(V2) 또는 'ccv3'(V3) 키로 base64 JSON이 들어 있다.
"""
from __future__ import annotations

import base64
import json
import re
import struct
import zlib

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class CardError(ValueError):
    pass


# ── 매크로 ────────────────────────────────────────────
_MACROS = [
    (re.compile(r"\{\{\s*char\s*\}\}", re.I), "char"),
    (re.compile(r"\{\{\s*user\s*\}\}", re.I), "user"),
    (re.compile(r"<BOT>"), "char"),
    (re.compile(r"<USER>"), "user"),
]


def apply_macros(text: str, char: str, user: str) -> str:
    if not text:
        return ""
    names = {"char": char, "user": user}
    for pattern, key in _MACROS:
        text = pattern.sub(lambda _m, v=names[key]: v, text)
    return text


# ── 가져오기 ──────────────────────────────────────────
def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return [value]


def _as_dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _normalize_entry(raw: dict, index: int) -> dict:
    ext = _as_dict(raw.get("extensions"))
    return {
        "name": str(raw.get("name") or raw.get("comment") or f"항목 {index + 1}"),
        "keys": [str(k) for k in _as_list(raw.get("keys") or raw.get("key"))],
        "secondary_keys": [str(k) for k in _as_list(raw.get("secondary_keys") or raw.get("keysecondary"))],
        "content": str(raw.get("content") or ""),
        "enabled": bool(raw.get("enabled", not raw.get("disable", False))),
        "constant": bool(raw.get("constant", False)),
        "selective": bool(raw.get("selective", False)),
        "case_sensitive": bool(raw.get("case_sensitive") or ext.get("case_sensitive") or False),
        "insertion_order": int(raw.get("insertion_order", raw.get("order", 100)) or 0),
        "priority": int(raw.get("priority", 10) or 0),
    }


def parse_card_json(obj: dict) -> tuple[dict, list[dict]]:
    """카드 JSON → (캐릭터 필드, 로어 항목 목록)."""
    if not isinstance(obj, dict):
        raise CardError("카드 JSON의 최상위는 객체여야 합니다")
    spec = obj.get("spec")
    data = obj.get("data") if spec in ("chara_card_v2", "chara_card_v3") else obj
    if not isinstance(data, dict):
        raise CardError("카드의 data 필드를 읽을 수 없습니다")
    name = str(data.get("name") or "").strip()
    if not name:
        raise CardError("카드에 name이 없습니다")
    try:
        return _card_fields(name, data)
    except (AttributeError, TypeError, ValueError) as exc:
        # 필드 모양이 표준과 다른 카드(숫자 자리에 글, 목록 자리에 숫자 등). 서버 오류 대신 읽지 못했다고 알린다
        raise CardError(f"카드 형식이 표준과 달라 읽지 못했습니다: {exc}") from exc


def _card_fields(name: str, data: dict) -> tuple[dict, list[dict]]:
    greetings = data.get("alternate_greetings") or []
    if isinstance(greetings, str):  # 글 하나만 적은 카드. 그대로 돌면 글자 하나하나가 인사말이 된다
        greetings = [greetings]
    character = {
        "name": name,
        "description": str(data.get("description") or ""),
        "personality": str(data.get("personality") or ""),
        "scenario": str(data.get("scenario") or ""),
        "first_mes": str(data.get("first_mes") or ""),
        "mes_example": str(data.get("mes_example") or ""),
        "system_prompt": str(data.get("system_prompt") or ""),
        "post_history_instructions": str(data.get("post_history_instructions") or ""),
        "creator_notes": str(data.get("creator_notes") or ""),
        "alternate_greetings": [str(g) for g in greetings if str(g).strip()],
        "tags": [str(t) for t in _as_list(data.get("tags"))],
        "extensions": _as_dict(data.get("extensions")),
    }
    book = _as_dict(data.get("character_book"))
    entries = book.get("entries") or []
    if isinstance(entries, dict):  # 일부 도구는 entries를 {id: entry} 형태로 저장한다
        entries = list(entries.values())
    lore = [_normalize_entry(e, i) for i, e in enumerate(entries) if isinstance(e, dict)]
    character["book_meta"] = {k: book[k] for k in ("name", "description") if book.get(k) is not None}
    for key in ("scan_depth", "token_budget"):  # 조립기가 매 턴 숫자로 쓰는 값이라 여기서 숫자로 맞춘다
        if book.get(key) is not None:
            character["book_meta"][key] = int(book[key])
    return character, lore


def _png_text_chunks(blob: bytes) -> dict[str, str]:
    if not blob.startswith(PNG_SIGNATURE):
        raise CardError("PNG 파일이 아닙니다")
    chunks: dict[str, str] = {}
    pos = len(PNG_SIGNATURE)
    while pos + 8 <= len(blob):
        (length,) = struct.unpack(">I", blob[pos:pos + 4])
        ctype = blob[pos + 4:pos + 8]
        data = blob[pos + 8:pos + 8 + length]
        pos += 12 + length
        try:
            if ctype == b"tEXt":
                key, _, text = data.partition(b"\x00")
                chunks[key.decode("latin-1")] = text.decode("latin-1")
            elif ctype == b"zTXt":
                key, _, rest = data.partition(b"\x00")
                chunks[key.decode("latin-1")] = zlib.decompress(rest[1:]).decode("latin-1")
            elif ctype == b"iTXt":
                key, _, rest = data.partition(b"\x00")
                compressed, rest = rest[0], rest[2:]
                _lang, _, rest = rest.partition(b"\x00")
                _translated, _, text = rest.partition(b"\x00")
                chunks[key.decode("latin-1")] = (zlib.decompress(text) if compressed else text).decode("utf-8")
        except (zlib.error, UnicodeDecodeError, IndexError):
            continue
        if ctype == b"IEND":
            break
    return chunks


def parse_card_bytes(blob: bytes, filename: str = "") -> tuple[dict, list[dict]]:
    if blob.startswith(PNG_SIGNATURE):
        chunks = _png_text_chunks(blob)
        encoded = chunks.get("ccv3") or chunks.get("chara")
        if not encoded:
            raise CardError("PNG 안에서 캐릭터 카드 데이터(chara/ccv3)를 찾지 못했습니다")
        try:
            obj = json.loads(base64.b64decode(encoded).decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise CardError(f"PNG 카드 데이터를 해석하지 못했습니다: {exc}") from exc
        return parse_card_json(obj)
    try:
        obj = json.loads(blob.decode("utf-8-sig"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise CardError(f"JSON을 읽지 못했습니다: {exc}") from exc
    return parse_card_json(obj)


# ── 내보내기 ──────────────────────────────────────────
def export_card(character: dict) -> dict:
    book_meta = character.get("book_meta") or {}
    entries = []
    for i, entry in enumerate(character.get("lore", [])):
        entries.append({
            "id": i + 1,
            "name": entry["name"],
            "keys": entry["keys"],
            "secondary_keys": entry["secondary_keys"],
            "content": entry["content"],
            "enabled": entry["enabled"],
            "constant": entry["constant"],
            "selective": entry["selective"],
            "case_sensitive": entry["case_sensitive"],
            "insertion_order": entry["insertion_order"],
            "priority": entry["priority"],
            "extensions": {},
        })
    data = {
        "name": character["name"],
        "description": character["description"],
        "personality": character["personality"],
        "scenario": character["scenario"],
        "first_mes": character["first_mes"],
        "mes_example": character["mes_example"],
        "creator_notes": character["creator_notes"],
        "system_prompt": character["system_prompt"],
        "post_history_instructions": character["post_history_instructions"],
        "alternate_greetings": character.get("alternate_greetings", []),
        "tags": character.get("tags", []),
        "creator": "",
        "character_version": "",
        "extensions": character.get("extensions") or {},
    }
    if entries or book_meta:
        data["character_book"] = {**book_meta, "extensions": {}, "entries": entries}
    return {"spec": "chara_card_v2", "spec_version": "2.0", "data": data}


def png_with_card(card: dict, width: int = 1, height: int = 1) -> bytes:
    """카드 데이터를 담은 최소 PNG를 만든다(테스트·내보내기용)."""
    def chunk(ctype: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + ctype + data + struct.pack(">I", zlib.crc32(ctype + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\x80\x80\x80" * width for _ in range(height))
    payload = base64.b64encode(json.dumps(card, ensure_ascii=False).encode("utf-8"))
    return (
        PNG_SIGNATURE
        + chunk(b"IHDR", ihdr)
        + chunk(b"tEXt", b"chara\x00" + payload)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )
