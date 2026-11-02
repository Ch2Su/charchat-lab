"""비용 계산의 가격표(app/pricing.py)를 확인한다."""
import json

from app import config, pricing

USAGE = {"input_total": 1000, "output": 100, "cache_read": 0, "cache_write": 0}


def use_override(monkeypatch, tmp_path, data):
    path = tmp_path / "pricing.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(config, "PRICING_OVERRIDE_PATH", path)


def test_override_with_numbers_is_used(monkeypatch, tmp_path):
    use_override(monkeypatch, tmp_path, {"gemini:fake": {"input": 1.0, "output": 2.0}, "openai_compat:*": {"input": 0, "output": 0}})
    assert pricing.cost(USAGE, "gemini", "fake")["usd"] == (1000 * 1.0 + 100 * 2.0) / 1_000_000
    assert pricing.cost(USAGE, "openai_compat", "qwen3.5:4b")["usd"] == 0  # 로컬 모델은 0으로 둘 수 있다


def test_null_or_missing_price_means_no_price(monkeypatch, tmp_path):
    # 예시 파일의 null 자리표시자가 0으로 읽혀 무료처럼 보이면 안 된다
    use_override(monkeypatch, tmp_path, {"gemini:fake": {"input": None, "output": None}, "gemini:half": {"input": 1.0}})
    assert pricing.cost(USAGE, "gemini", "fake") is None
    assert pricing.cost(USAGE, "gemini", "half") is None  # 키가 모자라도 KeyError로 죽지 않는다


def test_example_file_has_no_free_placeholders(monkeypatch):
    monkeypatch.setattr(config, "PRICING_OVERRIDE_PATH", config.ROOT / "pricing.example.json")
    assert pricing.cost(USAGE, "gemini", "gemini-3.8-flash") is None
    assert pricing.cost(USAGE, "openai_compat", "아무 로컬 모델")["usd"] == 0
