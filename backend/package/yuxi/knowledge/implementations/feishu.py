"""飞书知识空间的在线只读检索与正文阅读。"""

import re
import asyncio
from typing import Any
from uuid import uuid4

import httpx

from yuxi.knowledge.implementations.read_only_connectors import ReadOnlyConnectors
from yuxi.utils.secret_crypto import decrypt_secret, encrypt_secret, is_encrypted, SECRET_UNCHANGED_MARKER


class UpstreamError(RuntimeError):
    def __init__(self, message, *, code):
        super().__init__(message)
        self.code = code


FEISHU_DEFAULT_TOP_K = 10
FEISHU_MAX_TOP_K = 50


class FeishuKB(ReadOnlyConnectors):
    """使用管理员配置的用户凭证访问指定飞书知识空间。"""

    kb_type = "feishu"
    name = "飞书"
    description = "在线搜索飞书知识库并阅读新版普通文档，不同步入库"
    supports_document_read = True

    @classmethod
    def get_create_params_config(cls) -> dict[str, Any]:
        """声明连接配置，不提供用户登录或凭证自动刷新。"""
        return {
            "options": [
                {
                    "key": "feishu_user_token",
                    "label": "User Access Token",
                    "type": "password",
                    "required": True,
                    "description": (
                        "用户凭证需有知识库和新版文档只读权限；到期后需更换。"
                        "获授权的 Yuxi 用户共享该凭证的读取范围。"
                    ),
                },
                {
                    "key": "feishu_space_id",
                    "label": "知识空间 ID",
                    "type": "text",
                    "required": True,
                    "description": "飞书知识库设置页面 URL 中的数字 ID；仅搜索和阅读此空间内的新版普通文档。",
                },
            ]
        }

    @classmethod
    def validate_additional_params(cls, additional_params: dict | None) -> dict:
        """校验凭证及知识空间标识。"""
        params = dict(additional_params or {})
        for key in ("feishu_user_token", "feishu_space_id"):
            value = params.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"飞书参数缺失: {key}")
            params[key] = value.strip()
        if any(char.isspace() for char in params["feishu_user_token"]):
            raise ValueError("飞书 User Access Token 不能包含空白字符")
        if len(params["feishu_user_token"]) > 16384 or params["feishu_user_token"] == SECRET_UNCHANGED_MARKER:
            raise ValueError("飞书 User Access Token 无效")
        if not is_encrypted(params["feishu_user_token"]):
            params["feishu_secret_id"] = str(uuid4())
            params["feishu_user_token"] = encrypt_secret(
                params["feishu_user_token"], f"feishu-kb:{params['feishu_secret_id']}"
            )
        if not isinstance(params.get("feishu_secret_id"), str) or len(params["feishu_secret_id"]) != 36:
            raise ValueError("飞书凭据绑定无效")
        if not re.fullmatch(r"[0-9]+", params["feishu_space_id"]):
            raise ValueError("飞书知识空间 ID 必须为数字")
        return params

    def get_query_params_config(self, kb_id: str, **kwargs) -> dict:
        """只提供原生搜索的返回数量。"""
        del kb_id, kwargs
        return {
            "type": self.kb_type,
            "options": [
                {
                    "key": "final_top_k",
                    "label": "最多返回文档数",
                    "type": "number",
                    "default": FEISHU_DEFAULT_TOP_K,
                    "min": 1,
                    "max": FEISHU_MAX_TOP_K,
                }
            ],
        }

    async def aquery(
        self,
        query_text: str,
        kb_id: str,
        *,
        agent_call: bool = False,
        **kwargs,
    ) -> list[dict]:
        """调用飞书 Wiki 搜索，返回文档候选而非伪造的正文片段。"""
        del agent_call
        query = query_text.strip()
        if not query or len(query) > 50:
            raise ValueError("飞书搜索关键词长度须为 1 到 50 个字符")
        params = self._connection(kb_id)
        options = {**((self.databases_meta[kb_id].get("query_params") or {}).get("options") or {}), **kwargs}
        top_k = min(max(int(options.get("final_top_k", FEISHU_DEFAULT_TOP_K)), 1), FEISHU_MAX_TOP_K)
        file_name = str(options.get("file_name") or "").casefold()
        results = []
        page_token = None
        seen_tokens = set()
        seen_nodes = set()
        async with httpx.AsyncClient(timeout=30.0, trust_env=False, follow_redirects=False) as client:
            for _page in range(20):
                data = await self._request(
                    client,
                    params,
                    "POST",
                    "/wiki/v2/nodes/search",
                    json={"query": query, "space_id": params["feishu_space_id"]},
                    params={"page_size": 50, **({"page_token": page_token} if page_token else {})},
                )
                items = data.get("items")
                if not isinstance(items, list):
                    raise ValueError("飞书搜索响应缺少文档列表")
                for item in items:
                    if not isinstance(item, dict) or str(item.get("space_id")) != params["feishu_space_id"]:
                        raise ValueError("飞书搜索结果超出配置的知识空间")
                    if item.get("obj_type") != 8:
                        continue
                    node_id = item.get("node_id")
                    self._validate_node_id(node_id)
                    title = item.get("title")
                    if not isinstance(title, str):
                        raise ValueError("飞书搜索响应缺少文档标题")
                    if node_id in seen_nodes or (file_name and file_name not in title.casefold()):
                        continue
                    seen_nodes.add(node_id)
                    results.append(
                        {
                            "content": title,
                            "metadata": {
                                "source": title,
                                "file_id": node_id,
                                "result_type": "document",
                                "content_role": "document_title",
                            },
                        }
                    )
                    if len(results) >= top_k:
                        return results
                if not data.get("has_more"):
                    return results
                page_token = data.get("page_token")
                if not isinstance(page_token, str) or not page_token or page_token in seen_tokens:
                    raise ValueError("飞书搜索分页凭证缺失或重复")
                seen_tokens.add(page_token)
        raise ValueError("飞书搜索分页达到上限，请缩小关键词范围")

    def _connection(self, kb_id):
        params = self.validate_additional_params(self.databases_meta[kb_id]["metadata"])
        params["feishu_user_token"] = decrypt_secret(
            params["feishu_user_token"], f"feishu-kb:{params['feishu_secret_id']}"
        )
        return params

    async def open_file_content(self, kb_id, file_id, offset=0, limit=800):
        content = await self._document_content(kb_id, file_id)
        return self._build_open_file_window(content, offset=offset, limit=limit)

    async def find_file_content(
        self, kb_id, file_id, patterns, *, use_regex=False, case_sensitive=False, max_windows=5, window_size=80
    ):
        if use_regex:
            raise ValueError("飞书文档查找仅支持字面关键词")
        content = await self._document_content(kb_id, file_id)
        return await asyncio.to_thread(
            self._build_find_file_windows,
            content,
            patterns=patterns,
            use_regex=False,
            case_sensitive=case_sensitive,
            max_windows=max_windows,
            window_size=window_size,
        )

    async def _document_content(
        self,
        kb_id: str,
        file_id: str,
    ) -> str:
        """复查节点归属后读取当前正文，不缓存文档权限或内容。"""
        self._validate_node_id(file_id)
        params = self._connection(kb_id)
        async with httpx.AsyncClient(timeout=30.0, trust_env=False, follow_redirects=False) as client:
            data = await self._request(client, params, "GET", "/wiki/v2/spaces/get_node", params={"token": file_id})
            node = data.get("node")
            if not isinstance(node, dict) or str(node.get("space_id")) != params["feishu_space_id"]:
                raise ValueError("文档不属于配置的飞书知识空间")
            if node.get("node_type") == "shortcut":
                raise ValueError("飞书连接器暂不支持跨节点快捷方式，请打开原始文档")
            if node.get("obj_type") != "docx":
                raise ValueError("飞书连接器仅支持新版普通文档（Docx）正文阅读")
            document_id = node.get("obj_token")
            self._validate_node_id(document_id)
            data = await self._request(client, params, "GET", f"/docx/v1/documents/{document_id}/raw_content")
        content = data.get("content")
        if not isinstance(content, str):
            raise ValueError("飞书正文响应缺少文本内容")
        if len(content) > 8 * 1024 * 1024:
            raise ValueError("飞书文档过大，请拆分后阅读")
        return content

    @staticmethod
    def _validate_node_id(node_id: Any) -> None:
        """只允许资源 token 进入飞书固定 API 路径。"""
        if not isinstance(node_id, str) or not re.fullmatch(r"[A-Za-z0-9]+", node_id):
            raise ValueError("飞书文档节点 ID 无效")

    @staticmethod
    async def _request(client: httpx.AsyncClient, connection: dict, method: str, path: str, **kwargs) -> dict:
        """校验 HTTP 和飞书业务状态，错误不暴露凭据或远端正文。"""
        try:
            async with client.stream(
                method,
                f"https://open.feishu.cn/open-apis{path}",
                headers={"Authorization": f"Bearer {connection['feishu_user_token']}"},
                **kwargs,
            ) as response:
                response.raise_for_status()
                payload_bytes = bytearray()
                async for part in response.aiter_bytes():
                    payload_bytes.extend(part)
                    if len(payload_bytes) > 10 * 1024 * 1024:
                        raise UpstreamError("飞书响应过大", code="feishu_request_failed")
        except httpx.HTTPStatusError as exc:
            raise UpstreamError(
                f"飞书请求失败（HTTP {exc.response.status_code}），请检查用户凭证、权限或限流状态",
                code="feishu_request_failed",
            ) from None
        except httpx.HTTPError:
            raise UpstreamError("飞书服务连接失败，请稍后重试", code="feishu_request_failed") from None
        try:
            import json

            payload = json.loads(payload_bytes)
        except ValueError:
            raise UpstreamError("飞书返回了无效 JSON", code="feishu_request_failed") from None
        if not isinstance(payload, dict) or type(payload.get("code")) is not int:
            raise UpstreamError("飞书响应缺少业务状态码", code="feishu_request_failed")
        if payload["code"] != 0:
            raise UpstreamError(
                f"飞书请求失败（code={payload['code']}），请检查用户 token 是否过期及知识库、文档读取权限",
                code="feishu_request_failed",
            )
        data = payload.get("data")
        if not isinstance(data, dict):
            raise UpstreamError("飞书响应缺少数据", code="feishu_request_failed")
        return data
