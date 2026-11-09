from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from urllib.parse import urlsplit

from markdown_it import MarkdownIt


_ALLOWED = {
    "b", "i", "u", "s", "tg-spoiler", "a", "code", "pre", "blockquote", "br",
}
_VOID = {"br"}
_TOKEN_RE = re.compile(r"(<[^>]*>|&(?:#\d+|#x[0-9a-fA-F]+|[A-Za-z][A-Za-z0-9]+);|[^<&]+|[<&])")
_TAG_RE = re.compile(r"^<\s*(/?)\s*([a-zA-Z][a-zA-Z0-9-]*)")
_ALLOWED_LINK_SCHEMES = {"http", "https", "tg", "mailto"}


def _safe_href(value: str) -> str:
    try:
        parsed = urlsplit(value.strip())
        if parsed.scheme.lower() not in _ALLOWED_LINK_SCHEMES:
            return ""
        if parsed.scheme.lower() in {"http", "https"} and not parsed.netloc:
            return ""
        return value
    except ValueError:
        return ""


class _Sanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.stack: list[tuple[str, str]] = []
        self.list_depth = 0
        self.list_counters: list[int] = []

    def _open(self, source: str, rendered: str, attrs: str = "") -> None:
        self.out.append("<" + rendered + attrs + ">")
        self.stack.append((source, rendered))

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = (tag or "").lower()
        attrs_map = {str(key).lower(): value or "" for key, value in attrs}

        if tag in {"h1", "h2", "h3", "h4", "h5", "h6", "strong"}:
            self._open(tag, "b")
            return
        if tag in {"em"}:
            self._open(tag, "i")
            return
        if tag in {"ins"}:
            self._open(tag, "u")
            return
        if tag in {"strike", "del"}:
            self._open(tag, "s")
            return
        if tag in {"p", "div", "section", "article"}:
            if self.out and not self.out[-1].endswith("\n"):
                self.out.append("\n\n")
            self.stack.append((tag, ""))
            return
        if tag == "ul":
            self.list_depth += 1
            self.list_counters.append(0)
            self.stack.append((tag, ""))
            return
        if tag == "ol":
            self.list_depth += 1
            self.list_counters.append(0)
            self.stack.append((tag, ""))
            return
        if tag == "li":
            if self.list_counters:
                self.list_counters[-1] += 1
                parent = next((name for name, _ in reversed(self.stack) if name in {"ul", "ol"}), "ul")
                prefix = (str(self.list_counters[-1]) + ". ") if parent == "ol" else "• "
                self.out.append("\n" + ("  " * max(0, self.list_depth - 1)) + prefix)
            self.stack.append((tag, ""))
            return
        if tag == "hr":
            self.out.append("\n")
            return
        if tag == "br":
            self.out.append("\n")
            return
        if tag == "a":
            href = _safe_href(str(attrs_map.get("href") or ""))
            if href:
                self._open(tag, "a", ' href="' + html.escape(href, quote=True) + '"')
            else:
                self.stack.append((tag, ""))
            return
        if tag == "blockquote":
            expandable = " expandable" if "expandable" in attrs_map else ""
            self._open(tag, "blockquote", expandable)
            return
        if tag in {"b", "i", "u", "s", "tg-spoiler", "code", "pre"}:
            if tag == "pre":
                self._open(tag, "pre")
            else:
                self._open(tag, tag)
            return
        if tag == "span":
            self.stack.append((tag, ""))
            return
        if tag in {"img"}:
            alt = str(attrs_map.get("alt") or "").strip()
            if alt:
                self.out.append(html.escape(alt))
            return
        if tag in {"script", "style", "svg", "iframe", "object"}:
            self.stack.append((tag, "!drop"))
            return
        self.stack.append((tag, ""))

    def handle_startendtag(self, tag, attrs) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = (tag or "").lower()
        if tag in _VOID:
            return
        for index in range(len(self.stack) - 1, -1, -1):
            source, rendered = self.stack[index]
            if source != tag:
                continue
            closing = self.stack[index:]
            self.stack = self.stack[:index]
            for _source, name in reversed(closing):
                if name == "!drop":
                    continue
                if name:
                    self.out.append("</" + name + ">")
            if tag in {"ul", "ol"}:
                self.list_depth = max(0, self.list_depth - 1)
                if self.list_counters:
                    self.list_counters.pop()
            if tag in {"p", "div", "section", "article"}:
                self.out.append("\n\n")
            return

    def handle_data(self, data: str) -> None:
        if any(name == "!drop" for _, name in self.stack):
            return
        self.out.append(html.escape(data or "", quote=False))

    def result(self) -> str:
        for _source, rendered in reversed(self.stack):
            if rendered and rendered != "!drop":
                self.out.append("</" + rendered + ">")
        self.stack.clear()
        value = "".join(self.out)
        value = re.sub(r"\n{3,}", "\n\n", value).strip()
        return value or " "


