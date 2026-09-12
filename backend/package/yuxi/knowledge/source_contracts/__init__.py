"""Source Contract 模块：知识源契约的注册中心与命令门禁。

公开接口::

    from yuxi.knowledge.source_contracts import (
        resolve_contract,            # fail-closed 解析
        contract_registry_snapshot,  # 只读快照（前端/路由）
        load_kb_contract,            # 读取库上冻结的契约
        require_contract_command,    # Contract Command Gate
        validate_contract_media,     # 媒体类型校验（415）
        classify_legacy_kb,          # 存量回填分类
        SourceContractError,         # 422 基类
        UnknownSourceContractError,
        ContractCommandForbidden,
        ContractMediaRejected,
    )
"""

from yuxi.knowledge.source_contracts.definitions import (  # noqa: F401
    ALL_COMMANDS,
    COMMAND_ARCHIVE,
    COMMAND_DATASET_IMPORT,
    COMMAND_DATASET_PREVIEW,
    COMMAND_DOCUMENT_ADD,
    COMMAND_DOCUMENT_DELETE,
    COMMAND_DOCUMENT_INDEX,
    COMMAND_DOCUMENT_MOVE,
    COMMAND_DOCUMENT_PARSE,
    COMMAND_DOCUMENT_UPLOAD,
    COMMAND_FETCH_URL,
    COMMAND_FOLDER_CREATE,
    COMMAND_GRAPH_IMPORT_EXECUTE,
    COMMAND_GRAPH_IMPORT_ROLLBACK,
    COMMAND_GRAPH_IMPORT_UPLOAD,
    COMMAND_GRAPH_IMPORT_VALIDATE,
    COMMAND_LLM_GRAPH_BUILD,
    COMMAND_LLM_GRAPH_CONFIG,
    COMMAND_LLM_GRAPH_RESET,
    COMMAND_MINDMAP_GENERATE,
    COMMAND_RELEASE_CREATE,
    COMMAND_RELEASE_PUBLISH,
    COMMAND_RELEASE_ROLLBACK,
    COMMAND_RETRIEVAL_POLICY_CREATE,
    COMMAND_SAMPLE_QUESTIONS,
    COMMAND_SCIENTIFIC_PDF_RETRY,
    COMMAND_STATS_REPAIR,
)
from yuxi.knowledge.source_contracts.gate import (
    ContractCommandForbidden,
    ContractMediaRejected,
    classify_legacy_kb,
    load_kb_contract,
    require_contract_command,
    validate_contract_media,
)
from yuxi.knowledge.source_contracts.registry import (
    SourceContractError,
    UnknownSourceContractError,
    contract_registry_snapshot,
    registered_contracts,
    resolve_contract,
)
from yuxi.knowledge.source_contracts.specs import (
    SourceContractDisplay,
    SourceContractMediaRule,
    SourceContractSpec,
    contract_digest,
    spec_to_api_dict,
)

__all__ = [
    "ALL_COMMANDS",
    "COMMAND_ARCHIVE",
    "COMMAND_DATASET_IMPORT",
    "COMMAND_DATASET_PREVIEW",
    "COMMAND_DOCUMENT_ADD",
    "COMMAND_DOCUMENT_DELETE",
    "COMMAND_DOCUMENT_INDEX",
    "COMMAND_DOCUMENT_MOVE",
    "COMMAND_DOCUMENT_PARSE",
    "COMMAND_DOCUMENT_UPLOAD",
    "COMMAND_FETCH_URL",
    "COMMAND_FOLDER_CREATE",
    "COMMAND_GRAPH_IMPORT_EXECUTE",
    "COMMAND_GRAPH_IMPORT_ROLLBACK",
    "COMMAND_GRAPH_IMPORT_UPLOAD",
    "COMMAND_GRAPH_IMPORT_VALIDATE",
    "COMMAND_LLM_GRAPH_BUILD",
    "COMMAND_LLM_GRAPH_CONFIG",
    "COMMAND_LLM_GRAPH_RESET",
    "COMMAND_MINDMAP_GENERATE",
    "COMMAND_RELEASE_CREATE",
    "COMMAND_RELEASE_PUBLISH",
    "COMMAND_RELEASE_ROLLBACK",
    "COMMAND_RETRIEVAL_POLICY_CREATE",
    "COMMAND_SAMPLE_QUESTIONS",
    "COMMAND_SCIENTIFIC_PDF_RETRY",
    "COMMAND_STATS_REPAIR",
    "ContractCommandForbidden",
    "ContractMediaRejected",
    "SourceContractDisplay",
    "SourceContractError",
    "SourceContractMediaRule",
    "SourceContractSpec",
    "UnknownSourceContractError",
    "classify_legacy_kb",
    "contract_digest",
    "contract_registry_snapshot",
    "load_kb_contract",
    "registered_contracts",
    "require_contract_command",
    "resolve_contract",
    "spec_to_api_dict",
    "validate_contract_media",
]
