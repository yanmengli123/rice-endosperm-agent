"""抽取配置指纹（D3）：让「改配置 → 重抽取」的改进回路机械化。

历史问题：``extraction_result`` 缓存只看存在性，metadata 里虽然记录了
prompt/lexicon/model 版本，但没有任何代码比对——改词典、扩谓词、换模型后
重跑 build 一次 LLM 都不会重调，除非手工 reset 全库或逐 chunk 重抽。

指纹把「会影响抽取语义」的全部组件收敛成一个内容哈希：

- 算法身份：prompt / 谓词闭集 / 词典 / 抽取单元 / 门禁 / 触发词 / 指纹版本；
- 运行配置：model_spec、model_params、context_sentences、batch_size、
  strict_triggers、verifier_model_spec；
- 文档词典（doclex）：文档级 overlay 的指纹（无 overlay 时为 None）。

指纹中任何一项变化 → 缓存视为 stale → 重抽并覆盖。纯展示类配置
（并发数、进度文案）不进指纹，不触发无谓重抽。
"""

from __future__ import annotations

import hashlib
from typing import Any

# 指纹算法自身版本：指纹构造方式变化（新增/删除组成项）时 bump
EXTRACTION_FINGERPRINT_VERSION = "extraction_fingerprint_v1"


def relation_types_version(relation_types: frozenset[str] | set[str]) -> str:
    """谓词闭集的内容版本：集合成员变化即变（与字典序无关）。"""
    return hashlib.sha256("|".join(sorted(relation_types)).encode("utf-8")).hexdigest()[:16]


def compute_extraction_fingerprint(
    *,
    extractor_type: str,
    algorithm_versions: dict[str, Any],
    runtime_options: dict[str, Any],
    doclex_fingerprint: str | None = None,
) -> str:
    """把算法身份 + 运行配置 + 文档词典折叠成稳定指纹。

    ``algorithm_versions`` / ``runtime_options`` 只接受可 JSON 序列化的扁平
    字符串/数字/布尔值；调用方负责挑拣（见 LLMScientificGraphExtractor）。
    """
    payload = {
        "version": EXTRACTION_FINGERPRINT_VERSION,
        "extractor_type": extractor_type,
        "algorithm": {str(key): algorithm_versions[key] for key in sorted(algorithm_versions)},
        "runtime": {str(key): runtime_options[key] for key in sorted(runtime_options)},
        "doclex": doclex_fingerprint,
    }
    canonical = "\n".join(
        f"{section}:{json_line}"
        for section, json_line in (
            ("v", payload["version"]),
            ("e", payload["extractor_type"]),
            ("a", repr(payload["algorithm"])),
            ("r", repr(payload["runtime"])),
            ("d", payload["doclex"]),
        )
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]