def _table_to_pre(block: str) -> str:
    rows = []
    for line in block.splitlines():
        row = line.strip().strip("|")
        cells = [cell.strip() for cell in row.split("|")]
        if cells and all(re.fullmatch(r":?-{3,}:?", cell.replace(" ", "")) for cell in cells):
            continue
        rows.append(cells)
    if not rows:
        return ""
    columns = max(len(row) for row in rows)
    widths = []
    for col in range(columns):
        widths.append(min(32, max(3, max((len(row[col]) if col < len(row) else 0) for row in rows))))
    lines = []
    for index, row in enumerate(rows):
        values = []
        for col, width in enumerate(widths):
            cell = row[col] if col < len(row) else ""
            cell = cell[:width]
            values.append(cell.ljust(width))
        lines.append(" | ".join(values).rstrip())
        if index == 0 and len(rows) > 1:
            lines.append("-+-".join("-" * width for width in widths))
    return "<pre>" + html.escape("\n".join(lines)) + "</pre>"


def _convert_markdown_tables(value: str) -> str:
    lines = value.splitlines()
    output: list[str] = []
    index = 0
    while index < len(lines):
        if index + 1 < len(lines) and "|" in lines[index] and re.search(r"\|?\s*:?-{3,}:?\s*\|", lines[index + 1]):
            block = [lines[index], lines[index + 1]]
            index += 2
            while index < len(lines) and "|" in lines[index] and lines[index].strip():
                block.append(lines[index])
                index += 1
            output.append(_table_to_pre("\n".join(block)))
            continue
        output.append(lines[index])
        index += 1
    return "\n".join(output)


def _has_html(value: str) -> bool:
    return bool(re.search(r"</?(?:b|strong|i|em|u|ins|s|strike|del|tg-spoiler|a|code|pre|blockquote|p|div|h[1-6]|ul|ol|li|table)\b", value, re.I))


def to_telegram_html(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "Пустой ответ."
    if _has_html(raw):
        source = raw
    else:
        raw = _convert_markdown_tables(raw)
        source = MarkdownIt("commonmark", {"html": True, "breaks": True, "linkify": False}).render(raw)
    parser = _Sanitizer()
    parser.feed(source)
    parser.close()
    return parser.result()


def _utf16_units(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def split_telegram_html(value: str, limit: int = 3900) -> list[str]:
    """Split formatted output without cutting entities or leaving tags unbalanced."""
    limit = max(512, min(4096, int(limit)))
    chunks: list[str] = []
    current = ""
    current_units = 0
    open_tags: list[tuple[str, str]] = []

    def begin() -> tuple[str, int]:
        prefix = "".join(raw for raw, _ in open_tags)
        return prefix, 0

    current, current_units = begin()
    for token in _TOKEN_RE.findall(value or ""):
        if token.startswith("<"):
            match = _TAG_RE.match(token)
            if match:
                closing, name = match.groups()
                name = name.lower()
                if closing:
                    current += token
                    current_units += 0
                    for index in range(len(open_tags) - 1, -1, -1):
                        if open_tags[index][1] == name:
                            open_tags.pop(index)
                            break
                elif name not in {"br"} and not token.rstrip().endswith("/>"):
                    current += token
                    open_tags.append((token, name))
                else:
                    current += token
            else:
                current += token
            continue
        if token.startswith("&") and token.endswith(";"):
            pieces = [token]
        else:
            pieces = list(token)
        for piece in pieces:
            units = 1 if piece.startswith("&") and piece.endswith(";") else _utf16_units(piece)
            if current_units + units > limit and current.strip():
                closing = "".join("</" + name + ">" for _, name in reversed(open_tags))
                chunks.append(current + closing)
                current = "".join(raw for raw, _ in open_tags)
                current_units = 0
            current += piece
            current_units += units
    if current.strip() or not chunks:
        current += "".join("</" + name + ">" for _, name in reversed(open_tags))
        chunks.append(current)
    return [chunk for chunk in chunks if chunk.strip()]
