# 保持本包 __init__ 为空：graphs 包内模块会导入 repositories（如 milvus_graph_service →
# knowledge_graph_repository），而仓储又 import graph_utils 触发本 __init__——任何急切的
# 服务级再导出都会闭合循环导入环。请始终从完整子模块路径导入，例如
# from yuxi.knowledge.graphs.milvus_graph_service import MilvusGraphService
