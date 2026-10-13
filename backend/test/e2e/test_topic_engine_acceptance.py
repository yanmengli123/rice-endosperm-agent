"""Actual BERTopic CPU execution with synthetic, offline embeddings in Docker."""

import json
import os
import subprocess
from pathlib import Path

import numpy as np
import pytest

from yuxi.literature.topic_contract import prepare_document, validate_result

pytestmark = pytest.mark.e2e


@pytest.mark.parametrize("chinese_comparison", [False, True])
def test_locked_cpu_runtime_fits_offline_embeddings_without_network(tmp_path, chinese_comparison):
    python = os.environ.get("YUXI_TOPIC_PYTHON", "/opt/topic-runtime/.venv/bin/python")
    if not Path(python).is_file():
        pytest.skip("专用 BERTopic 可选运行时未安装；请在 topic-worker 容器运行")
    rng = np.random.default_rng(42)
    centres = rng.normal(size=(3, 1024))
    centres /= np.linalg.norm(centres, axis=1, keepdims=True)
    labels = ["starch amylose flour grain", "heat stress transcription gene", "nitrogen fertilizer soil agronomy"]
    if chinese_comparison:
        labels[:2] = ["水稻胚乳淀粉合成直链淀粉贮藏蛋白", "水稻高温胁迫转录因子基因表达信号转导"]
    docs = [
        prepare_document(
            f"r{i}",
            f"r{i}",
            {
                "title": f"Rice {labels[i // 20]} {i}",
                "abstract": labels[i // 20],
                "year": 2020 + i % 3,
                "source": "fixture",
            },
        )
        for i in range(60)
    ]
    vectors = np.stack([centres[i // 20] + rng.normal(scale=0.002, size=1024) for i in range(60)]).astype(np.float32)
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    np.save(tmp_path / "joint_bge_m3.npy", vectors, allow_pickle=False)
    if chinese_comparison:
        # Independent vector space; a coordinate permutation preserves its test clusters.
        np.save(tmp_path / "chinese_bge_zh.npy", np.roll(vectors[:40], 50, axis=1), allow_pickle=False)
    (tmp_path / "input.json").write_text(
        json.dumps(
            {
                "documents": docs,
                "options": {
                    "cluster_sizes": [5, 10],
                    "analysis_year": 2026,
                    "stability": True,
                    "chinese_comparison": chinese_comparison,
                },
            }
        ),
        encoding="utf-8",
    )
    engine = Path(__file__).resolve().parents[2] / "package/yuxi/literature/topic_engine.py"
    completed = subprocess.run(
        [python, str(engine), str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=300,
        env={
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "OMP_NUM_THREADS": "2",
            "NUMBA_NUM_THREADS": "2",
            "USE_TORCH": "0",
            "USE_TF": "0",
            "HF_HUB_OFFLINE": "1",
        },
    )
    assert completed.returncode == 0, completed.stderr[-3000:]
    result = validate_result(json.loads((tmp_path / "result.json").read_text()), docs)
    model = result["models"]["joint_bge_m3"]
    assert len(model["assignments"]) == 60
    assert sum(t["count"] for t in model["topics"]) + model["outliers"] == 60
    assert len(model["topics"]) >= 2
    assert len(model["candidates"]) == 2 and model["stability"]["seeds"] == [42, 7]
    assert model["dynamic"] and model["hierarchy"] and model["similarity"]
    if chinese_comparison:
        assert set(result["models"]) == {"joint_bge_m3", "chinese_bge_zh", "chinese_bge_m3_comparison"}
        chinese_keys = {d["document_key"] for d in docs if d["language_of_text"] == "zh"}
        assert len(chinese_keys) == 40
        for name in ("chinese_bge_zh", "chinese_bge_m3_comparison"):
            value = result["models"][name]
            assert {a["document_key"] for a in value["assignments"]} == chinese_keys
            assert value["selected"]["min_cluster_size"] == 15
            assert value["topics"] and all(t["word_frequencies"] for t in value["topics"])
