"""页面结构化提取与受预算约束的分页收集。

本模块只读取浏览器当前页面，不修改 DOM、滚动位置或焦点。分页时由会话层
执行已经受控的导航或原生按钮动作；提取器只决定是否存在足够明确的下一页。
"""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass
from time import monotonic
from typing import Any
from urllib.parse import urldefrag, urlsplit, urlunsplit


_DEFAULT_MAX_PAGES = 5
_DEFAULT_MAX_ITEMS = 200
_DEFAULT_MAX_TEXT_CHARS = 100_000
_DEFAULT_TIMEOUT_MS = 30_000
_MAX_TABLE_ROWS = 20
_MAX_RESULT_ITEMS = 1_000
_MAX_QUERY_CHARS = 2_000
_MAX_TABLE_COLUMNS = 10
_MAX_TABLE_TEXT_CHARS = 512
_MAX_FORM_FIELDS = 20
_MAX_FIELD_VALUE_CHARS = 512
_MAX_META_ITEMS = 100
_MAX_META_TEXT_CHARS = 2_000
_MAX_JSON_LD_ITEMS = 20
_MAX_JSON_LD_CHARS = 20_000
_MAX_BROWSE_PAGES = 50
_MAX_BROWSE_TEXT_CHARS = 1_000_000
_MAX_BROWSE_TIMEOUT_MS = 300_000


class ExtractionError(Exception):
    """提取参数或只读页面访问失败的稳定错误。"""

    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.message = message


@dataclass(frozen=True)
class BrowseBudget:
    """一次自动分页允许访问和返回内容的明确上限。"""

    max_pages: int = _DEFAULT_MAX_PAGES
    max_items: int = _DEFAULT_MAX_ITEMS
    max_text_chars: int = _DEFAULT_MAX_TEXT_CHARS
    timeout_ms: int = _DEFAULT_TIMEOUT_MS
    max_visited_urls: int = _DEFAULT_MAX_PAGES

    @classmethod
    def create(
        cls,
        *,
        max_pages: int = _DEFAULT_MAX_PAGES,
        max_items: int = _DEFAULT_MAX_ITEMS,
        max_text_chars: int = _DEFAULT_MAX_TEXT_CHARS,
        timeout_ms: int | None = None,
    ) -> "BrowseBudget":
        values = {
            "max_pages": max_pages,
            "max_items": max_items,
            "max_text_chars": max_text_chars,
            "timeout_ms": _DEFAULT_TIMEOUT_MS if timeout_ms is None else timeout_ms,
        }
        maxima = {
            "max_pages": _MAX_BROWSE_PAGES,
            "max_items": _MAX_RESULT_ITEMS,
            "max_text_chars": _MAX_BROWSE_TEXT_CHARS,
            "timeout_ms": _MAX_BROWSE_TIMEOUT_MS,
        }
        for name, value in values.items():
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ExtractionError("invalid_args", f"{name} 必须是正整数")
            if value > maxima[name]:
                raise ExtractionError(
                    "invalid_args", f"{name} 不能大于 {maxima[name]}"
                )
        return cls(
            max_pages=values["max_pages"],
            max_items=values["max_items"],
            max_text_chars=values["max_text_chars"],
            timeout_ms=values["timeout_ms"],
            max_visited_urls=values["max_pages"],
        )

    def public_payload(self) -> dict[str, int]:
        return asdict(self)


def _normalize_url(url: str) -> str:
    """删除 fragment 并规范化协议和主机大小写，供分页去重使用。"""
    try:
        without_fragment, _ = urldefrag(url)
        parts = urlsplit(without_fragment)
    except (TypeError, ValueError):
        return url
    if not parts.scheme or not parts.netloc:
        return without_fragment
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", parts.query, "")
    )


