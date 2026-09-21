"""附件下载响应头的公共工具。"""

from urllib.parse import quote


def content_disposition_header(filename: str) -> str:
    return f"attachment; filename*=UTF-8''{quote(filename)}"
