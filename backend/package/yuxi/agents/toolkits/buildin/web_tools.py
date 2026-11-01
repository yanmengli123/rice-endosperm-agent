"""Provider-neutral web tools; registered through the existing permission pipeline."""

from langchain_core.tools import ToolException
from yuxi.agents.toolkits.registry import tool
from yuxi.services.web_tools.errors import InvalidInputError
from yuxi.services.web_tools.schemas import SearchInput, FetchInput
from yuxi.services.web_tools.service import search_web, fetch_web


@tool(category='buildin', tags=['搜索'], display_name='网络搜索', args_schema=SearchInput,
      config_guide='管理员在设置 → 网络搜索中选择供应商和配置凭据')
async def web_search(query: str, count: int = 10, time_range: str | None = None, sites: list[str] | None = None,
                     block_hosts: list[str] | None = None, content_format: str = 'markdown',
                     include_raw_content: bool = False):
    """Search the public web. Results contain external snippets, not verified knowledge-base evidence."""
    try:
        return await search_web(SearchInput(query=query, count=count, time_range=time_range, sites=sites,
                                            block_hosts=block_hosts, content_format=content_format,
                                            include_raw_content=include_raw_content))
    except InvalidInputError as error:
        raise ToolException(str(error)) from None


@tool(category='buildin', tags=['搜索'], display_name='读取网页正文', args_schema=FetchInput,
      config_guide='管理员在设置 → 网络搜索中配置支持正文抓取的供应商')
async def web_fetch(url: str):
    """Read a public webpage through the configured provider. Web content remains external information."""
    try:
        return await fetch_web(FetchInput(url=url))
    except InvalidInputError as error:
        raise ToolException(str(error)) from None
