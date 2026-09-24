"""证据正交维度受控词表与「维度 × Claim 类别」许可矩阵（ADR-0006）。

E0–E4（``EvidenceLevel``）回答「这条 Claim 需要哪种证据及定位」；
本模块的正交维度回答「这条证据本身是什么性质」。两者必须分别判定：

- RNA-seq 共表达 = ``EXPERIMENTAL_ASSAY`` + ``ASSOCIATION``：是实验测量，
  但仍只是关联，永远不能支撑 ``CAUSAL_REGULATION``。
- 计算预测（``COMPUTATIONAL_PREDICTION``）无论置信度多高，都不能单独支撑
  互作 / 修饰 / 因果类机制 Claim。

许可矩阵是确定性代码、随 ``EVIDENCE_DIMENSION_VOCAB_VERSION`` 版本化；
矩阵变更 = 契约变更，必须走 ADR 评审。模型不参与本模块的任何判定。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

EVIDENCE_DIMENSION_VOCAB_VERSION = "evidence-dimensions.v1"


class MeasurementKind(StrEnum):
    """证据的测量性质。"""

    EXPERIMENTAL_ASSAY = "EXPERIMENTAL_ASSAY"
    COMPUTATIONAL_PREDICTION = "COMPUTATIONAL_PREDICTION"
    CURATED_ANNOTATION = "CURATED_ANNOTATION"
    LITERATURE_ASSERTION = "LITERATURE_ASSERTION"


class SupportType(StrEnum):
    """证据对 Claim 的支持性质（与是否实验测量正交）。"""

    DIRECT = "DIRECT"
    ASSOCIATION = "ASSOCIATION"
    ORTHOLOG_INFERENCE = "ORTHOLOG_INFERENCE"
    TEXT_MINING = "TEXT_MINING"


class AssayType(StrEnum):
    """实验类型受控词表（PSI-MI / ECO 子集对齐，禁止自由文本）。"""

    YEAST_TWO_HYBRID = "YEAST_TWO_HYBRID"
    PULL_DOWN = "PULL_DOWN"
    CO_IP = "CO_IP"
    BIFC = "BIFC"
    CHIP_SEQ = "CHIP_SEQ"
    EMSA = "EMSA"
    PHOSPHOPROTEOMICS = "PHOSPHOPROTEOMICS"
    MASS_SPECTROMETRY = "MASS_SPECTROMETRY"
    RNA_SEQ = "RNA_SEQ"
    KNOCKOUT = "KNOCKOUT"
    CRISPR_PERTURBATION = "CRISPR_PERTURBATION"
    OVEREXPRESSION = "OVEREXPRESSION"
    REPORTER_ASSAY = "REPORTER_ASSAY"
    QTL_MAPPING = "QTL_MAPPING"
    FIELD_TRIAL = "FIELD_TRIAL"
    OTHER = "OTHER"


class ClaimClass(StrEnum):
    """Claim 类型闭集——Source Registry 的 supported_claim_types 取值于此。"""

    DATASET_METADATA = "DATASET_METADATA"
    RECORD_FACT = "RECORD_FACT"
    BIBLIOGRAPHIC_FACT = "BIBLIOGRAPHIC_FACT"
    ASSOCIATION = "ASSOCIATION"
    MOLECULAR_INTERACTION = "MOLECULAR_INTERACTION"
    PTM_MODIFICATION = "PTM_MODIFICATION"
    CAUSAL_REGULATION = "CAUSAL_REGULATION"
    LOCATOR_FACT = "LOCATOR_FACT"


class PredictionConfidence(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


# 互作实验：可直接支撑 MOLECULAR_INTERACTION 的实验类型
_DIRECT_INTERACTION_ASSAYS = frozenset(
    {AssayType.YEAST_TWO_HYBRID, AssayType.PULL_DOWN, AssayType.CO_IP, AssayType.BIFC, AssayType.EMSA}
)
# 修饰实验：可直接支撑 PTM_MODIFICATION 的实验类型
_DIRECT_PTM_ASSAYS = frozenset({AssayType.PHOSPHOPROTEOMICS, AssayType.MASS_SPECTROMETRY})
# 扰动实验：可直接支撑 CAUSAL_REGULATION 的实验类型
_DIRECT_CAUSAL_ASSAYS = frozenset(
    {AssayType.KNOCKOUT, AssayType.CRISPR_PERTURBATION, AssayType.OVEREXPRESSION, AssayType.REPORTER_ASSAY}
)
# 任何测量性质下都可支撑的低义务 Claim 类别
_LOW_OBLIGATION_CLAIMS = frozenset(
    {ClaimClass.DATASET_METADATA, ClaimClass.RECORD_FACT, ClaimClass.BIBLIOGRAPHIC_FACT, ClaimClass.LOCATOR_FACT}
)

REASON_OK_DIRECT_ASSAY = "OK_DIRECT_ASSAY"
REASON_OK_ASSOCIATION_CLAIM = "OK_ASSOCIATION_CLAIM"
REASON_OK_LOW_OBLIGATION = "OK_LOW_OBLIGATION"
REASON_REJECT_PREDICTION_MECHANISM = "REJECT_PREDICTION_CANNOT_PROVE_MECHANISM"
REASON_REJECT_ASSOCIATION_ONLY = "REJECT_ASSOCIATION_CANNOT_PROVE_MECHANISM"
REASON_REJECT_TEXT_MINING = "REJECT_TEXT_MINING_CANNOT_PROVE_MECHANISM"
REASON_REJECT_ORTHOLOG_ONLY = "REJECT_ORTHOLOG_INFERENCE_REQUIRES_DIRECT_CONFIRMATION"
REASON_REJECT_ASSAY_MISMATCH = "REJECT_ASSAY_TYPE_MISMATCH"
REASON_REJECT_LITERATURE_ASSERTION = "REJECT_LITERATURE_ASSERTION_REQUIRES_PRIMARY_EVIDENCE"
REASON_REJECT_CURATED_NEEDS_PRIMARY = "REJECT_CURATED_ANNOTATION_NEEDS_PRIMARY_ASSAY"

_MECHANISM_CLAIMS = frozenset(
    {ClaimClass.MOLECULAR_INTERACTION, ClaimClass.PTM_MODIFICATION, ClaimClass.CAUSAL_REGULATION}
)


@dataclass(frozen=True, slots=True)
class ClaimSupportDecision:
    """维度许可矩阵的确定性裁决（可进运行清单的 reason_code）。"""

    supported: bool
    reason_code: str
    vocab_version: str = EVIDENCE_DIMENSION_VOCAB_VERSION


def evaluate_claim_support(
    measurement_kind: MeasurementKind,
    support_type: SupportType,
    claim_class: ClaimClass,
    *,
    assay_types: frozenset[AssayType] | None = None,
) -> ClaimSupportDecision:
    """判定一条证据的维度组合是否被允许支撑指定类别的 Claim。

    这是许可矩阵的唯一入口；发布门禁与 Claim 核验层共用，禁止旁路。
    """
    assays = frozenset(assay_types or ())
    if claim_class in _LOW_OBLIGATION_CLAIMS:
        return ClaimSupportDecision(True, REASON_OK_LOW_OBLIGATION)
    if measurement_kind == MeasurementKind.COMPUTATIONAL_PREDICTION:
        return ClaimSupportDecision(False, REASON_REJECT_PREDICTION_MECHANISM)
    if support_type == SupportType.TEXT_MINING:
        return ClaimSupportDecision(False, REASON_REJECT_TEXT_MINING)
    if support_type == SupportType.ORTHOLOG_INFERENCE:
        return ClaimSupportDecision(False, REASON_REJECT_ORTHOLOG_ONLY)
    if claim_class == ClaimClass.ASSOCIATION:
        if support_type == SupportType.ASSOCIATION:
            return ClaimSupportDecision(True, REASON_OK_ASSOCIATION_CLAIM)
        return ClaimSupportDecision(False, REASON_REJECT_ASSAY_MISMATCH)
    # 以下为机制类 Claim：只有 DIRECT 支持性质的实验测量可进入实验类型比对。
    if support_type != SupportType.DIRECT or measurement_kind != MeasurementKind.EXPERIMENTAL_ASSAY:
        if support_type == SupportType.ASSOCIATION:
            return ClaimSupportDecision(False, REASON_REJECT_ASSOCIATION_ONLY)
        if measurement_kind == MeasurementKind.CURATED_ANNOTATION:
            return ClaimSupportDecision(False, REASON_REJECT_CURATED_NEEDS_PRIMARY)
        return ClaimSupportDecision(False, REASON_REJECT_LITERATURE_ASSERTION)
    required = {
        ClaimClass.MOLECULAR_INTERACTION: _DIRECT_INTERACTION_ASSAYS,
        ClaimClass.PTM_MODIFICATION: _DIRECT_PTM_ASSAYS,
        ClaimClass.CAUSAL_REGULATION: _DIRECT_CAUSAL_ASSAYS,
    }[claim_class]
    if assays & required:
        return ClaimSupportDecision(True, REASON_OK_DIRECT_ASSAY)
    return ClaimSupportDecision(False, REASON_REJECT_ASSAY_MISMATCH)


__all__ = [
    "EVIDENCE_DIMENSION_VOCAB_VERSION",
    "AssayType",
    "ClaimClass",
    "ClaimSupportDecision",
    "MeasurementKind",
    "PredictionConfidence",
    "SupportType",
    "evaluate_claim_support",
]
