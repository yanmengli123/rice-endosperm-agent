# SPDX-License-Identifier: AGPL-3.0-or-later
"""One bounded native translation process; secrets arrive only through stdin."""

import hashlib
import json
import os
import sys
from pathlib import Path

import fitz

from app.services.layout_fix import fix_translated_layout
from pdf_zh_translator.ocr import is_scanned_pdf, ocr_pdf_to_searchable
from pdf_zh_translator.pdf_layout import create_dual_pdf, translate_pdf, verify_translation_issues
from pdf_zh_translator.translators import CachedTranslator, VendorTranslator
from scientific_qa import critical_issues


class IdempotentVendor(VendorTranslator):
    def _post_json(self, url, payload):
        payload = dict(payload)
        payload["messages"] = [dict(message) for message in payload["messages"]]
        payload["messages"][0]["content"] += "\nPreserve gene symbols (including Os-prefixed symbols), locus identifiers, sample sizes, units, numerical results, and all qualifications exactly. Never strengthen a scientific claim."
        payload["request_id"] = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        return super()._post_json(url, payload)


class ProgressCache(CachedTranslator):
    """Report actual completed blocks without guessing a document-wide percentage."""

    def __init__(self, vendor, home):
        super().__init__(vendor, home / "translation-cache.jsonl")
        self.home, self.completed = home, 0
        self.critical_issues = []

    def translate_batch(self, texts):
        texts, output, types = list(texts), [], self.block_types
        try:
            for offset in range(0, len(texts), 8):
                if types:
                    self.block_types = types[offset:offset + 8]
                result = super().translate_batch(texts[offset:offset + 8])
                for original, translation in zip(texts[offset:offset + 8], result, strict=True):
                    self.critical_issues.extend(critical_issues(original, translation, self.completed + 1))
                    self.completed += 1
                output.extend(result)
                progress(self.home, f"翻译与排版：已处理 {self.completed} 个文本块", 0.1)
        finally:
            self.block_types = types
        return output


def progress(home, stage, value):
    temp = home / "progress.tmp"
    temp.write_text(json.dumps({"stage": stage, "progress": value}))
    temp.replace(home / "progress.json")


def issue_score(issues):
    return sum(i.severity == "error" for i in issues), len(issues)


def execute(home, token):
    job = json.loads((home / "job.json").read_text())
    options = job["options"]
    source, translated = home / "source.pdf", home / "translated.pdf"
    with fitz.open(source) as document:
        count = document.page_count
        geometry = [[*p.mediabox, p.rotation] for p in document]
    if not 1 <= count <= int(options["page_limit"]):
        raise ValueError("PDF_PAGE_LIMIT")
    input_pdf = source
    if options["ocr_mode"] == "auto" and is_scanned_pdf(source):
        input_pdf = home / "searchable.pdf"
        ocr_pdf_to_searchable(source, input_pdf, language="eng", tessdata="/usr/share/tesseract-ocr/5/tessdata")
    vendor = IdempotentVendor(
        api_url=os.environ["PDF_GATEWAY_URL"], api_key=token, mode="openai-compatible",
        model=options["model_spec"], progress=False, timeout=600, retries=1,
    )
    translator = ProgressCache(vendor, home)
    progress(home, "翻译与原位排版", 0.1)
    report = translate_pdf(input_pdf, translated, translator, preserve_graphics_text=True, skip_overflow=False)
    if not report.translated_blocks:
        raise ValueError("PDF_NO_TRANSLATABLE_TEXT")
    progress(home, "检查公式、图表与页面", 0.8)
    issues = verify_translation_issues(input_pdf, translated)
    history = [{"pass": 1, "errors": issue_score(issues)[0], "issues": len(issues)}]
    if issues and options.get("qa_mode") == "iterative":
        for iteration in range(2, min(4, int(options.get("qa_max_passes", 2))) + 1):
            snapshot = translated.read_bytes()
            changed = fix_translated_layout(translated)
            candidate = verify_translation_issues(input_pdf, translated) if changed else issues
            improved = changed and issue_score(candidate) < issue_score(issues)
            history.append({"pass": iteration, "accepted": improved, "errors": issue_score(candidate)[0], "issues": len(candidate)})
            if not improved:
                translated.write_bytes(snapshot)
                break
            issues = candidate
            if not issues:
                break
    with fitz.open(translated) as document:
        if document.page_count != count or [[*p.mediabox, p.rotation] for p in document] != geometry:
            raise ValueError("PDF_GEOMETRY_CHANGED")
    critical = translator.critical_issues
    # The native engine protects formulas with placeholders before model calls.
    # Check the final PDF too, after restoration and font/layout processing.
    with fitz.open(input_pdf) as original, fitz.open(translated) as rendered:
        for index in range(count):
            page_issues = critical_issues(original[index].get_text(), rendered[index].get_text(), 0)
            critical.extend({**issue, "page": index + 1} for issue in page_issues)
    qa = {
        "status": "passed" if not issues and not critical else "failed", "page_count": count,
        "checks": {"layout": "passed" if not issues else "failed", "critical_tokens": "passed" if not critical else "failed", "scientific_semantics": "requires_human_review"},
        "error_count": sum(i.severity == "error" for i in issues) + len(critical), "warning_count": sum(i.severity != "error" for i in issues),
        "engine_warning_count": len(report.warnings),
        "pass_history": history, "warnings": report.warnings,
        "issues": [{"page": i.page, "code": i.code, "severity": i.severity, "message": i.message} for i in issues] + critical,
    }
    (home / "qa.json").write_text(json.dumps(qa, ensure_ascii=False))
    if issues or critical:
        return {"status": "failed", "stage": "质量检查未通过", "error_code": "PDF_QA_FAILED", "error": "译后检查发现问题，候选译文未发布", "qa_available": True, "page_count": count}
    progress(home, "生成双语 PDF", 0.95)
    create_dual_pdf(source, translated, home / "dual.pdf")
    manifest = {
        "schema_version": 1, "input_kind": "original_pdf", "source_sha256": job["source_sha256"],
        "page_count": count, "geometry": geometry, "options": options,
        "page_map": [{"original": i, "translated": i, "dual_original": 2 * i - 1, "dual_translated": 2 * i} for i in range(1, count + 1)],
        "outputs": {name: {"sha256": hashlib.sha256((home / filename).read_bytes()).hexdigest(), "size": (home / filename).stat().st_size} for name, filename in (("pdf_zh", "translated.pdf"), ("pdf_dual", "dual.pdf"), ("qa", "qa.json"))},
    }
    (home / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False))
    return {"status": "completed", "stage": "已完成", "progress": 1, "page_count": count, "qa_available": True}


if __name__ == "__main__":
    home = Path(sys.argv[1])
    try:
        result = execute(home, sys.stdin.read())
    except Exception as exc:
        # Provider credentials and document bodies must not enter process logs.
        code = str(exc) if isinstance(exc, ValueError) and str(exc).startswith("PDF_") else type(exc).__name__
        result = {"status": "failed", "stage": "执行失败", "error_code": code, "error": "PDF 引擎执行失败；请核对模型任务、输入与质量报告"}
    (home / "result.json").write_text(json.dumps(result, ensure_ascii=False))
