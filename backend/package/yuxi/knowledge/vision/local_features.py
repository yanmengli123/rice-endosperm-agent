"""Deterministic local-feature geometry matching for figure screenshots.

This is the bounded fallback after exact SHA and pHash.  ORB descriptors propose
pixel correspondences and a RANSAC homography verifies that they agree on one
spatial transform.  A page is publishable only when match count, inlier ratio and
two-sided spatial coverage all pass; semantic similarity or a few repeated text
glyphs cannot satisfy the gate.
"""

from __future__ import annotations

from typing import Any

LOCAL_FEATURE_VERSION = "orb_homography_v1"
LOCAL_FEATURE_MIN_GOOD_MATCHES = 24
LOCAL_FEATURE_MIN_INLIERS = 20
LOCAL_FEATURE_MIN_INLIER_RATIO = 0.45
LOCAL_FEATURE_MIN_QUERY_COVERAGE = 0.12
LOCAL_FEATURE_MIN_CANDIDATE_COVERAGE = 0.08


def _coverage(points: Any, shape: tuple[int, int]) -> float:
    import cv2

    if points is None or len(points) < 3:
        return 0.0
    hull = cv2.convexHull(points)
    area = float(cv2.contourArea(hull))
    image_area = float(max(1, shape[0] * shape[1]))
    return area / image_area


def match_local_feature_geometry(query_bytes: bytes, candidate_bytes: bytes) -> dict[str, Any]:
    """Return audited ORB/RANSAC metrics; decode/feature failures fail closed."""
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
        strong = bool(
            len(good) >= LOCAL_FEATURE_MIN_GOOD_MATCHES
            and inliers >= LOCAL_FEATURE_MIN_INLIERS
            and inlier_ratio >= LOCAL_FEATURE_MIN_INLIER_RATIO
            and query_coverage >= LOCAL_FEATURE_MIN_QUERY_COVERAGE
            and candidate_coverage >= LOCAL_FEATURE_MIN_CANDIDATE_COVERAGE
        )
        return {
            "version": LOCAL_FEATURE_VERSION,
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
