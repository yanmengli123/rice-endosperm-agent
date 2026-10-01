"""文献资源身份归一：DOI / RID / 目录名三键合一（ADR-0010）。

语义移植自 PaperAgent ``paperkb/doi.py`` 与 ``paperkb/layout.py``
（版本锚定 paperagent 1.4.0 / paperparse 2.3.0）。目标是**导入兼容**：
同一 DOI 在 Yuxi 与 PaperAgent 必须得到字节级一致的 RID/目录名，
否则 P3 导入器会把这些文献落到平行目录。

与上游的**全部**差异（均为超集增强，清洁输入行为逐字节一致）：
1. ``normalize_doi`` 额外剥离 ``doi.org`` URL 前缀与尾部句点——上游只在
   ``extract_doi_from_text`` 处做同类清洗，URL 形态在上游会被 ``is_doi``
   拒绝而降级为指纹身份；
2. **保留 DOI 大小写**（与上游一致，非差异）：上游为 WOS 导出保真刻意不做
   小写归一，小写会破坏导入兼容，禁止"顺手"加 casefold。

反向映射（``dirname_to_doi`` / md5 目录反查 / ``doi_md5_map``）随 P3
导入器一并移植，P0 不含。
"""

from __future__ import annotations

import hashlib
import re

from yuxi.literature.identity.kinds import KIND_PREFIX

# --- 移植自 paperkb/doi.py 的正则（保持逐字符一致） ---------------------------

#: DOI 形态唯一判定入口（拒绝"文件名当 DOI"）。
_DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$")

_INVALID_DIR_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
# 允许 "~"（":" 的转义占位）保留在目录名里
_NON_SLUG = re.compile(r"[^\w.~\-]")

# DOI 中"可安全进目录名"的字符集；含其他字符时加消歧后缀保证单射
_UNSAFE_FOR_DIRNAME = re.compile(r"[^A-Za-z0-9._/:\-]")

# Yuxi 超集：URL 形态的 DOI（上游 normalize 不处理，此处剥离后可被 is_doi 接受）
_DOI_URL_PREFIX = re.compile(r"^(?:https?://)?(?:dx\.)?doi\.org/", re.IGNORECASE)


def normalize_doi(raw: str | None) -> str:
    """规范化 DOI：strip + 去首尾空白与引号；空/None → ""。

    超集增强（上游 ``doi.py::normalize_doi`` 仅做 strip+去引号）：
    剥离 ``https://doi.org/`` 等 URL 前缀、去尾部句点。**保留大小写**
    （WOS 导出保真 + 导入兼容的 RID 字节级一致，见模块 docstring 第 2 条）。
    """
    if not raw:
        return ""
    value = raw.strip().strip("\"'")
    value = _DOI_URL_PREFIX.sub("", value)
    return value.rstrip(".")


def is_doi(value: str | None) -> bool:
    """是否为合法 DOI 形态（``10.NNNN/…``）。全库唯一判据，用于拒绝幽灵身份。"""
    return bool(_DOI_RE.match(normalize_doi(value) if value else (value or "").strip()))


def doi_to_dirname(doi: str) -> str:
    """DOI → 目录名（**单射**；移植自 paperkb/doi.py，逐规则一致）。

    规则：
      1) 含 "/" 或 ":" 时按可逆映射转义：字面 "_" → "__"、"/" → "_"、":" → "~"；
         **不含分隔符的值（``_global`` 等保留键、md5 目录名）原样保留**；
      2) 其余非法字符 → "-"；
      3) 原值含不安全字符（空格/括号等）→ 截断并追加 ``--<md5前6>`` 消歧后缀，
         保证不同 DOI 永不落到同一目录名（幂等、确定性）。
    """
    raw = normalize_doi(doi) or (doi or "").strip()
    if "/" in raw or ":" in raw:
        base = raw.replace("_", "__").replace("/", "_").replace(":", "~")
    else:
        base = raw
    base = _INVALID_DIR_CHARS.sub("-", base)
    base = _NON_SLUG.sub("-", base)
    if raw and _UNSAFE_FOR_DIRNAME.search(raw):
        base = base[:110] + "--" + hashlib.md5(raw.encode("utf-8")).hexdigest()[:6]
    return base[:120] or "paper"


