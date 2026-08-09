"""使用 MinerU 官方精准解析 API 处理文档。"""

from __future__ import annotations

import os
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any

import requests

from yuxi.knowledge.parser.base import BaseDocumentProcessor, DocumentParserException
from yuxi.knowledge.parser.credential_cache import ocr_credential_cache
from yuxi.knowledge.parser.zip_utils import process_zip_file_sync
from yuxi.utils import hashstr, logger

_SERVICE_ID = "mineru_official"
_DEFAULT_API_BASE = "https://mineru.net/api/v4"
_AUTH_PROBE_TASK_ID = "00000000-0000-4000-8000-000000000000"
_VALID_MODEL_VERSIONS = {"pipeline", "vlm"}
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
_REQUEST_MAX_ATTEMPTS = 3


class MinerUOfficialParser(BaseDocumentProcessor):
    """MinerU 官方云服务解析器。"""

    def __init__(
        self,
        api_token: str | None = None,
        model_version: str | None = None,
        api_base: str | None = None,
        api_key: str | None = None,
    ):
        # api_key 仅保留给已有内部调用兼容；全局运行时只读取设置页保存的凭证缓存。
        self._api_token_override = api_token or api_key
        self._model_version_override = model_version
        self._api_base_override = api_base

    def get_service_name(self) -> str:
        return _SERVICE_ID

    def get_supported_extensions(self) -> list[str]:
        return [
            ".pdf",
            ".doc",
            ".docx",
            ".ppt",
            ".pptx",
            ".xls",
            ".xlsx",
            ".png",
            ".jpg",
            ".jpeg",
            ".jp2",
            ".webp",
            ".gif",
            ".bmp",
        ]

    def check_health(self) -> dict[str, Any]:
        """验证 Token，不创建解析任务或消耗文档解析额度。"""
        try:
            api_token, api_base, _model_version = self._runtime_config()
            response = requests.get(
                f"{api_base}/extract/task/{_AUTH_PROBE_TASK_ID}",
                headers=self._headers(api_token),
                timeout=10,
            )
            payload = response.json()

            if response.status_code in {401, 403} or payload.get("msgCode") in {"A0202", "A0211"}:
                return {
                    "status": "unhealthy",
                    "message": "MinerU API Token 无效、已过期或权限不足",
                    "details": {"error_code": payload.get("msgCode"), "status_code": response.status_code},
                }

            message = str(payload.get("msg") or "")
            if response.status_code == 200 and (
                payload.get("code") in {0, -60012} or "task not found" in message.lower()
            ):
                return {
                    "status": "healthy",
                    "message": "MinerU 官方 API 连接正常",
                    "details": {"api_base": api_base, "probe": "read_only_task_lookup"},
                }

            return {
                "status": "unhealthy",
                "message": f"MinerU 官方 API 返回异常: {message or f'HTTP {response.status_code}'}",
                "details": {"error_code": payload.get("code"), "status_code": response.status_code},
            }
        except DocumentParserException as exc:
            return {"status": "unhealthy", "message": str(exc), "details": {"error_code": exc.status_code}}
        except requests.exceptions.Timeout:
            return {"status": "timeout", "message": "MinerU 官方 API 请求超时", "details": {"timeout": "10s"}}
        except requests.exceptions.ConnectionError:
            return {
                "status": "unavailable",
                "message": "无法连接到 MinerU 官方 API",
                "details": {"api_base": self._api_base_override or _DEFAULT_API_BASE},
            }
        except (ValueError, requests.exceptions.JSONDecodeError):
            return {"status": "unhealthy", "message": "MinerU 官方 API 返回了无效响应", "details": {}}
        except Exception as exc:
            return {"status": "error", "message": f"MinerU 连接测试失败: {exc}", "details": {}}

    def process_file(self, file_path: str, params: dict[str, Any] | None = None) -> str:
        """上传本地文档、轮询任务，并返回 MinerU 生成的 Markdown。"""
        if not os.path.exists(file_path):
            raise DocumentParserException(f"文件不存在: {file_path}", self.get_service_name(), "file_not_found")

        file_ext = Path(file_path).suffix.lower()
        if not self.supports_file_type(file_ext):
            raise DocumentParserException(
                f"不支持的文件类型: {file_ext}", self.get_service_name(), "unsupported_file_type"
            )

        params = params or {}
        start_time = time.time()
        try:
            logger.info(f"MinerU Official 开始处理: {os.path.basename(file_path)}")
            batch_id = self._upload_file(file_path, params)
            result = self._poll_batch_result(batch_id)
            zip_url = result.get("full_zip_url")

            try:
                zip_path = self._download_zip(zip_url)
            except Exception:
                text = self._download_and_extract(zip_url)
                logger.info(
                    f"MinerU Official 处理成功: {os.path.basename(file_path)} - "
                    f"{len(text)} 字符 ({time.time() - start_time:.2f}s)"
                )
                return text

            try:
                processed = process_zip_file_sync(
                    zip_path,
                    image_bucket=params.get("image_bucket") or "public",
                    image_prefix=params.get("image_prefix") or "unknown/kb-images",
                )
                text = processed["markdown_content"]
            except Exception:
                logger.exception(f"处理 MinerU 结果包失败，改用结果包中的 Markdown: {zip_path}")
                text = self._read_markdown_from_zip(zip_path)
            finally:
                try:
                    os.unlink(zip_path)
                except OSError:
                    pass

            logger.info(
                f"MinerU Official 处理成功: {os.path.basename(file_path)} - "
                f"{len(text)} 字符 ({time.time() - start_time:.2f}s)"
            )
            return text
        except DocumentParserException:
            raise
        except Exception as exc:
            logger.error(f"MinerU Official 处理失败: {exc} ({time.time() - start_time:.2f}s)")
            raise DocumentParserException(
                f"MinerU Official 处理失败: {exc}", self.get_service_name(), "processing_failed"
            ) from exc

    def _runtime_config(self) -> tuple[str, str, str]:
        runtime = None if self._api_token_override else ocr_credential_cache.get(_SERVICE_ID)
        api_token = self._api_token_override or (runtime.api_token if runtime else "")
        if not api_token:
            raise DocumentParserException(
                "尚未配置 MinerU API Token，请由超级管理员在设置页面完成配置",
                self.get_service_name(),
                "missing_api_token",
            )

        api_base = self._api_base_override or (runtime.api_base if runtime else _DEFAULT_API_BASE)
        model_version = self._model_version_override or (
            str(runtime.settings.get("model_version") or "vlm") if runtime else "vlm"
        )
        if model_version not in _VALID_MODEL_VERSIONS:
            raise DocumentParserException(
                f"不支持的 MinerU model_version: {model_version}", self.get_service_name(), "invalid_model_version"
            )
        return api_token, api_base.rstrip("/"), model_version

    @staticmethod
    def _headers(api_token: str) -> dict[str, str]:
        return {"Content-Type": "application/json", "Authorization": f"Bearer {api_token}"}

    @staticmethod
    def _request_with_retry(request_callable, *args, rewind=None, **kwargs):
        """Retry transient MinerU/network failures with a short bounded backoff."""
        last_error = None
        for attempt in range(1, _REQUEST_MAX_ATTEMPTS + 1):
            if attempt > 1 and rewind is not None:
                rewind.seek(0)
            try:
                response = request_callable(*args, **kwargs)
                if response.status_code not in _RETRYABLE_STATUS_CODES or attempt == _REQUEST_MAX_ATTEMPTS:
                    return response
                last_error = RuntimeError(f"HTTP {response.status_code}")
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
                last_error = exc
                if attempt == _REQUEST_MAX_ATTEMPTS:
                    raise

            delay = min(2 ** (attempt - 1), 4)
            logger.warning(
                f"MinerU Official 请求暂时失败，{delay} 秒后重试 ({attempt}/{_REQUEST_MAX_ATTEMPTS}): {last_error}"
            )
            time.sleep(delay)

        raise RuntimeError(f"MinerU Official 请求失败: {last_error}")

    def _upload_file(self, file_path: str, params: dict[str, Any]) -> str:
        api_token, api_base, configured_model_version = self._runtime_config()
        model_version = str(params.get("model_version") or configured_model_version)
        if model_version not in _VALID_MODEL_VERSIONS:
            raise DocumentParserException(
                f"不支持的 MinerU model_version: {model_version}", self.get_service_name(), "invalid_model_version"
            )

        filename = os.path.basename(file_path)
        data_id = str(params.get("data_id") or filename)
        if len(data_id) > 128:
            data_id = f"{data_id[:118]}_{hashstr(data_id, length=8)}"

        file_item: dict[str, Any] = {
            "name": filename,
            "is_ocr": params.get("is_ocr", True),
            "data_id": data_id,
        }
        if params.get("page_ranges"):
            file_item["page_ranges"] = params["page_ranges"]

        upload_data = {
            "enable_formula": params.get("enable_formula", True),
            "enable_table": params.get("enable_table", True),
            "language": params.get("language", "ch"),
            "model_version": model_version,
            "files": [file_item],
        }
        response = self._request_with_retry(
            requests.post,
            f"{api_base}/file-urls/batch",
            headers=self._headers(api_token),
            json=upload_data,
            timeout=30,
        )
        if response.status_code != 200:
            raise DocumentParserException(
                f"申请 MinerU 上传链接失败: HTTP {response.status_code}",
                self.get_service_name(),
                "upload_url_failed",
            )

        result = response.json()
        if result.get("code") != 0:
            message = result.get("msg") or "未知错误"
            raise DocumentParserException(
                f"申请 MinerU 上传链接失败: {message}",
                self.get_service_name(),
                f"api_error_{result.get('code', 'unknown')}",
            )

        upload_urls = result.get("data", {}).get("file_urls") or []
        if not upload_urls:
            raise DocumentParserException("MinerU 未返回文件上传链接", self.get_service_name(), "no_upload_url")

        with open(file_path, "rb") as source:
            upload_response = self._request_with_retry(
                requests.put,
                upload_urls[0],
                data=source,
                timeout=60,
                rewind=source,
            )
        if upload_response.status_code != 200:
            raise DocumentParserException(
                f"上传文件到 MinerU 失败: HTTP {upload_response.status_code}",
                self.get_service_name(),
                "file_upload_failed",
            )
        return result["data"]["batch_id"]

    def _poll_batch_result(self, batch_id: str, max_wait_time: int = 600) -> dict[str, Any]:
        start_time = time.time()
        while time.time() - start_time < max_wait_time:
            api_token, api_base, _model_version = self._runtime_config()
            response = self._request_with_retry(
                requests.get,
                f"{api_base}/extract-results/batch/{batch_id}",
                headers=self._headers(api_token),
                timeout=30,
            )
            if response.status_code != 200:
                raise DocumentParserException(
                    f"查询 MinerU 任务失败: HTTP {response.status_code}",
                    self.get_service_name(),
                    "status_query_failed",
                )

            result = response.json()
            if result.get("code") != 0:
                message = result.get("msg") or "未知错误"
                raise DocumentParserException(
                    f"查询 MinerU 任务失败: {message}",
                    self.get_service_name(),
                    f"api_error_{result.get('code', 'unknown')}",
                )

            extract_results = result.get("data", {}).get("extract_result") or []
            if not extract_results:
                time.sleep(5)
                continue

            file_result = extract_results[0]
            state = file_result.get("state")
            if state == "done":
                return file_result
            if state == "failed":
                raise DocumentParserException(
                    f"MinerU 文档解析失败: {file_result.get('err_msg') or '未知错误'}",
                    self.get_service_name(),
                    "parsing_failed",
                )
            time.sleep(5)

        raise DocumentParserException("MinerU 任务处理超时", self.get_service_name(), "timeout")

    def _download_and_extract(self, zip_url: str | None) -> str:
        zip_path = self._download_zip(zip_url)
        try:
            return self._read_markdown_from_zip(zip_path)
        finally:
            try:
                os.unlink(zip_path)
            except OSError:
                pass

    def _download_zip(self, zip_url: str | None) -> str:
        if not zip_url:
            raise DocumentParserException("MinerU 未返回结果下载链接", self.get_service_name(), "no_download_url")
        response = self._request_with_retry(requests.get, zip_url, timeout=60)
        if response.status_code != 200:
            raise DocumentParserException(
                f"下载 MinerU 结果失败: HTTP {response.status_code}", self.get_service_name(), "download_failed"
            )
        with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as temp_file:
            temp_file.write(response.content)
            return temp_file.name

    def _read_markdown_from_zip(self, zip_path: str) -> str:
        with zipfile.ZipFile(zip_path, "r") as archive:
            markdown_files = [name for name in archive.namelist() if name.lower().endswith(".md")]
            if not markdown_files:
                raise DocumentParserException(
                    "MinerU 结果包中没有 Markdown 文件", self.get_service_name(), "extract_content_failed"
                )
            markdown_name = next((name for name in markdown_files if Path(name).name == "full.md"), markdown_files[0])
            with archive.open(markdown_name) as markdown_file:
                return markdown_file.read().decode("utf-8")
