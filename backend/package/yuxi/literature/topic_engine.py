"""Isolated CPU entry point. Only JSON/NPY in, structured analytical JSON out.

Invoked by file path in the separately locked runtime: do not import Yuxi,
read credentials, fetch models or deserialize pickle here.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
from collections import Counter, defaultdict
from pathlib import Path

os.environ.setdefault("USE_TORCH", "0")
os.environ.setdefault("USE_TF", "0")
os.environ.setdefault("NUMBA_NUM_THREADS", "2")

import numpy as np
from bertopic import BERTopic
from bertopic.backend import BaseEmbedder
from bertopic.vectorizers import ClassTfidfTransformer
from hdbscan import HDBSCAN
from scipy.cluster.hierarchy import linkage
from scipy.spatial.distance import squareform
from sklearn.feature_extraction.text import CountVectorizer
from sklearn.metrics import adjusted_rand_score, normalized_mutual_info_score, silhouette_score
from umap import UMAP

from topic_lexical import tokenize


class FixedReduction:
    def __init__(self, coordinates):
        self.coordinates = coordinates

    def fit(self, x, y=None):
        if len(x) != len(self.coordinates):
            raise ValueError("降维坐标与输入行数不一致")
        return self

    def transform(self, x):
        return self.coordinates


def analyze(documents, embeddings, options, *, chinese=False):
    n = len(documents)
    if n < 10 or embeddings.shape != (n, 1024) or not np.isfinite(embeddings).all():
        raise ValueError("语料不足或嵌入矩阵无效")
    norms = np.linalg.norm(embeddings, axis=1)
    if np.any(norms < 1e-10):
        raise ValueError("嵌入含零向量")
    embeddings = embeddings / norms[:, None]
    texts = [d["text"] for d in documents]
    seed = options.get("seed", 42)
    neighbors = min(15 if chinese else options.get("neighbors", 30), n - 1)
    coordinates = UMAP(
        n_neighbors=neighbors,
        n_components=5,
        min_dist=0.0,
        metric="cosine",
        random_state=seed,
        n_jobs=1,
        low_memory=True,
    ).fit_transform(embeddings)
    default_sizes = [10, 15, 25] if chinese else [30, 60, 100]
    sizes = sorted(
        {
            min(max(2, int(size)), max(2, n // 2))
            for size in (default_sizes if chinese else options.get("cluster_sizes", default_sizes))
        }
    )
    candidates = []
    best = None
    for size in sizes:
        vectorizer = CountVectorizer(
            tokenizer=tokenize, token_pattern=None, lowercase=False, ngram_range=(1, 2), min_df=1, max_features=50000
        )
        clusterer = HDBSCAN(
            min_cluster_size=size,
            min_samples=min(3 if chinese else 10, size),
            metric="euclidean",
            cluster_selection_method="eom",
            prediction_data=True,
            core_dist_n_jobs=2,
        )
        model = BERTopic(
            embedding_model=BaseEmbedder(),
            umap_model=FixedReduction(coordinates),
            hdbscan_model=clusterer,
            vectorizer_model=vectorizer,
            ctfidf_model=ClassTfidfTransformer(reduce_frequent_words=True),
            top_n_words=20,
            calculate_probabilities=False,
        )
        labels, strengths = model.fit_transform(texts, embeddings)
        labels = np.asarray(labels)
        assigned = labels >= 0
        ids = sorted(set(labels[assigned]))
        if len(ids) > 500:
            raise ValueError("主题数超过 500 个安全预算，请增大最小簇规模")
        silhouette = None
        positions = np.flatnonzero(assigned)
        if len(positions) > 1500:
            positions = np.random.default_rng(seed).choice(positions, 1500, replace=False)
        if 1 < len(set(labels[positions])) < len(positions):
            silhouette = float(silhouette_score(embeddings[positions], labels[positions], metric="cosine"))
        coherence = npmi(model, texts)
        words = [word for topic in ids for word, weight in model.get_topic(topic)[:10] if weight > 0]
        diversity = len(set(words)) / max(1, len(words))
        coverage = float(assigned.mean())
        score = (
            0.30 * coverage
            + 0.25 * ((silhouette or 0) + 1) / 2
            + 0.25 * ((np.mean(list(coherence.values())) if coherence else 0) + 1) / 2
            + 0.20 * diversity
        )
        metrics = {
            "min_cluster_size": size,
            "min_samples": clusterer.min_samples,
            "neighbors": neighbors,
            "topics": len(ids),
            "outlier_rate": 1 - coverage,
            "coverage": coverage,
            "silhouette": silhouette,
            "npmi": float(np.mean(list(coherence.values()))) if coherence else None,
            "diversity": diversity,
            "selection_score": float(score),
        }
        candidates.append(metrics)
        eligible = not chinese or size == min(15, max(2, n // 2))
        if eligible and (best is None or score > best[0]):
            best = score, model, labels, np.asarray(strengths), metrics, coherence
    _, model, labels, strengths, selected, coherence = best
    ids = sorted(set(labels) - {-1})
    centroids = np.stack([embeddings[labels == topic].mean(axis=0) for topic in ids]) if ids else np.empty((0, 1024))
    if ids:
        centroids /= np.linalg.norm(centroids, axis=1, keepdims=True)
        similarity = embeddings @ centroids.T
        nearest = similarity.argmax(axis=1)
    topics = []
    assignments = []
    for column, topic in enumerate(ids):
        positions = np.flatnonzero(labels == topic)
        representatives = []
        seen = set()
        for pos in positions[np.argsort(-similarity[positions, column])]:
            d = documents[pos]
            if d["text_sha256"] not in seen:
                representatives.append(d["document_key"])
                seen.add(d["text_sha256"])
            if len(representatives) == 5:
                break
        words = [{"term": word, "weight": float(weight)} for word, weight in model.get_topic(topic) if weight > 0]
        frequencies = Counter(word for pos in positions for word in tokenize(texts[pos]))
        topics.append(
            {
                "topic": int(topic),
                "count": len(positions),
                "label": " / ".join(w["term"] for w in words[:3]),
                "keywords": words,
                "word_frequencies": frequencies.most_common(100),
                "representatives": representatives,
                "npmi": coherence.get(topic),
                "centroid": centroids[column].tolist(),
            }
        )
    for i, d in enumerate(documents):
        assignments.append(
            {
                "document_key": d["document_key"],
                "topic": int(labels[i]),
                "membership": float(strengths[i]),
                "nearest_topic": int(ids[nearest[i]]) if ids else -1,
                "centroid_cosine": float(similarity[i, nearest[i]]) if ids else 0.0,
                "x": float(coordinates[i, 0]),
                "y": float(coordinates[i, 1]),
            }
        )
    groups = defaultdict(Counter)
    for d, topic in zip(documents, labels, strict=True):
        if d.get("year") is not None:
            groups[str(int(d["year"]))][int(topic)] += 1
    yearly = [
        {"year": int(year), "topic": topic, "count": count, "denominator": sum(counts.values())}
        for year, counts in sorted(groups.items())
        for topic, count in sorted(counts.items())
    ]
    cosine = centroids @ centroids.T
    pairs = [
        {"a": int(a), "b": int(b), "cosine": float(cosine[i, j])}
        for i, a in enumerate(ids)
        for j, b in enumerate(ids)
        if i < j
    ]
    hierarchy = (
        linkage(squareform(np.clip(1 - cosine, 0, 2), checks=False), method="average").tolist() if len(ids) > 1 else []
    )
    stability = {}
    if options.get("stability", True):
        alt = UMAP(
            n_neighbors=neighbors,
            n_components=5,
            min_dist=0.0,
            metric="cosine",
            random_state=7 if seed != 7 else 42,
            n_jobs=1,
            low_memory=True,
        ).fit_transform(embeddings)
        alternate = HDBSCAN(
            min_cluster_size=selected["min_cluster_size"], min_samples=selected["min_samples"], core_dist_n_jobs=2
        ).fit_predict(alt)
        both = (labels >= 0) & (alternate >= 0)
        stability = {
            "ari_all": float(adjusted_rand_score(labels, alternate)),
            "nmi_all": float(normalized_mutual_info_score(labels, alternate)),
            "ari_both_assigned": float(adjusted_rand_score(labels[both], alternate[both])) if both.any() else None,
            "both_assigned": int(both.sum()),
            "alternate_outlier_rate": float((alternate == -1).mean()),
            "seeds": [seed, 7 if seed != 7 else 42],
        }
    dynamic = []
    eligible = [
        i for i, d in enumerate(documents) if d.get("year") is not None and d["year"] <= options["analysis_year"]
    ]
    if ids and eligible:
        table = model.topics_over_time(
            [texts[i] for i in eligible],
            [(int(documents[i]["year"]) // 5) * 5 for i in eligible],
            topics=[int(labels[i]) for i in eligible],
        )
        dynamic = [
            {"topic": int(row.Topic), "year": int(row.Timestamp), "count": int(row.Frequency), "words": row.Words}
            for row in table.itertuples()
        ]
    return {
        "topics": topics,
        "assignments": assignments,
        "yearly": yearly,
        "similarity": pairs,
        "hierarchy": hierarchy,
        "candidates": candidates,
        "selected": selected,
        "stability": stability,
        "dynamic": dynamic,
        "warnings": (["未识别到可分离主题；全部记录保留为离群"] if not ids else []),
        "coordinates_method": "first_two_of_umap_5d",
        "analysis_year": options["analysis_year"],
    }


def npmi(model, documents):
    binary = model.vectorizer_model.transform(documents).astype(np.float64)
    binary.data.fill(1)
    vocab = model.vectorizer_model.vocabulary_
    result = {}
    for topic, words in model.get_topics().items():
        if topic < 0:
            continue
        ids = [vocab[word] for word, weight in words[:10] if word in vocab and weight > 0]
        subset = binary[:, ids]
        counts = np.asarray(subset.sum(axis=0)).ravel()
        pairs = (subset.T @ subset).toarray()
        scores = []
        for a in range(len(ids)):
            for b in range(a + 1, len(ids)):
                both = pairs[a, b]
                p = both / len(documents)
                scores.append(
                    -1.0
                    if both == 0
                    else 0.0
                    if p == 1
                    else float(np.log(p / ((counts[a] / len(documents)) * (counts[b] / len(documents)))) / -np.log(p))
                )
        result[int(topic)] = float(np.mean(scores)) if scores else -1.0
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    directory = args.directory
    payload = json.loads((directory / "input.json").read_text())
    documents, options = payload["documents"], payload["options"]
    embeddings = np.load(directory / "joint_bge_m3.npy", allow_pickle=False)
    models = {"joint_bge_m3": analyze(documents, embeddings, options)}
    if options.get("chinese_comparison"):
        positions = [i for i, d in enumerate(documents) if d["language_of_text"] == "zh"]
        chinese = [documents[i] for i in positions]
        if len(chinese) < 10:
            raise ValueError("中文子集少于 10 条，请关闭中文对照或增加语料")
        zh = np.load(directory / "chinese_bge_zh.npy", allow_pickle=False)
        models["chinese_bge_zh"] = analyze(chinese, zh, options, chinese=True)
        models["chinese_bge_m3_comparison"] = analyze(chinese, embeddings[positions], options, chinese=True)
    result = {
        "schema_version": "literature-topics-v1",
        "models": models,
        "method": {
            "text": "title+abstract; title-only retained",
            "noise": "retained as -1",
            "seed": options.get("seed", 42),
            "analysis_year": options["analysis_year"],
            "time_denominator": "all eligible records including noise",
            "versions": {
                name: importlib.metadata.version(name)
                for name in ("bertopic", "umap-learn", "hdbscan", "numpy", "scikit-learn", "jieba")
            },
            "selection": "heuristic among candidate cluster sizes, not a scientific quality threshold",
        },
    }
    (directory / "result.json").write_text(json.dumps(result, ensure_ascii=False, allow_nan=False), encoding="utf-8")


if __name__ == "__main__":
    main()