def _slug(value: str) -> str:
    """外部标识 → 目录安全片段（移植自 paperkb/doi.py::_slug）。"""
    return re.sub(r"[^A-Za-z0-9._\-]+", "-", (value or "").strip()).strip("-")


def make_rid(
    kind: str = "paper",
    *,
    doi: str = "",
    isbn: str = "",
    cnki: str = "",
    cstr: str = "",
    arxiv: str = "",
    report_no: str = "",
    fingerprint: str = "",
    parent_rid: str = "",
) -> str:
    """生成统一资源键 RID（内部主键；外部标识只是别名）。

    优先级：DOI > ISBN > CNKI > CSTR > arXiv > 报告号 > 内容指纹。
    什么都没有时用内容指纹 ``nd-<指纹前12>``（同一文件重复导入 → 同一 RID，
    天然去重）。子资源：kind 命中 KIND_PREFIX 且给了 parent_rid →
    ``<前缀><父RID>``（例：``si__doi-10.1002_adma.202407106``）。
    返回值为目录安全字符串（可直接用作 MinIO 对象前缀/目录名）。
    """
    k = (kind or "paper").strip().lower()
    prefix = KIND_PREFIX.get(k, "")
    if prefix and parent_rid:
        return (prefix + parent_rid)[:150]
    body = ""
    d = normalize_doi(doi)
    if is_doi(d):
        body = "doi-" + doi_to_dirname(d)
    elif isbn:
        body = "isbn-" + _slug(isbn)
    elif cnki:
        body = "cnki-" + _slug(cnki)
    elif cstr:
        body = "cstr-" + _slug(cstr)
    elif arxiv:
        body = "arxiv-" + _slug(arxiv)
    elif report_no:
        body = "rep-" + _slug(report_no)
    elif fingerprint:
        body = "nd-" + _slug(fingerprint).lower()[:12]
    else:
        body = "nd-untitled"
    return (prefix + body)[:150].strip("-_") or "nd-untitled"


def kind_from_rid(rid: str) -> str:
    """由 RID 前缀推导资源类型（**最长前缀优先**）。

    移植自 paperkb/layout.py::kind_from_rid。无类型前缀时返回 "paper"
    （``doi-…`` 与无标识的 ``nd-…`` 都按论文处理）。前缀唯一来源是
    :data:`yuxi.literature.identity.kinds.KIND_PREFIX`——注意 ``standard``
    的前缀是 ``std__`` 而非 ``standard__``。
    """
    r = (rid or "").strip()
    for kind, prefix in sorted(KIND_PREFIX.items(), key=lambda kv: -len(kv[1])):
        if prefix and r.startswith(prefix):
            return kind
    return "paper"


def library_candidates(rid: str, doi: str = "") -> tuple[str, ...]:
    """RID / 裸 DOI / 目录名三种键写法 → 统一的候选**目录名**元组（顺序即优先级）。

    语义移植自 paperkb/attachments.py::library_candidates（Yuxi 适配：纯函数、
    返回目录名字符串而非 Path，由调用方拼 MinIO 前缀/本地根）。三种写法必须
    命中同一目录，否则依附资料会落到平行目录（用户两处都找不到——上游教训）。
    """
    r = (rid or "").strip()
    cands: list[str] = []
    if r:
        cands.append(r)
        if r.startswith("doi-"):
            rest = r[4:]
            cands.append(rest)  # 已按目录名书写的历史 RID
            cands.append(doi_to_dirname(rest))  # 规范 DOI → 目录名
        elif is_doi(r):
            cands.append(doi_to_dirname(r))  # 裸 DOI（含 /）→ 目录名
    d = normalize_doi(doi)
    if d and is_doi(d):
        cands.append(doi_to_dirname(d))  # 显式 DOI 入参 → 目录名
    out: list[str] = []
    seen: set[str] = set()
    for c in cands:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return tuple(out)
