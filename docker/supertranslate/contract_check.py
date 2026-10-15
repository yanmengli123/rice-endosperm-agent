# SPDX-License-Identifier: AGPL-3.0-or-later
"""上游引擎契约冒烟检查 + 源版本锁定。

engine.py 依赖上游 8 个符号（app.services.layout_fix / pdf_zh_translator.*）。
上游重构导致的名字消失或签名漂移，如果只在首个翻译任务运行时才暴露，
就是一次线上翻车；本脚本让它在**镜像构建期**与 **bridge 启动期**即失败。

三种模式：
- 默认：契约检查（符号导入 / engine 调用面签名兼容 / 返回类型字段 / rice.json）
- ``--lock-source <dir> --expect <lock> [--allow-unpinned 1]``：对源目录算内容
  摘要并与锁文件比对（上游非 git 仓库，锁定只能基于内容摘要）
- ``--write-lock <dir> <lock>``：生成锁文件（升级上游后的登记动作）

摘要口径与 Dockerfile 的 COPY 面一致（pdf_zh_translator/ + app/ + 根部四个
元文件），排除 __pycache__/*.pyc，宿主机与镜像内计算结果逐字节一致。
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import inspect
import json
import sys
from dataclasses import fields, is_dataclass
from pathlib import Path
from typing import Any, get_type_hints

#: 构建实际消费的上游路径（相对源根），与 Dockerfile COPY 保持一致。
SOURCE_ROOTS = ("pdf_zh_translator", "app", "LICENSE", "README.md", "pyproject.toml", "uv.lock")

#: Yuxi 注入到上游树的自有文件（Dockerfile COPY 进 corpora/）：不属于"上游源"，
#: 其完整性由 corpus_digest 与契约检查的 rice.json 项覆盖，不进源摘要。
INJECTED_UPSTREAM_FILES = {"pdf_zh_translator/corpora/rice.json"}

_FINGERPRINT_PATH = Path("/opt/source/SOURCE.fingerprint")

ENGINE_CALLSITES: tuple[tuple[str, dict[str, Any]], ...] = (
    ("fix_translated_layout", {"args": ("path",)}),
    ("is_scanned_pdf", {"args": ("path",)}),
    (
        "ocr_pdf_to_searchable",
        {"args": ("input", "output"), "kwargs": {"language": "eng", "tessdata": "/tessdata"}},
    ),
    (
        "translate_pdf",
        {
            "args": ("input", "output", "translator"),
            "kwargs": {"preserve_graphics_text": True, "skip_overflow": False},
        },
    ),
    ("verify_translation_issues", {"args": ("original", "translated")}),
    ("create_dual_pdf", {"args": ("original", "translated", "output")}),
)


def _record(results: list[dict[str, Any]], name: str, ok: bool, detail: str = "") -> bool:
    results.append({"name": name, "ok": bool(ok), "detail": detail})
    return bool(ok)


def _accepts(
    func: Any, *, args: tuple[str, ...], kwargs: dict[str, Any] | None = None
) -> tuple[bool, str]:
    """engine.py 的调用面能否被当前签名接受（bind_partial：允许额外可选参数）。"""
    try:
        inspect.signature(func).bind_partial(*args, **(kwargs or {}))
        return True, ""
    except TypeError as exc:
        return False, str(exc)


def _return_dataclass(func: Any) -> type | None:
    hints = get_type_hints(func)
    annotation = hints.get("return")
    if isinstance(annotation, type) and is_dataclass(annotation):
        return annotation
    origin = getattr(annotation, "__origin__", None)  # List[TranslationIssue] → item
    if origin is list:
        args = getattr(annotation, "__args__", ())
        if args and isinstance(args[0], type) and is_dataclass(args[0]):
            return args[0]
    return None


def verify() -> dict[str, Any]:
    """契约全量检查：导入面 → 签名面 → 返回类型面 → 术语表面。"""
    results: list[dict[str, Any]] = []

    def symbol(module_name: str, attr: str):
        ok, value = True, None
        try:
            value = getattr(importlib.import_module(module_name), attr)
        except Exception as exc:  # noqa: BLE001 - 任何导入失败都按契约断裂报告
            ok, value = False, f"{type(exc).__name__}: {exc}"
        _record(results, f"import {module_name}.{attr}", ok, "" if ok else str(value))
        return value if ok else None

    layout_fix = symbol("app.services.layout_fix", "fix_translated_layout")
    is_scanned = symbol("pdf_zh_translator.ocr", "is_scanned_pdf")
    ocr_searchable = symbol("pdf_zh_translator.ocr", "ocr_pdf_to_searchable")
    translate_pdf = symbol("pdf_zh_translator.pdf_layout", "translate_pdf")
    verify_issues = symbol("pdf_zh_translator.pdf_layout", "verify_translation_issues")
    create_dual = symbol("pdf_zh_translator.pdf_layout", "create_dual_pdf")
    vendor_cls = symbol("pdf_zh_translator.translators", "VendorTranslator")
    cached_cls = symbol("pdf_zh_translator.translators", "CachedTranslator")

    # engine.py 的 6 个函数调用面
    for func, (name, call_spec) in zip(
        (layout_fix, is_scanned, ocr_searchable, translate_pdf, verify_issues, create_dual),
        ENGINE_CALLSITES,
    ):
        if func is None:
            continue  # 导入已失败，签名检查跳过（已计失败）
        ok, detail = _accepts(func, args=call_spec["args"], kwargs=call_spec.get("kwargs"))
        _record(results, f"call {name}{call_spec['args']}", ok, detail)

    # 返回类型字段：engine 访问 report.translated_blocks/.warnings 与 issue 四字段
    if (report_cls := _return_dataclass(translate_pdf) if translate_pdf else None) is not None:
        names = {f.name for f in fields(report_cls)}
        _record(
            results,
            "TranslationReport.fields ⊇ {translated_blocks, warnings}",
            {"translated_blocks", "warnings"} <= names,
        )
    else:
        _record(
            results,
            "TranslationReport annotated dataclass return",
            False,
            "translate_pdf 返回注解缺失或非 dataclass",
        )
    if (issue_cls := _return_dataclass(verify_issues) if verify_issues else None) is not None:
        names = {f.name for f in fields(issue_cls)}
        _record(
            results,
            "TranslationIssue.fields ⊇ {page, code, severity, message}",
            {"page", "code", "severity", "message"} <= names,
        )
    else:
        _record(
            results,
            "TranslationIssue annotated dataclass return",
            False,
            "verify_translation_issues 返回注解缺失或非 dataclass",
        )

    # VendorTranslator：engine 以 7 个 kwargs 构造子类实例
    if vendor_cls is not None:
        ok, detail = _accepts(
            vendor_cls,
            args=(),
            kwargs={
                "api_url": "http://gateway",
                "api_key": "t",
                "mode": "openai-compatible",
                "model": "m",
                "progress": False,
                "timeout": 600,
                "retries": 1,
            },
        )
        _record(results, "call VendorTranslator(api_url=…, retries=…)", ok, detail)
        _record(
            results,
            "VendorTranslator._post_json(url, payload)",
            callable(getattr(vendor_cls, "_post_json", None)),
        )

    # CachedTranslator：双位置构造 + translate_batch + block_types 属性（ProgressCache 依赖）
    if cached_cls is not None:
        ok, detail = _accepts(cached_cls, args=("vendor", "cache_file"))
        _record(results, "call CachedTranslator(vendor, cache_file)", ok, detail)
        _record(
            results,
            "CachedTranslator.translate_batch(texts)",
            callable(getattr(cached_cls, "translate_batch", None)),
        )
        _record(results, "CachedTranslator.block_types", hasattr(cached_cls, "block_types"))

    # rice.json：领域术语表必须在 corpora/ 内且结构可解析（corpus_digest 已覆盖其内容）
    corpora = Path(importlib.import_module("pdf_zh_translator.corpus").__file__).parent / "corpora"
    rice = corpora / "rice.json"
    try:
        data = json.loads(rice.read_text("utf-8"))
        pairs = [
            pair
            for category, terms in data.items()
            if category != "_metadata" and isinstance(terms, dict)
            for pair in terms.items()
        ]
        ok = bool(pairs) and all(
            isinstance(en, str) and isinstance(zh, str) and en.strip() and zh.strip()
            for en, zh in pairs
        )
        _record(results, f"corpora/rice.json ({len(pairs)} terms)", ok)
    except Exception as exc:  # noqa: BLE001
        _record(results, "corpora/rice.json", False, f"{type(exc).__name__}: {exc}")

    return {"ok": all(item["ok"] for item in results), "checks": results}


# ---------------------------------------------------------------- 源摘要锁定


def source_digest(root: Path, *, verbose: bool = False) -> str:
    """对构建消费的源子集算确定性摘要（宿主机与镜像内逐字节一致）。"""
    digest = hashlib.sha256()
    components: dict[str, str] = {}
    for name in SOURCE_ROOTS:
        digest.update(name.encode())
        path = root / name
        if path.is_file():
            digest.update(hashlib.sha256(path.read_bytes()).digest())
            components[name] = "file"
            continue
        part = hashlib.sha256()
        for file in sorted(
            p
            for p in path.rglob("*")
            if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
        ):
            relative = str(file.relative_to(root)).replace("\\", "/")
            if relative in INJECTED_UPSTREAM_FILES:
                continue
            part.update(relative.encode())
            part.update(hashlib.sha256(file.read_bytes()).digest())
        components[name] = part.hexdigest()[:16]
        digest.update(part.digest())
    if verbose:
        print(json.dumps({"roots": components}, ensure_ascii=False), file=sys.stderr)
    return digest.hexdigest()


def lock_source(root: Path, expect: Path, allow_unpinned: bool) -> int:
    actual = source_digest(root, verbose=True)
    locked = json.loads(expect.read_text("utf-8")) if expect.is_file() else None
    pinned = bool(locked and locked.get("source_digest") == actual)
    if not pinned and not allow_unpinned:
        print(
            f"SOURCE LOCK MISMATCH: actual={actual} "
            f"locked={locked and locked.get('source_digest')}",
            file=sys.stderr,
        )
        print(
            "升级上游后请重新生成锁：python contract_check.py --write-lock <dir> <lock>",
            file=sys.stderr,
        )
        return 1
    try:
        _FINGERPRINT_PATH.write_text(
            json.dumps(
                {
                    "source_digest": actual,
                    "pinned": pinned,
                    "locked_digest": locked and locked.get("source_digest"),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except OSError:
        pass  # 非 root/开发机运行时无 /opt/source 写权限；/contract 端点会回落到锁文件内容
    print(json.dumps({"source_digest": actual, "pinned": pinned}))
    return 0


def write_lock(root: Path, lock: Path) -> int:
    digest = source_digest(root)
    lock.write_text(
        json.dumps(
            {"source_digest": digest, "roots": list(SOURCE_ROOTS)}, ensure_ascii=False, indent=2
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"locked {digest} -> {lock}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="契约结果以 JSON 输出")
    parser.add_argument("--lock-source", type=Path, help="源目录（校验摘要并写指纹）")
    parser.add_argument("--expect", type=Path, help="锁文件路径")
    parser.add_argument("--allow-unpinned", choices=("0", "1"), default="0")
    parser.add_argument("--write-lock", nargs=2, type=Path, metavar=("DIR", "LOCK"))
    args = parser.parse_args(argv)
    if args.write_lock:
        return write_lock(*args.write_lock)
    if args.lock_source:
        return lock_source(
            args.lock_source, args.expect or Path("SOURCE.lock"), args.allow_unpinned == "1"
        )
    report = verify()
    print(
        json.dumps(report, ensure_ascii=False, indent=2)
        if args.json
        else "\n".join(
            f"{'PASS' if item['ok'] else 'FAIL'}  {item['name']}"
            + (f"  — {item['detail']}" if item["detail"] else "")
            for item in report["checks"]
        )
    )
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
