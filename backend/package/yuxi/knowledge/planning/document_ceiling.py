"""Optional server-authored document ceiling; text/model choices can only narrow it."""


def constrain_document_ids(snapshot: dict, file_ids: list[str] | None) -> list[str] | None:
    ceiling = snapshot.get("document_scope_ceiling")
    if ceiling is None:
        return file_ids
    if not isinstance(ceiling, dict) or not isinstance(ceiling.get("file_ids"), list):
        raise ValueError("运行的文档范围上限损坏；拒绝检索")
    allowed = list(dict.fromkeys(str(value).strip() for value in ceiling["file_ids"] if str(value).strip()))
    if not allowed:
        raise ValueError("当前会话没有已授权的原文证据文档；阅读产物不能冒充原文证据")
    requested = set(file_ids or [])
    if requested and not requested.intersection(allowed):
        raise ValueError("请求文献不在当前会话的服务端绑定范围内，请新建跨文献会话")
    return [value for value in allowed if not requested or value in requested]