def _same_origin(first: str, second: str) -> bool:
    """比较协议、主机和端口，不因路径或 fragment 误判跨站。"""
    try:
        first_parts = urlsplit(first)
        second_parts = urlsplit(second)
        return (
            first_parts.scheme.lower(),
            first_parts.hostname.lower() if first_parts.hostname else None,
            first_parts.port,
        ) == (
            second_parts.scheme.lower(),
            second_parts.hostname.lower() if second_parts.hostname else None,
            second_parts.port,
        )
    except ValueError:
        return False


class PageExtractor:
    """从当前 BrowserSession 页面读取稳定的链接、表格、表单与元数据。"""

    def __init__(self, session: Any) -> None:
        self._session = session

    @staticmethod
    def _positive_limit(
        value: int,
        name: str,
        *,
        maximum: int = _MAX_RESULT_ITEMS,
    ) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ExtractionError("invalid_args", f"{name} 必须是正整数")
        if value > maximum:
            raise ExtractionError("invalid_args", f"{name} 不能大于 {maximum}")
        return value

    def _evaluate(self, expression: str, argument: Any = None) -> Any:
        """仅运行工具自带的读取表达式，不执行页面脚本字符串。"""
        try:
            return self._session._page.evaluate(expression, argument)
        except Exception as exc:
            if self._session._is_permanent_browser_error(exc):
                raise ExtractionError("page_closed", "页面已关闭，无法读取结构化内容") from exc
            raise ExtractionError("extract_failed", f"读取页面结构失败: {exc}") from exc

    @staticmethod
    def _ensure_list(value: Any, *, name: str) -> list[dict[str, Any]]:
        if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
            raise ExtractionError("extract_failed", f"{name} 返回了无效结构")
        return value

    def find_in_page(self, query: str, max_results: int) -> dict[str, Any]:
        """在可见文本节点中查找字符串，不滚动也不打开浏览器查找窗口。"""
        if not isinstance(query, str) or not query.strip():
            raise ExtractionError("invalid_args", "query 必须是非空字符串")
        if len(query) > _MAX_QUERY_CHARS:
            raise ExtractionError(
                "invalid_args", f"query 不能超过 {_MAX_QUERY_CHARS} 个字符"
            )
        limit = self._positive_limit(max_results, "max_results")
        ref_selector = "a[href], button, input, textarea, select, [role=button]"
        result = self._evaluate(
            r"""(input) => {
                const query = input.query.toLocaleLowerCase();
                const limit = input.limit;
                const ignored = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE']);
                const refTargets = Array.from(document.querySelectorAll(input.refSelector));
                const visible = (element) => {
                    for (let current = element; current; current = current.parentElement) {
                        if (current.hidden || current.getClientRects().length === 0) return false;
                        const style = getComputedStyle(current);
                        if (style.display === 'none' || style.visibility === 'hidden' ||
                            style.visibility === 'collapse' || Number(style.opacity) <= 0) return false;
                    }
                    return true;
                };
                const matches = [];
                let count = 0;
                const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, {
                    acceptNode(node) {
                        const parent = node.parentElement;
                        if (!parent || ignored.has(parent.tagName) || !node.nodeValue.trim() || !visible(parent)) {
                            return NodeFilter.FILTER_REJECT;
                        }
                        return NodeFilter.FILTER_ACCEPT;
                    }
                });
                let node;
                while ((node = walker.nextNode())) {
                    const text = node.nodeValue.replace(/\s+/g, ' ').trim();
                    if (!text.toLocaleLowerCase().includes(query)) continue;
                    count += 1;
                    if (matches.length >= limit) continue;
                    const parent = node.parentElement;
                    const context = (parent.innerText || parent.textContent || '')
                        .replace(/\s+/g, ' ').trim();
                    const refTarget = parent.closest(input.refSelector);
                    matches.push({
                        text: text.slice(0, 500),
                        context: context.slice(0, 800),
                        tag: parent.tagName.toLowerCase(),
                        _ref_index: refTarget ? refTargets.indexOf(refTarget) : -1
                    });
                }
                return {matches, match_count: count, truncated: count > matches.length};
            }""",
            {"query": query.strip(), "limit": limit, "refSelector": ref_selector},
        )
        if not isinstance(result, dict):
            raise ExtractionError("extract_failed", "页面内搜索返回了无效结构")
        match_count = result.get("match_count")
        truncated = result.get("truncated")
        if isinstance(match_count, bool) or not isinstance(match_count, int) or not isinstance(truncated, bool):
            raise ExtractionError("extract_failed", "页面内搜索统计信息无效")
        refs = self._session._refs_for_selector_locked(ref_selector)
        matches: list[dict[str, Any]] = []
        for item in self._ensure_list(result.get("matches"), name="页面内搜索"):
            ref_index = item.get("_ref_index")
            matches.append(
                {
                    "text": item.get("text") if isinstance(item.get("text"), str) else "",
                    "context": item.get("context") if isinstance(item.get("context"), str) else "",
                    "tag": item.get("tag") if isinstance(item.get("tag"), str) else "",
                    "ref": refs.get(ref_index) if isinstance(ref_index, int) else None,
                }
            )
        return {
            "query": query,
            "matches": matches,
            "match_count": match_count,
            "truncated": truncated,
        }

    def extract_links(self, max_items: int) -> dict[str, Any]:
        """读取当前页面链接并以规范化 URL 去重，不改变页面状态。"""
        limit = self._positive_limit(max_items, "max_items")
        raw_links = self._ensure_list(
            self._evaluate(
                r"""(input) => {
                    const links = Array.from(document.querySelectorAll('a[href]'));
                    const clip = (value) => String(value || '').slice(0, input.textLimit);
                    return links.slice(0, input.limit + 1).map((element, index) => {
                        const rawHref = element.getAttribute('href') || '';
                        let url = null;
                        let internal = false;
                        try {
                            url = new URL(rawHref, document.baseURI).href;
                            internal = new URL(url).origin === location.origin;
                        } catch (_) {}
                        return {
                            text: clip((element.innerText || element.textContent || '').replace(/\s+/g, ' ').trim()),
                            url: url ? clip(url) : null, raw_href: clip(rawHref),
                            is_internal: internal,
                            rel: clip(element.getAttribute('rel')),
                            _index: index
                        };
                    });
                }""",
                {"limit": limit, "textLimit": _MAX_META_TEXT_CHARS},
            ),
            name="链接提取",
        )
        refs = self._session._refs_for_selector_locked("a[href]")
        seen_urls: set[str] = set()
        links: list[dict[str, Any]] = []
        source_truncated = len(raw_links) > limit
        for item in raw_links[:limit]:
            url = item.get("url")
            if not isinstance(url, str) or not url:
                continue
            normalized = _normalize_url(url)
            if normalized in seen_urls:
                continue
            seen_urls.add(normalized)
            index = item.get("_index")
            links.append(
                {
                    "text": item.get("text") if isinstance(item.get("text"), str) else "",
                    "url": url,
                    "raw_href": item.get("raw_href") if isinstance(item.get("raw_href"), str) else "",
                    "is_internal": item.get("is_internal") is True,
                    "rel": item.get("rel") if isinstance(item.get("rel"), str) else "",
                    "ref": refs.get(index) if isinstance(index, int) else None,
                }
            )
        return {"links": links, "count": len(links), "truncated": source_truncated}

    def extract_tables(self, max_items: int) -> dict[str, Any]:
        """读取 table 的标题、表头和文本单元格，不执行表格内脚本。"""
        limit = self._positive_limit(max_items, "max_items", maximum=100)
        raw_tables = self._ensure_list(
            self._evaluate(
                r"""(input) => {
                    const text = (element) => (element
                        ? element.textContent.replace(/\s+/g, ' ').trim().slice(0, input.textLimit)
                        : '');
                    return Array.from(document.querySelectorAll('table')).slice(0, input.limit + 1)
                        .map((table, index) => {
                            const allRows = Array.from(table.rows);
                            const headerRow = table.tHead?.rows[0] || allRows.find((row) => row.querySelector('th'));
                            const headers = headerRow ? Array.from(headerRow.cells).slice(0, input.columnLimit).map(text) : [];
                            const bodyRows = allRows.filter((row) => row !== headerRow);
                            const rows = bodyRows.slice(0, input.rowLimit)
                                .map((row) => Array.from(row.cells).slice(0, input.columnLimit).map(text));
                            return {
                                table_index: index,
                                caption: text(table.caption),
                                headers,
                                rows,
                                truncated: bodyRows.length > input.rowLimit ||
                                    (headerRow ? headerRow.cells.length > input.columnLimit : false) ||
                                    bodyRows.some((row) => row.cells.length > input.columnLimit)
                            };
                        });
                }""",
                {
                    "limit": limit,
                    "rowLimit": min(limit, _MAX_TABLE_ROWS),
                    "columnLimit": _MAX_TABLE_COLUMNS,
                    "textLimit": _MAX_TABLE_TEXT_CHARS,
                },
            ),
            name="表格提取",
        )
        tables = raw_tables[:limit]
        return {
            "tables": tables,
            "count": len(tables),
            "truncated": len(raw_tables) > limit or any(item.get("truncated") is True for item in tables),
        }

    def extract_forms(self, max_items: int) -> dict[str, Any]:
        """读取表单字段而不提交或聚焦任何控件。"""
        limit = self._positive_limit(max_items, "max_items", maximum=100)
        raw_forms = self._ensure_list(
            self._evaluate(
                r"""(input) => {
                    const controls = Array.from(document.querySelectorAll('input, textarea, select'));
                    const clip = (value, limit) => String(value || '').slice(0, limit);
                    const labelOf = (element) => {
                        const labels = element.labels ? Array.from(element.labels)
                            .map((label) => (label.innerText || label.textContent || '').trim()) : [];
                        if (labels.length) return clip(labels.join(' ').replace(/\s+/g, ' ').trim(), input.textLimit);
                        const parentLabel = element.closest('label');
                        return clip((element.getAttribute('aria-label') || parentLabel?.innerText || '')
                            .replace(/\s+/g, ' ').trim(), input.textLimit);
                    };
                    return Array.from(document.forms).slice(0, input.limit + 1).map((form, index) => ({
                        form_index: index,
                        action: clip(form.getAttribute('action') ? new URL(form.getAttribute('action'), document.baseURI).href : document.URL, input.textLimit),
                        method: (form.getAttribute('method') || 'get').toLowerCase(),
                        fields: Array.from(form.elements).filter((element) =>
                            element instanceof HTMLInputElement || element instanceof HTMLTextAreaElement || element instanceof HTMLSelectElement
                        ).slice(0, input.fieldLimit).map((element) => {
                            const controlIndex = controls.indexOf(element);
                            const type = element instanceof HTMLInputElement ? (element.type || 'text') : element.tagName.toLowerCase();
                            const value = element instanceof HTMLInputElement && element.type === 'password' ? '[redacted]' : String(element.value || '');
                            return {
                                name: clip(element.getAttribute('name'), input.textLimit), type,
                                label: labelOf(element), required: Boolean(element.required),
                                value: value.slice(0, input.textLimit), _index: controlIndex
                            };
                        }),
                        truncated: Array.from(form.elements).filter((element) =>
                            element instanceof HTMLInputElement || element instanceof HTMLTextAreaElement || element instanceof HTMLSelectElement
                        ).length > input.fieldLimit
                    }));
                }""",
                {
                    "limit": limit,
                    "fieldLimit": _MAX_FORM_FIELDS,
                    "textLimit": _MAX_FIELD_VALUE_CHARS,
                },
            ),
            name="表单提取",
        )
        const_refs = self._session._refs_for_selector_locked(
            "input, textarea, select"
        )
        forms: list[dict[str, Any]] = []
        for form in raw_forms[:limit]:
            fields = form.get("fields")
            if not isinstance(fields, list):
                raise ExtractionError("extract_failed", "表单字段结构无效")
            safe_fields: list[dict[str, Any]] = []
            for field in fields:
                if not isinstance(field, dict):
                    raise ExtractionError("extract_failed", "表单字段结构无效")
                index = field.get("_index")
                safe_fields.append(
                    {
                        "name": field.get("name") if isinstance(field.get("name"), str) else "",
                        "type": field.get("type") if isinstance(field.get("type"), str) else "",
                        "label": field.get("label") if isinstance(field.get("label"), str) else "",
                        "required": field.get("required") is True,
                        "value": field.get("value") if isinstance(field.get("value"), str) else "",
                        "ref": const_refs.get(index) if isinstance(index, int) else None,
                    }
                )
            forms.append(
                {
                    "form_index": form.get("form_index") if isinstance(form.get("form_index"), int) else len(forms),
                    "action": form.get("action") if isinstance(form.get("action"), str) else "",
                    "method": form.get("method") if isinstance(form.get("method"), str) else "get",
                    "fields": safe_fields,
                    "truncated": form.get("truncated") is True,
                }
            )
        return {
            "forms": forms,
            "count": len(forms),
            "truncated": len(raw_forms) > limit
            or any(form["truncated"] for form in forms),
        }

    def extract_metadata(self) -> dict[str, Any]:
        """读取 head 元数据并只 JSON.parse 合法 JSON-LD 文本。"""
        result = self._evaluate(
            """(input) => {
                const meta = {};
                const openGraph = {};
                let truncated = false;
                const clip = (value) => String(value || '').slice(0, input.textLimit);
                let metaCount = 0;
                for (const element of document.querySelectorAll('meta[name], meta[property]')) {
                    const key = element.getAttribute('property') || element.getAttribute('name');
                    const value = element.getAttribute('content');
                    if (!key || value === null || Object.prototype.hasOwnProperty.call(meta, key)) continue;
                    if (metaCount >= input.metaLimit) { truncated = true; continue; }
                    if (key.toLowerCase().startsWith('og:')) openGraph[clip(key)] = clip(value);
                    else meta[clip(key)] = clip(value);
                    metaCount += 1;
                }
                const jsonLd = [];
                for (const script of document.querySelectorAll('script[type="application/ld+json"]')) {
                    if (jsonLd.length >= input.jsonLdLimit) { truncated = true; continue; }
                    const source = script.textContent || '';
                    if (source.length > input.jsonLdChars) { truncated = true; continue; }
                    try {
                        const parsed = JSON.parse(source);
                        if (JSON.stringify(parsed).length > input.jsonLdChars) { truncated = true; continue; }
                        jsonLd.push(parsed);
                    } catch (_) {}
                }
                const canonical = document.querySelector('link[rel="canonical"]')?.getAttribute('href');
                let canonicalUrl = null;
                try { canonicalUrl = canonical ? new URL(canonical, document.baseURI).href : null; } catch (_) {}
                return {
                    title: clip(document.title),
                    description: clip(meta.description),
                    canonical_url: canonicalUrl ? clip(canonicalUrl) : null,
                    open_graph: openGraph,
                    meta,
                    json_ld: jsonLd,
                    truncated
                };
            }""",
            {
                "metaLimit": _MAX_META_ITEMS,
                "textLimit": _MAX_META_TEXT_CHARS,
                "jsonLdLimit": _MAX_JSON_LD_ITEMS,
                "jsonLdChars": _MAX_JSON_LD_CHARS,
            },
        )
        if not isinstance(result, dict):
            raise ExtractionError("extract_failed", "元数据提取返回了无效结构")
        return result

    def _extract_kind(
        self,
        kind: str,
        max_items: int,
        remaining_text_chars: int,
    ) -> tuple[list[dict[str, Any]], bool]:
        # 先按一项可能占用的最大文本量收紧本页提取数量，避免在汇总预算
        # 检查前先把异常大表格或表单从浏览器进程传回 Python。
        estimated_chars = {
            "links": _MAX_META_TEXT_CHARS * 3,
            "tables": _MAX_TABLE_ROWS * _MAX_TABLE_COLUMNS * _MAX_TABLE_TEXT_CHARS,
            "forms": _MAX_FORM_FIELDS * _MAX_FIELD_VALUE_CHARS * 3,
            "metadata": _MAX_JSON_LD_CHARS,
        }
        if kind not in estimated_chars:
            raise ExtractionError("invalid_args", "extract_kind 必须是 links、tables、forms 或 metadata")
        safe_limit = min(
            max_items,
            max(1, remaining_text_chars // estimated_chars[kind]),
        )
        if kind == "links":
            result = self.extract_links(safe_limit)
            return result["links"], result["truncated"]
        if kind == "tables":
            result = self.extract_tables(safe_limit)
            return result["tables"], result["truncated"]
        if kind == "forms":
            result = self.extract_forms(safe_limit)
            return result["forms"], result["truncated"]
        if kind == "metadata":
            return [self.extract_metadata()], False

    def _find_next_page(self) -> dict[str, Any] | None:
        """只选择明确标记或具有明确可访问名称的下一页控件。"""
        candidate = self._evaluate(
            r"""() => {
                const normalize = (value) => String(value || '').replace(/\s+/g, ' ').trim();
                const nextNames = new Set(['next', 'next page', '下一页', '下页', '下一頁']);
                const toUrl = (element) => {
                    const raw = element.getAttribute('href');
                    if (!raw || element.getAttribute('target') === '_blank') return null;
                    try {
                        const url = new URL(raw, document.baseURI);
                        return ['http:', 'https:'].includes(url.protocol) ? url.href : null;
                    } catch (_) { return null; }
                };
                const named = (element) => normalize(
                    element.getAttribute('aria-label') || element.getAttribute('title') ||
                    element.innerText || element.textContent
                ).toLocaleLowerCase();
                const isNext = (element) => nextNames.has(named(element));
                const link = (element, priority) => {
                    const url = toUrl(element);
                    return url ? {kind: 'link', url, priority} : null;
                };
                for (const element of document.querySelectorAll('a[rel~="next"][href], link[rel~="next"][href]')) {
                    const found = link(element, 1); if (found) return found;
                }
                for (const element of document.querySelectorAll('a[href]')) {
                    if (isNext(element)) { const found = link(element, 2); if (found) return found; }
                }
                const buttons = Array.from(document.querySelectorAll('button, [role="button"]'));
                const eligibleButton = (element) => !element.matches(':disabled, [type="submit"], [type="image"]') &&
                    element.getAttribute('aria-disabled') !== 'true' &&
                    !element.closest('form') && !element.hasAttribute('form') && isNext(element);
                const regions = document.querySelectorAll(
                    '[role="navigation"], nav, .pagination, [aria-label*="pagination" i], [aria-label*="分页"]'
                );
                for (const region of regions) {
                    for (const element of region.querySelectorAll('a[href]')) {
                        if (isNext(element)) { const found = link(element, 3); if (found) return found; }
                    }
                    for (const element of region.querySelectorAll('button, [role="button"]')) {
                        const index = buttons.indexOf(element);
                        if (index >= 0 && eligibleButton(element)) {
                            return {kind: 'button', index, priority: 3};
                        }
                    }
                }
                return null;
            }"""
        )
        return candidate if isinstance(candidate, dict) else None

    @staticmethod
    def _item_key(kind: str, item: dict[str, Any]) -> str:
        if kind == "links" and isinstance(item.get("url"), str):
            return _normalize_url(item["url"])
        return json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def _page_content_fingerprint(self) -> str:
        """在浏览器内计算页面可读文本指纹，只返回短字符串而不传回整页内容。"""
        result = self._evaluate(
            """() => {
                const walker = document.createTreeWalker(
                    document.body, NodeFilter.SHOW_TEXT
                );
                const sampleLimit = 200000;
                let hash = 2166136261;
                let totalLength = 0;
                let sampledLength = 0;
                let node;
                while ((node = walker.nextNode())) {
                    const text = node.nodeValue || '';
                    totalLength += text.length;
                    const end = Math.min(text.length, sampleLimit - sampledLength);
                    for (let index = 0; index < end; index += 1) {
                        hash ^= text.charCodeAt(index);
                        hash = Math.imul(hash, 16777619);
                    }
                    sampledLength += end;
                }
                return `${totalLength}:${sampledLength}:${hash >>> 0}`;
            }"""
        )
        if not isinstance(result, str):
            raise ExtractionError("extract_failed", "页面内容指纹返回了无效结构")
        return result

    def collect_paginated(
        self,
        snapshot_id: str,
        *,
        extract_kind: str,
        budget: BrowseBudget,
        same_origin: bool,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        """在固定预算内提取当前页并跟随明确的下一页链接或按钮。"""
        if not isinstance(extract_kind, str):
            raise ExtractionError("invalid_args", "extract_kind 必须是字符串")
        if not isinstance(same_origin, bool):
            raise ExtractionError("invalid_args", "same_origin 必须是布尔值")
        deadline = monotonic() + budget.timeout_ms / 1000.0
        current_snapshot_id = snapshot_id
        first_origin = self._session._page.url
        items: list[dict[str, Any]] = []
        item_keys: set[str] = set()
        page_signatures: set[str] = set()
        seen_urls: set[str] = set()
        visited_urls: list[str] = []
        stop_reason = "no_next_page"
        truncated = False
        text_chars = 0
        event_type = "none"
        used_fallback = False
        dialogs: list[dict[str, Any]] = []

        def cancelled(*, execution_state: str = "cancelled") -> dict[str, Any]:
            return {
                "cancelled": True,
                "execution_state": execution_state,
                "items": items,
                "pages_visited": len(visited_urls),
                "snapshot_id": current_snapshot_id,
            }

        while True:
            if cancel_event is not None and cancel_event.is_set():
                return cancelled()
            if monotonic() >= deadline:
                stop_reason = "timeout"
                truncated = True
                break
            current_url = _normalize_url(self._session._page.url)
            if len(visited_urls) >= budget.max_visited_urls:
                stop_reason = "max_pages"
                truncated = True
                break
            visited_urls.append(current_url)
            seen_urls.add(current_url)
            remaining_items = budget.max_items - len(items)
            if remaining_items <= 0:
                stop_reason = "max_items"
                truncated = True
                break
            page_items, page_truncated = self._extract_kind(
                extract_kind,
                remaining_items,
                budget.max_text_chars - text_chars,
            )
            if cancel_event is not None and cancel_event.is_set():
                return cancelled()
            fingerprint = json.dumps(
                page_items, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            content_fingerprint = self._page_content_fingerprint()
            page_signature = (
                f"{current_url}\u0000{content_fingerprint}\u0000{fingerprint}"
            )
            if page_signature in page_signatures:
                stop_reason = "repeated_page"
                truncated = True
                break
            page_signatures.add(page_signature)
            for item in page_items:
                key = self._item_key(extract_kind, item)
                if key in item_keys:
                    continue
                item_text_size = len(
                    json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                )
                if text_chars + item_text_size > budget.max_text_chars:
                    stop_reason = "max_text_chars"
                    truncated = True
                    break
                item_keys.add(key)
                items.append(item)
                text_chars += item_text_size
                if len(items) >= budget.max_items:
                    stop_reason = "max_items"
                    truncated = True
                    break
            if stop_reason in {"max_items", "max_text_chars"}:
                break
            if page_truncated:
                truncated = True
            if len(visited_urls) >= budget.max_pages:
                stop_reason = "max_pages"
                truncated = True
                break
            if monotonic() >= deadline:
                stop_reason = "timeout"
                truncated = True
                break
            next_page = self._find_next_page()
            if next_page is None:
                stop_reason = "no_next_page"
                break
            if next_page.get("kind") == "link":
                next_url = next_page.get("url")
                if not isinstance(next_url, str) or not next_url:
                    stop_reason = "no_next_page"
                    break
                normalized_next_url = _normalize_url(next_url)
                if normalized_next_url in seen_urls:
                    stop_reason = "repeated_page"
                    truncated = True
                    break
                if same_origin and not _same_origin(first_origin, next_url):
                    stop_reason = "no_next_page"
                    break
                if cancel_event is not None and cancel_event.is_set():
                    return cancelled()
                remaining_ms = max(1, int((deadline - monotonic()) * 1000))
                navigation = self._session._navigate_for_pagination_locked(
                    next_url,
                    remaining_ms,
                    _cancel_event=cancel_event,
                )
            elif next_page.get("kind") == "button":
                index = next_page.get("index")
                if not isinstance(index, int) or index < 0:
                    stop_reason = "no_next_page"
                    break
                if cancel_event is not None and cancel_event.is_set():
                    return cancelled()
                remaining_ms = max(1, int((deadline - monotonic()) * 1000))
                navigation = self._session._click_pagination_next_locked(
                    index,
                    remaining_ms,
                    _cancel_event=cancel_event,
                )
            else:
                stop_reason = "no_next_page"
                break
            try:
                navigation_payload = json.loads(navigation)
            except (TypeError, json.JSONDecodeError):
                stop_reason = "navigation_failed"
                truncated = True
                break
            if navigation_payload.get("error_type") == "cancelled":
                return cancelled(
                    execution_state=(
                        "unknown"
                        if navigation_payload.get("execution_state") == "unknown"
                        else "cancelled"
                    )
                )
            next_snapshot_id = navigation_payload.get("snapshot_id")
            returned_event_type = navigation_payload.get("event_type")
            if isinstance(returned_event_type, str):
                event_type = returned_event_type
            used_fallback = navigation_payload.get("used_fallback") is True
            returned_dialogs = navigation_payload.get("dialogs")
            if isinstance(returned_dialogs, list):
                dialogs = returned_dialogs
            if isinstance(next_snapshot_id, str) and next_snapshot_id:
                # 动作一旦发出，旧快照已经失效；即使后续判为导航失败，也要把
                # 可恢复观察返回给调用方，不能继续引用翻页前的快照。
                current_snapshot_id = next_snapshot_id
            if not navigation_payload.get("ok") or navigation_payload.get("event_type") == "popup":
                stop_reason = (
                    "repeated_page"
                    if navigation_payload.get("error_type") == "repeated_page"
                    else "navigation_failed"
                )
                truncated = True
                break
            if not isinstance(next_snapshot_id, str) or not next_snapshot_id:
                stop_reason = "navigation_failed"
                truncated = True
                break
            if same_origin and not _same_origin(first_origin, self._session._page.url):
                # 按钮没有静态 href 可在动作前预检，因此这里绝不读取跨源结果，
                # 并明确告知调用方自动分页已到达同源边界。
                stop_reason = "cross_origin"
                truncated = True
                break
            if monotonic() >= deadline:
                stop_reason = "timeout"
                truncated = True
                break

        return {
            "extract_kind": extract_kind,
            "items": items,
            "pages_visited": len(visited_urls),
            "items_collected": len(items),
            "visited_urls": visited_urls,
            "stop_reason": stop_reason,
            "truncated": truncated,
            "budget": budget.public_payload(),
            "snapshot_id": current_snapshot_id,
            "url": self._session._page.url,
            "event_type": event_type,
            "used_fallback": used_fallback,
            "dialogs": dialogs,
        }
