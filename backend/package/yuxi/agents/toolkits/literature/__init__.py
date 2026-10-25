"""文献域 Agent 工具包（P6b）：触发 @tool 装饰器注册（category="literature"）。"""

from yuxi.agents.toolkits.literature.tools import (
    get_citation_neighbors,
    list_paper_attachments,
    search_literature_papers,
)

__all__ = [
    "get_citation_neighbors",
    "list_paper_attachments",
    "search_literature_papers",
]
