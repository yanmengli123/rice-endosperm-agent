"""Keep connector credentials out of public database metadata and request logs."""

from yuxi.utils.secret_crypto import SECRET_UNCHANGED_MARKER, is_encrypted


def public_connector_info(info: dict) -> dict:
    if info.get("kb_type") != "feishu":
        return info
    result = dict(info)
    for key in ("metadata", "additional_params"):
        if isinstance(result.get(key), dict):
            params = dict(result[key])
            params.pop("feishu_secret_id", None)
            if params.get("feishu_user_token"):
                params["feishu_user_token"] = SECRET_UNCHANGED_MARKER
            result[key] = params
    return result


def submitted_feishu_params(submitted: dict, existing: dict | None = None) -> dict:
    params = dict(submitted)
    params.pop("feishu_secret_id", None)
    token = params.get("feishu_user_token")
    if token == SECRET_UNCHANGED_MARKER or (existing is not None and not token):
        params["feishu_user_token"] = existing.get("feishu_user_token") if existing else None
        params["feishu_secret_id"] = existing.get("feishu_secret_id") if existing else None
    elif is_encrypted(token):
        raise ValueError("不能从请求导入加密的飞书凭据，请重新输入 User Access Token")
    from yuxi.knowledge.implementations.feishu import FeishuKB

    return FeishuKB.normalize_additional_params({**(existing or {}), **params})
