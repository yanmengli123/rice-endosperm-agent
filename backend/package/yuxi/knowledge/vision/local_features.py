"""Deterministic local-feature geometry matching for figure screenshots.

This is the bounded fallback after exact SHA and pHash.  ORB descriptors propose
pixel correspondences and a RANSAC homography verifies that they agree on one
spatial transform.  A page is publishable only when match count, inlier ratio and
two-sided spatial coverage all pass; semantic similarity or a few repeated text
glyphs cannot satisfy the gate.
"""

from __future__ import annotations

from typing import Any

LOCAL_FEATURE_VERSION = "orb_homography_v2"
LOCAL_FEATURE_MIN_GOOD_MATCHES = 24
LOCAL_FEATURE_MIN_INLIERS = 20
LOCAL_FEATURE_MIN_INLIER_RATIO = 0.45
LOCAL_FEATURE_MIN_QUERY_COVERAGE = 0.12
LOCAL_FEATURE_MIN_CANDIDATE_COVERAGE = 0.08
# "截图包含候选"形态：候选被内点凸包覆盖 ≥ 50%，查询侧只需 ≥ 2%（单 panel 在整图中的占比）
_SUPERSET_MIN_CANDIDATE_COVERAGE = 0.5
_SUPERSET_MIN_QUERY_COVERAGE = 0.02


def local_feature_thresholds(query_short_side: int) -> dict[str, float]:
    """按查询图短边分级的发布门限（确定性、随 metrics 一并审计）。

    小图（单 panel 截图，常见 250–350px）特征点天然少，沿用大图门限会把真实命中整体挡在
    门外；分级后仍要求 RANSAC 几何一致 + 双向覆盖，只是把"多少个一致点算够"随尺寸缩放。
    """
    if query_short_side >= 600:
        return {
            "profile": "large",
            "min_good": LOCAL_FEATURE_MIN_GOOD_MATCHES,
            "min_inliers": LOCAL_FEATURE_MIN_INLIERS,
            "min_inlier_ratio": LOCAL_FEATURE_MIN_INLIER_RATIO,
            "min_query_coverage": LOCAL_FEATURE_MIN_QUERY_COVERAGE,
            "min_candidate_coverage": LOCAL_FEATURE_MIN_CANDIDATE_COVERAGE,
        }
    if query_short_side >= 300:
        return {
            "profile": "medium",
            "min_good": 16,
            "min_inliers": 14,
            "min_inlier_ratio": 0.35,
            "min_query_coverage": 0.10,
            "min_candidate_coverage": 0.06,
        }
    return {
        "profile": "small",
        "min_good": 12,
        "min_inliers": 10,
        "min_inlier_ratio": 0.30,
        "min_query_coverage": 0.08,
        "min_candidate_coverage": 0.05,
    }


def _coverage(points: Any, shape: tuple[int, int]) -> float:
    import cv2

    if points is None or len(points) < 3:
        return 0.0
    hull = cv2.convexHull(points)
    area = float(cv2.contourArea(hull))
    image_area = float(max(1, shape[0] * shape[1]))
    return area / image_area


def match_local_feature_geometry(
    query_bytes: bytes, candidate_bytes: bytes, *, thresholds: dict[str, float] | None = None
) -> dict[str, Any]:
    """Return audited ORB/RANSAC metrics; decode/feature failures fail closed.

    ``thresholds`` 缺省按查询图短边分级（:func:`local_feature_thresholds`），并随 metrics 回传供审计。
    """
    empty = {
        "version": LOCAL_FEATURE_VERSION,
        "strong": False,
        "good_matches": 0,
        "inliers": 0,
        "inlier_ratio": 0.0,
        "query_coverage": 0.0,
        "candidate_coverage": 0.0,
    }
    if not query_bytes or not candidate_bytes:
        return empty
    try:
        import cv2
        import numpy as np

        query = cv2.imdecode(np.frombuffer(query_bytes, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
        candidate = cv2.imdecode(np.frombuffer(candidate_bytes, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
        if query is None or candidate is None:
            return empty
        gate = dict(thresholds or local_feature_thresholds(int(min(query.shape[:2]))))
        empty["thresholds"] = gate

        def bounded(image):
            height, width = image.shape[:2]
            scale = min(1.0, 1600.0 / max(height, width))
            if scale < 1.0:
                return cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
            return image

        query = bounded(query)
        candidate = bounded(candidate)
        detector = cv2.ORB_create(nfeatures=2000, scaleFactor=1.2, nlevels=8, fastThreshold=10)
        query_points, query_descriptors = detector.detectAndCompute(query, None)
        candidate_points, candidate_descriptors = detector.detectAndCompute(candidate, None)
        if query_descriptors is None or candidate_descriptors is None:
            return empty
        pairs = cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(query_descriptors, candidate_descriptors, k=2)
        good = [first for first, second in pairs if first.distance < 0.72 * second.distance]
        if len(good) < 4:
            return {**empty, "good_matches": len(good)}

        query_xy = np.float32([query_points[item.queryIdx].pt for item in good])
        candidate_xy = np.float32([candidate_points[item.trainIdx].pt for item in good])
        _homography, mask = cv2.findHomography(
            query_xy.reshape(-1, 1, 2),
            candidate_xy.reshape(-1, 1, 2),
            cv2.RANSAC,
            4.0,
        )
        if mask is None:
            return {**empty, "good_matches": len(good)}
        inlier_mask = mask.ravel().astype(bool)
        inliers = int(inlier_mask.sum())
        inlier_ratio = inliers / max(1, len(good))
        query_coverage = _coverage(query_xy[inlier_mask], query.shape[:2])
        candidate_coverage = _coverage(candidate_xy[inlier_mask], candidate.shape[:2])
        # 覆盖判据两种合法形态：截图是候选的一部分（query 被大面积覆盖）；或截图包含候选
        # （整张多 panel 图的截图 vs 库内单 panel 资产：candidate 被大面积覆盖，query 覆盖天然很小）
        coverage_ok = (
            query_coverage >= gate["min_query_coverage"] and candidate_coverage >= gate["min_candidate_coverage"]
        ) or (candidate_coverage >= _SUPERSET_MIN_CANDIDATE_COVERAGE and query_coverage >= _SUPERSET_MIN_QUERY_COVERAGE)
        strong = bool(
            len(good) >= gate["min_good"]
            and inliers >= gate["min_inliers"]
            and inlier_ratio >= gate["min_inlier_ratio"]
            and coverage_ok
        )
        return {
            "version": LOCAL_FEATURE_VERSION,
            "thresholds": gate,
            "strong": strong,
            "good_matches": len(good),
            "inliers": inliers,
            "inlier_ratio": round(inlier_ratio, 4),
            "query_coverage": round(query_coverage, 4),
            "candidate_coverage": round(candidate_coverage, 4),
        }
    except Exception:  # noqa: BLE001 - an unavailable/invalid CV stack is not authority
        return empty


__all__ = [
    "LOCAL_FEATURE_VERSION",
    "match_local_feature_geometry",
]
