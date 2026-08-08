from __future__ import annotations

import pytest

from yuxi.knowledge.parser.base import DocumentParserException
from yuxi.knowledge.parser.deepseek_ocr import DeepSeekOCRParser
from yuxi.models.providers.cache import ModelInfo


def _siliconflow_chat_info(api_key: str = "page-key") -> ModelInfo:
    return ModelInfo(
        provider_id="siliconflow-cn",
        model_id="deepseek-ai/DeepSeek-V4-Flash",
        model_type="chat",
        display_name="DeepSeek V4 Flash",
        api_key=api_key,
        base_url="https://api.siliconflow.cn/v1",
        provider_type="openai",
    )


def test_deepseek_ocr_uses_model_provider_credential(monkeypatch):
    monkeypatch.setenv("SILICONFLOW_API_KEY", "legacy-env-key")
    monkeypatch.setattr(
        "yuxi.knowledge.parser.deepseek_ocr.model_cache.get_all_specs",
        lambda model_type=None: [_siliconflow_chat_info()],
    )

    parser = DeepSeekOCRParser()

    assert parser.api_key == "page-key"
    assert parser.api_url == "https://api.siliconflow.cn/v1/chat/completions"


def test_deepseek_ocr_does_not_fallback_to_environment(monkeypatch):
    monkeypatch.setenv("SILICONFLOW_API_KEY", "legacy-env-key")
    monkeypatch.setattr(
        "yuxi.knowledge.parser.deepseek_ocr.model_cache.get_all_specs",
        lambda model_type=None: [],
    )

    with pytest.raises(DocumentParserException, match="模型供应商"):
        DeepSeekOCRParser()
