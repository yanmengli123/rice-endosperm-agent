from yuxi.knowledge.base import KBNotFoundError, KnowledgeBase
from yuxi.knowledge.products.registry import get_product_spec, registry_snapshot
from yuxi.utils import logger


class KnowledgeBaseFactory:
    """知识库工厂类，负责创建不同类型的知识库实例"""

    # 注册的知识库类型映射 {kb_type: kb_class}
    _kb_types: dict[str, type[KnowledgeBase]] = {}

    @classmethod
    def register(cls, kb_class: type[KnowledgeBase]):
        """
        注册知识库类型

        Args:
            kb_class: 知识库类
        """
        if not issubclass(kb_class, KnowledgeBase):
            raise ValueError("Knowledge base class must inherit from KnowledgeBase")
        if not kb_class.kb_type:
            raise ValueError("Knowledge base class must define kb_type")
        spec = get_product_spec(kb_class.kb_type)
        if spec.category == "derived_product":
            # 派生知识产品（如 llmwiki）由 Wiki 控制面/编译面/发布面管理，
            # 永远不能注册为 KnowledgeBase 存储适配器——否则它会获得
            # 文件上传与 aquery 能力，混淆"原始证据"和"派生知识"。
            raise ValueError(
                f"kb_type={kb_class.kb_type!r} is a derived product; "
                "register it with the Wiki control plane instead of KnowledgeBaseFactory"
            )

        cls._kb_types[kb_class.kb_type] = kb_class
        # logger.info(f"Registered knowledge base type: {kb_class.kb_type}")

    @classmethod
    def create(cls, kb_type: str, work_dir: str, **kwargs) -> KnowledgeBase:
        """
        创建知识库实例

        Args:
            kb_type: 知识库类型
            work_dir: 工作目录
            **kwargs: 其他初始化参数

        Returns:
            知识库实例

        Raises:
            KBNotFoundError: 未知的知识库类型
        """
        spec = get_product_spec(kb_type)
        if spec.category == "derived_product":
            raise KBNotFoundError(
                f"kb_type={spec.kb_type!r} is a derived knowledge product without a storage "
                "adapter; create it through the Wiki control plane"
            )
        if kb_type not in cls._kb_types:
            available_types = list(cls._kb_types.keys())
            raise KBNotFoundError(f"Unknown knowledge base type: {kb_type}. Available types: {available_types}")

        kb_class = cls._kb_types[kb_type]

        try:
            # 创建实例
            instance = kb_class(work_dir, **kwargs)
            logger.info(f"Created {kb_type} knowledge base instance at {work_dir}")
            return instance
        except Exception as e:
            logger.error(f"Failed to create {kb_type} knowledge base: {e}")
            raise

    @classmethod
    def get_available_types(cls) -> dict[str, dict]:
        """
        获取所有可用的知识库类型

        Returns:
            知识库类型信息字典
        """
        result = {}
        for kb_type, kb_class in cls._kb_types.items():
            spec = get_product_spec(kb_type)
            result[kb_type] = {
                "name": kb_class.name,
                "description": kb_class.description,
                "requires_embedding_model": kb_class.requires_embedding_model,
                "supports_documents": kb_class.supports_documents,
                "create_params": kb_class.get_create_params_config(),
                "category": spec.category,
                "trust_class": spec.trust_class,
                "authority_class": spec.authority_class,
            }
        return result

    @classmethod
    def get_product_registry(cls) -> list[dict]:
        """暴露产品能力注册中心快照（含尚未有存储适配器的派生产品）。"""
        return registry_snapshot()

    @classmethod
    def get_kb_class(cls, kb_type: str) -> type[KnowledgeBase]:
        """
        获取指定类型的知识库类。

        Args:
            kb_type: 知识库类型

        Returns:
            知识库类
        """
        spec = get_product_spec(kb_type)
        if spec.category == "derived_product":
            raise KBNotFoundError(f"kb_type={spec.kb_type!r} is a derived knowledge product without a storage adapter")
        if kb_type not in cls._kb_types:
            available_types = list(cls._kb_types.keys())
            raise KBNotFoundError(f"Unknown knowledge base type: {kb_type}. Available types: {available_types}")
        return cls._kb_types[kb_type]

    @classmethod
    def is_type_supported(cls, kb_type: str) -> bool:
        """
        检查是否支持指定的知识库类型

        Args:
            kb_type: 知识库类型

        Returns:
            是否支持
        """
        if get_product_spec(kb_type).category == "derived_product":
            return False
        return kb_type in cls._kb_types
