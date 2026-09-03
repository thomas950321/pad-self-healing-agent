import argparse
import asyncio
import base64
import fnmatch
import json
import mimetypes
import os
import re
import hashlib
import socket
import struct
import subprocess
import time
import traceback
from collections import deque
from html import unescape
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import httpx
from playwright.async_api import Browser, Page, TimeoutError as PlaywrightTimeoutError, async_playwright

try:
    from bs4 import BeautifulSoup  # type: ignore
except Exception:
    BeautifulSoup = None


# AI 可以要求、而執行器也允許執行的動作白名單。
ALLOWED_ACTIONS = {
    "click",
    "acceptdialog",
    "fill",
    "press",
    "selectoption",
    "wait",
    "scroll",
    "refresh",
    "goback",
    "openurl",
    "switchtab",
    "closetab",
    "hover",
}


PAGE_SNAPSHOT_HTML_EXCERPT_LIMIT = 18000
PAGE_SNAPSHOT_TEXT_EXCERPT_LIMIT = 12000
PAGE_SNAPSHOT_ELEMENT_LIMIT = 80
PAGE_SNAPSHOT_LINK_LIMIT = 40

PROMPT_PAGE_HTML_EXCERPT_LIMIT = 2800
PROMPT_PAGE_TEXT_EXCERPT_LIMIT = 1800
PROMPT_FRAME_HTML_EXCERPT_LIMIT = 1200
PROMPT_FRAME_TEXT_EXCERPT_LIMIT = 900
PROMPT_FRAME_LIMIT = 2
PROMPT_INTERACTIVE_ELEMENT_LIMIT = 12

PAGE_STATE_TARGET_LIMIT = 3
PAGE_STATE_FRAME_LIMIT = 3
PAGE_STATE_TIMEOUT_MS = 250
OVERLAY_FRAME_LIMIT = 2
OVERLAY_TIMEOUT_MS = 200

ACTION_POST_CLICK_DELAY_MS = 150
ACTION_POST_NAVIGATION_DELAY_MS = 300
ACTION_NAVIGATION_TIMEOUT_MS = 3000
ACTION_LOCATOR_TIMEOUT_MS = 2000
ACTION_VISIBLE_TIMEOUT_MS = 500
ACTION_WAIT_MIN_MS = 10000
ACTION_WAIT_MAX_MS = 20000
ACTION_NEW_PAGE_TIMEOUT_MS = 1500
ACTION_NEW_PAGE_FALLBACK_TIMEOUT_MS = 250

# CDP 的 TCP port 開啟不代表瀏覽器層 WebSocket 已可用，因此連線前會
# 先執行 Browser.getVersion probe，再進入 Playwright connect_over_cdp。
CDP_CONNECT_TIMEOUT_MS = 8000
CDP_CONNECT_ATTEMPTS = 2
CDP_PREFLIGHT_TIMEOUT_SECONDS = 2.0
CDP_RETRY_DELAYS_SECONDS = (0.5, 1.0)

# If the model is uncertain but still provides a bounded, reversible recovery
# plan with a real verification rule, allow the agent to try it. Destructive
# or data-entry actions remain excluded from this soft-confidence path.
SOFT_RECOVERY_ACTIONS = {
    "click",
    "acceptdialog",
    "press",
    "wait",
    "scroll",
    "hover",
    "switchtab",
}
SOFT_RECOVERY_VERIFICATIONS = {
    "page_ready",
    "dialog_accepted",
    "selector_visible",
    "text_visible",
    "url_contains",
    "url_equals",
    "title_contains",
}


# Log-first diagnosis mode: rely on context.json, screenshots, browser state, and logs.
WORKFLOW_GUIDES = {}


# 讀取 context 與處理輸入資料的工具
def _load_context(context_file: str) -> Dict[str, Any]:
    with open(context_file, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def _read_image_as_data_url(image_file: str) -> str:
    mime_type, _ = mimetypes.guess_type(image_file)
    if not mime_type or not mime_type.startswith("image/"):
        mime_type = "image/jpeg"

    with open(image_file, "rb") as f:
        encoded = base64.b64encode(f.read()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def _write_text_atomic(target_path: str, text: str) -> None:
    directory = os.path.dirname(target_path)
    if directory:
        os.makedirs(directory, exist_ok=True)

    tmp_path = f"{target_path}.tmp"
    with open(tmp_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp_path, target_path)


def _cleanup_html_artifacts(artifact_dir: str) -> None:
    if not artifact_dir or not os.path.isdir(artifact_dir):
        return

    for entry in os.listdir(artifact_dir):
        lowered = entry.lower()
        if lowered != "page.html" and not fnmatch.fnmatch(lowered, "frame_*.html"):
            continue

        path = os.path.join(artifact_dir, entry)
        try:
            if os.path.isfile(path):
                os.remove(path)
        except Exception:
            pass



def _html_excerpt_from_text(html: str, limit: int) -> str:
    text = str(html or "")
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _soup_text_excerpt(html: str, limit: int) -> str:
    html = str(html or "")
    if not html:
        return ""

    if BeautifulSoup is not None:
        try:
            soup = BeautifulSoup(html, "html.parser")
            text = soup.get_text(" ", strip=True)
            text = unescape(re.sub(r"\s+", " ", text)).strip()
            return text if len(text) <= limit else text[: limit - 3] + "..."
        except Exception:
            pass

    text = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.IGNORECASE)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = unescape(re.sub(r"\s+", " ", text)).strip()
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _soup_summary(html: str) -> Dict[str, Any]:
    summary: Dict[str, Any] = {
        "title": "",
        "headings": [],
        "forms": [],
        "links": [],
    }

    html = str(html or "")
    if not html or BeautifulSoup is None:
        return summary

    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return summary

    try:
        title_tag = soup.find("title")
        if title_tag:
            summary["title"] = _truncate_text(title_tag.get_text(" ", strip=True), 200)
    except Exception:
        pass

    try:
        headings: List[str] = []
        for tag in soup.find_all(["h1", "h2", "h3", "legend"], limit=12):
            text = _truncate_text(tag.get_text(" ", strip=True), 160)
            if text and text not in headings:
                headings.append(text)
        summary["headings"] = headings
    except Exception:
        pass

    try:
        forms: List[Dict[str, Any]] = []
        for form in soup.find_all("form", limit=10):
            form_entry: Dict[str, Any] = {
                "id": _truncate_text(form.get("id", ""), 80),
                "name": _truncate_text(form.get("name", ""), 80),
                "action": _truncate_text(form.get("action", ""), 160),
                "method": _truncate_text(form.get("method", ""), 20),
                "text": _truncate_text(form.get_text(" ", strip=True), 220),
            }
            forms.append({key: value for key, value in form_entry.items() if value})
        summary["forms"] = forms
    except Exception:
        pass

    try:
        links: List[Dict[str, Any]] = []
        for anchor in soup.find_all("a", limit=PAGE_SNAPSHOT_LINK_LIMIT):
            href = _truncate_text(anchor.get("href", ""), 180)
            text = _truncate_text(anchor.get_text(" ", strip=True), 160)
            aria = _truncate_text(anchor.get("aria-label", ""), 160)
            if href or text or aria:
                links.append({k: v for k, v in {"text": text, "href": href, "aria_label": aria}.items() if v})
        summary["links"] = links
    except Exception:
        pass

    return summary


async def _capture_scope_snapshot(
    scope: Any,
    scope_label: str,
    artifact_dir: str,
    max_elements: int = PAGE_SNAPSHOT_ELEMENT_LIMIT,
    capture_html: bool = True,
    capture_interactive_elements: bool = True,
) -> Dict[str, Any]:
    snapshot: Dict[str, Any] = {
        "scope_label": scope_label,
        "url": "",
        "title": "",
        "html_path": "",
        "html_excerpt": "",
        "text_excerpt": "",
        "summary": {},
        "interactive_elements": [],
    }

    if not scope:
        return snapshot

    try:
        snapshot["url"] = _truncate_text(getattr(scope, "url", "") or "", 240)
    except Exception:
        snapshot["url"] = ""

    try:
        snapshot["title"] = _truncate_text(await _safe_page_title(scope), 160)
    except Exception:
        snapshot["title"] = ""

    html = ""
    if capture_html:
        try:
            html = await scope.content()
        except Exception:
            html = ""

        if html:
            safe_scope = re.sub(r"[^A-Za-z0-9._-]+", "_", scope_label).strip("_") or "scope"
            html_path = os.path.join(artifact_dir, f"{safe_scope}.html")
            try:
                _write_text_atomic(html_path, html)
                snapshot["html_path"] = html_path
            except Exception:
                snapshot["html_path"] = ""

        snapshot["html_excerpt"] = _html_excerpt_from_text(html, PAGE_SNAPSHOT_HTML_EXCERPT_LIMIT)
        snapshot["text_excerpt"] = _soup_text_excerpt(html, PAGE_SNAPSHOT_TEXT_EXCERPT_LIMIT)
        snapshot["summary"] = _soup_summary(html)

    if capture_interactive_elements:
        try:
            element_payload = await scope.evaluate(
                """({limit}) => {
                    const normalize = (value) => (value || '').toString().replace(/\\s+/g, ' ').trim();
                    const visible = (el) => {
                        if (!el) return false;
                        const style = window.getComputedStyle(el);
                        if (!style) return false;
                        const rect = el.getBoundingClientRect();
                        return style.display !== 'none' && style.visibility !== 'hidden' &&
                            style.opacity !== '0' && rect.width > 0 && rect.height > 0;
                    };
                    const selector = [
                        'button',
                        'a',
                        'input',
                        'select',
                        'textarea',
                        '[role="button"]',
                        '[role="link"]',
                        '[role="menuitem"]',
                        '[role="tab"]',
                        '[onclick]',
                        '[aria-label]',
                        '[title]'
                    ].join(',');
                    const nodes = Array.from(document.querySelectorAll(selector)).slice(0, limit);
                    return nodes.map((el, index) => {
                        const rect = el.getBoundingClientRect();
                        const style = window.getComputedStyle(el);
                        return {
                            index,
                            tag: el.tagName.toLowerCase(),
                            text: normalize(el.innerText || el.textContent),
                            id: normalize(el.id),
                            name: normalize(el.getAttribute('name')),
                            role: normalize(el.getAttribute('role')),
                            aria_label: normalize(el.getAttribute('aria-label')),
                            title: normalize(el.getAttribute('title')),
                            href: normalize(el.getAttribute('href')),
                            value: normalize(el.value),
                            type: normalize(el.getAttribute('type')),
                            classes: normalize(el.className),
                            visible: visible(el),
                            disabled: !!el.disabled,
                            rect: {
                                x: Math.round(rect.x),
                                y: Math.round(rect.y),
                                width: Math.round(rect.width),
                                height: Math.round(rect.height)
                            },
                            display: style ? style.display : '',
                        };
                    });
                }""",
                {"limit": max_elements},
            )
        except Exception:
            element_payload = []

        if isinstance(element_payload, list):
            snapshot["interactive_elements"] = [
                {k: v for k, v in element.items() if v not in ("", None, [], {})}
                for element in element_payload
                if isinstance(element, dict)
            ]

    return snapshot


async def _capture_page_snapshot(page: Optional[Page], artifact_dir: str) -> Dict[str, Any]:
    if not page:
        return {}

    snapshot: Dict[str, Any] = {
        "page": {},
        "frames": [],
    }

    try:
        os.makedirs(artifact_dir, exist_ok=True)
    except Exception:
        pass

    try:
        snapshot["page"] = await _capture_scope_snapshot(page, "page", artifact_dir)
    except Exception:
        snapshot["page"] = {}

    frames: List[Any] = []
    try:
        frames = list(getattr(page, "frames", []))
    except Exception:
        frames = []

    for index, frame in enumerate(frames):
        try:
            frame_label = f"frame_{index}"
            capture_html = index < 3
            frame_snapshot = await _capture_scope_snapshot(
                frame,
                frame_label,
                artifact_dir,
                max_elements=20,
                capture_html=capture_html,
                capture_interactive_elements=capture_html,
            )
            frame_snapshot["index"] = index
            frame_snapshot["name"] = _truncate_text(getattr(frame, "name", "") or "", 160)
            try:
                frame_element = await frame.frame_element()
                frame_box = await frame_element.bounding_box()
            except Exception:
                frame_box = None
            if isinstance(frame_box, dict):
                frame_snapshot["host_rect"] = {
                    "x": int(round(frame_box.get("x", 0) or 0)),
                    "y": int(round(frame_box.get("y", 0) or 0)),
                    "width": int(round(frame_box.get("width", 0) or 0)),
                    "height": int(round(frame_box.get("height", 0) or 0)),
                }
            else:
                frame_snapshot["host_rect"] = {}
            snapshot["frames"].append(frame_snapshot)
        except Exception:
            continue

    snapshot["frame_count"] = len(frames)
    snapshot["captured_at"] = datetime.now(timezone.utc).isoformat()
    return snapshot


def _build_snapshot_prompt_view(page_snapshot: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(page_snapshot, dict) or not page_snapshot:
        return {}

    def _count_interactive_elements(scope: Any) -> Tuple[int, int]:
        if not isinstance(scope, dict):
            return (0, 0)

        elements = scope.get("interactive_elements", [])
        if not isinstance(elements, list):
            return (0, 0)

        visible_count = 0
        total_count = 0
        for item in elements:
            if not isinstance(item, dict):
                continue
            total_count += 1
            if bool(item.get("visible", False)):
                visible_count += 1
        return visible_count, total_count

    def _trim_elements(elements: Any, limit: int = 12) -> List[Dict[str, Any]]:
        if not isinstance(elements, list):
            return []
        trimmed: List[Dict[str, Any]] = []
        for item in elements[:limit]:
            if not isinstance(item, dict):
                continue
            if not bool(item.get("visible", False)):
                continue
            trimmed.append(
                {
                    "tag": _truncate_text(item.get("tag", ""), 20),
                    "text": _truncate_text(item.get("text", ""), 180),
                    "id": _truncate_text(item.get("id", ""), 80),
                    "name": _truncate_text(item.get("name", ""), 80),
                    "role": _truncate_text(item.get("role", ""), 40),
                    "aria_label": _truncate_text(item.get("aria_label", ""), 160),
                    "title": _truncate_text(item.get("title", ""), 160),
                    "href": _truncate_text(item.get("href", ""), 160),
                    "type": _truncate_text(item.get("type", ""), 30),
                    "visible": bool(item.get("visible", False)),
                    "disabled": bool(item.get("disabled", False)),
                    "rect": item.get("rect", {}),
                }
            )
        return trimmed

    page_part = page_snapshot.get("page", {}) if isinstance(page_snapshot.get("page", {}), dict) else {}
    frames_part = _ordered_snapshot_frames(page_snapshot)

    prompt_view: Dict[str, Any] = {
        "frame_count": int(page_snapshot.get("frame_count", 0) or 0),
        "captured_at": _truncate_text(page_snapshot.get("captured_at", ""), 40),
        "page": {
            "url": _truncate_text(page_part.get("url", ""), 240),
            "title": _truncate_text(page_part.get("title", ""), 160),
            "html_excerpt": _truncate_text(page_part.get("html_excerpt", ""), PROMPT_PAGE_HTML_EXCERPT_LIMIT),
            "text_excerpt": _truncate_text(page_part.get("text_excerpt", ""), PROMPT_PAGE_TEXT_EXCERPT_LIMIT),
            "summary": page_part.get("summary", {}),
            "visible_interactive_count": _count_interactive_elements(page_part)[0],
            "interactive_count": _count_interactive_elements(page_part)[1],
            "interactive_elements": _trim_elements(page_part.get("interactive_elements", []), PROMPT_INTERACTIVE_ELEMENT_LIMIT),
        },
        "frames": [],
    }

    for frame in frames_part[:PROMPT_FRAME_LIMIT]:
        if not isinstance(frame, dict):
            continue
        visible_count, total_count = _count_interactive_elements(frame)
        prompt_view["frames"].append(
            {
                "index": frame.get("index", 0),
                "name": _truncate_text(frame.get("name", ""), 80),
                "url": _truncate_text(frame.get("url", ""), 240),
                "title": _truncate_text(frame.get("title", ""), 160),
                "host_rect": frame.get("host_rect", {}),
                "visible_interactive_count": visible_count,
                "interactive_count": total_count,
                "html_excerpt": _truncate_text(frame.get("html_excerpt", ""), PROMPT_FRAME_HTML_EXCERPT_LIMIT),
                "text_excerpt": _truncate_text(frame.get("text_excerpt", ""), PROMPT_FRAME_TEXT_EXCERPT_LIMIT),
                "summary": frame.get("summary", {}),
                "interactive_elements": _trim_elements(frame.get("interactive_elements", []), 8),
            }
        )

    return prompt_view


def _rect_as_ints(value: Any) -> Tuple[int, int, int, int]:
    if not isinstance(value, dict):
        return (0, 0, 0, 0)
    try:
        return (
            int(round(float(value.get("x", 0) or 0))),
            int(round(float(value.get("y", 0) or 0))),
            int(round(float(value.get("width", 0) or 0))),
            int(round(float(value.get("height", 0) or 0))),
        )
    except Exception:
        return (0, 0, 0, 0)


def _snapshot_interactive_counts(scope: Any) -> Tuple[int, int]:
    if not isinstance(scope, dict):
        return (0, 0)

    elements = scope.get("interactive_elements", [])
    if not isinstance(elements, list):
        return (0, 0)

    visible_count = 0
    total_count = 0
    for element in elements:
        if not isinstance(element, dict):
            continue
        total_count += 1
        if _normalize_bool(element.get("visible")):
            visible_count += 1

    return (visible_count, total_count)


def _snapshot_scope_priority(scope: Any) -> Tuple[int, int, int, int, int]:
    if not isinstance(scope, dict):
        return (0, 0, 0, 0, 0)

    visible_count, total_count = _snapshot_interactive_counts(scope)
    hx, hy, width, height = _rect_as_ints(scope.get("host_rect", {}))
    area = max(0, width * height)

    # Prefer denser interactive regions first; use geometry only as a tie-breaker.
    score = (visible_count * 140) + (total_count * 15) + (min(area, 250000) // 5000)
    return (score, total_count, visible_count, -hy, -hx)


def _snapshot_element_priority(
    element: Any,
    host_rect: Any = None,
    scope_index: int = 0,
    scope_visible_count: int = 0,
    scope_total_count: int = 0,
) -> Tuple[int, int, int, int, int, int, int]:
    if not isinstance(element, dict):
        return (0, 0, 0, 0, 0, 0, 0)

    ex, ey, width, height = _rect_as_ints(element.get("rect", {}))
    area = max(0, width * height)
    tag = str(element.get("tag", "") or "").strip().lower()
    textish = str(
        element.get("text", "")
        or element.get("aria_label", "")
        or element.get("title", "")
        or ""
    ).strip()

    score = (scope_visible_count * 140) + (scope_total_count * 15)
    if _normalize_bool(element.get("visible")):
        score += 100
    if tag in {"a", "button", "input", "select", "textarea", "option"}:
        score += 30
    if textish:
        score += 20
    if len(textish) <= 24:
        score += 8
    if area > 0:
        score += min(area, 10000) // 100

    hx, hy, _, _ = _rect_as_ints(host_rect or {})
    return (score, scope_visible_count, scope_total_count, -scope_index, -(hy + ey), -(hx + ex), -area)


def _ordered_snapshot_frames(page_snapshot: Dict[str, Any]) -> List[Dict[str, Any]]:
    frames_part = page_snapshot.get("frames", []) if isinstance(page_snapshot.get("frames", []), list) else []
    ordered_frames = [frame for frame in frames_part if isinstance(frame, dict)]
    ordered_frames.sort(
        key=lambda frame: _snapshot_scope_priority(frame),
        reverse=True,
    )
    return ordered_frames


def _ordered_snapshot_elements(page_snapshot: Dict[str, Any], visible_only: bool = True) -> List[Dict[str, Any]]:
    if not isinstance(page_snapshot, dict) or not page_snapshot:
        return []

    records: List[Tuple[Tuple[int, int, int, int, int, int, int], Dict[str, Any]]] = []

    def _add_elements(elements: Any, host_rect: Any, scope_index: int, scope_visible_count: int, scope_total_count: int) -> None:
        if not isinstance(elements, list):
            return
        for element in elements:
            if not isinstance(element, dict):
                continue
            if visible_only and not _normalize_bool(element.get("visible")):
                continue
            records.append(
                (
                    _snapshot_element_priority(
                        element,
                        host_rect,
                        scope_index,
                        scope_visible_count,
                        scope_total_count,
                    ),
                    element,
                )
            )

    page_part = page_snapshot.get("page", {}) if isinstance(page_snapshot.get("page", {}), dict) else {}
    page_visible_count, page_total_count = _snapshot_interactive_counts(page_part)
    _add_elements(page_part.get("interactive_elements", []), page_part.get("host_rect", {}), 0, page_visible_count, page_total_count)

    for frame_index, frame in enumerate(_ordered_snapshot_frames(page_snapshot), start=1):
        frame_visible_count, frame_total_count = _snapshot_interactive_counts(frame)
        _add_elements(
            frame.get("interactive_elements", []),
            frame.get("host_rect", {}),
            frame_index,
            frame_visible_count,
            frame_total_count,
        )

    records.sort(key=lambda item: item[0], reverse=True)
    return [element for _, element in records]


def _collect_visible_snapshot_click_candidates(page_snapshot: Dict[str, Any]) -> List[str]:
    if not isinstance(page_snapshot, dict) or not page_snapshot:
        return []

    candidates: List[str] = []
    for element in _ordered_snapshot_elements(page_snapshot, visible_only=True):
        for candidate in _derive_target_text_candidates_v2(
            element.get("text", ""),
            element.get("aria_label", ""),
            element.get("title", ""),
            element.get("id", ""),
            element.get("name", ""),
            element.get("role", ""),
            element.get("href", ""),
            element.get("type", ""),
        ):
            if candidate and candidate not in candidates:
                candidates.append(candidate)

    return candidates[:24]


def _attach_debug_artifacts(result: Dict[str, Any], image_file: str, page_snapshot: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if not isinstance(result, dict):
        return result

    artifacts = result.get("DebugArtifacts", {})
    if not isinstance(artifacts, dict):
        artifacts = {}

    artifacts["screenshot_path"] = image_file
    if page_snapshot and isinstance(page_snapshot, dict):
        page_part = page_snapshot.get("page", {}) if isinstance(page_snapshot.get("page", {}), dict) else {}
        if page_part.get("html_path"):
            artifacts["page_html_path"] = page_part.get("html_path")
        if page_part.get("html_excerpt"):
            artifacts["page_html_excerpt"] = page_part.get("html_excerpt")
        if page_part.get("text_excerpt"):
            artifacts["page_text_excerpt"] = page_part.get("text_excerpt")
        if page_part.get("summary"):
            artifacts["page_summary"] = page_part.get("summary")
        if page_snapshot.get("frames"):
            artifacts["frame_count"] = page_snapshot.get("frame_count", 0)
            frame_debug: List[Dict[str, Any]] = []
            for frame in page_snapshot.get("frames", [])[:4]:
                if not isinstance(frame, dict):
                    continue
                frame_debug.append(
                    {
                        "index": frame.get("index", 0),
                        "name": frame.get("name", ""),
                        "url": frame.get("url", ""),
                        "html_path": frame.get("html_path", ""),
                        "summary": frame.get("summary", {}),
                    }
                )
            artifacts["frame_snapshots"] = frame_debug

    result["DebugArtifacts"] = artifacts
    if page_snapshot:
        result["PageSnapshot"] = page_snapshot
    return result


def _collect_browser_hint_candidates(browser_note: Optional[Dict[str, Any]]) -> List[str]:
    if not isinstance(browser_note, dict) or not browser_note:
        return []

    candidates: List[str] = []
    page_state = browser_note.get("page_state", {})
    if isinstance(page_state, dict):
        for candidate in page_state.get("visible_candidates", []):
            candidate_text = str(candidate or "").strip()
            if candidate_text and candidate_text not in candidates:
                candidates.append(candidate_text)

        target_visible_text = str(page_state.get("target_visible_text", "") or "").strip()
        if target_visible_text and target_visible_text not in candidates:
            candidates.append(target_visible_text)

    snapshot_source = browser_note.get("page_snapshot_raw", {})
    if not isinstance(snapshot_source, dict) or not snapshot_source:
        snapshot_source = browser_note.get("page_snapshot", {})

    for candidate in _collect_visible_snapshot_click_candidates(snapshot_source):
        if candidate and candidate not in candidates:
            candidates.append(candidate)

    return candidates[:24]


_CORE_RESULT_KEYS = (
    "HealStatus",
    "HealMessage",
    "NewSelector",
    "RecoveredStep",
    "NeedHuman",
    "ActionPlan",
    "Verification",
    "VerificationResult",
)

_META_RESULT_KEYS = (
    "CurrentURL",
    "CorrelationId",
    "FailedActionName",
    "FailedActionSource",
    "FailedActionLogPath",
)


def _finalize_result(
    result: Dict[str, Any],
    image_file: str,
    page_snapshot: Optional[Dict[str, Any]] = None,
    include_debug: bool = False,
) -> Dict[str, Any]:
    if not isinstance(result, dict):
        return result

    final_result: Dict[str, Any] = {}
    for key in _CORE_RESULT_KEYS:
        if key == "HealStatus":
            final_result[key] = result.get(key, "FAILED") or "FAILED"
        elif key == "HealMessage":
            final_result[key] = result.get(key, "") or ""
        elif key == "NewSelector":
            final_result[key] = result.get(key, "") or ""
        elif key == "RecoveredStep":
            final_result[key] = result.get(key, "") or ""
        elif key == "NeedHuman":
            final_result[key] = bool(result.get(key, True))
        elif key == "ActionPlan":
            action_plan = result.get(key, [])
            final_result[key] = action_plan if isinstance(action_plan, list) else []
        elif key == "Verification":
            verification = result.get(key, {})
            final_result[key] = verification if isinstance(verification, dict) else {}
        elif key == "VerificationResult":
            verification_result = result.get(key, {})
            final_result[key] = verification_result if isinstance(verification_result, dict) else {}

    final_result["TechnicalDetail"] = result.get("TechnicalDetail", "") or ""
    execution_log = result.get("ExecutionLog", [])
    final_result["ExecutionLog"] = execution_log if isinstance(execution_log, list) else []
    for key in _META_RESULT_KEYS:
        value = result.get(key, "")
        final_result[key] = "" if value is None else value

    if include_debug:
        final_result = _attach_debug_artifacts(final_result, image_file, page_snapshot)
    else:
        final_result["TechnicalDetail"] = ""

    return final_result


def _extract_json_object(text: str) -> Dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(cleaned[start : end + 1])
        raise


def _normalize_status(value: Optional[str]) -> str:
    normalized = str(value or "").strip().upper()
    return normalized if normalized in {"SUCCESS", "FAILED", "TIMEOUT"} else "FAILED"


def _normalize_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def _allow_openai_fallback() -> bool:
    # Customer deployment is restricted to the internal AI gateway.
    return False


async def _safe_page_title(page: Optional[Page], default: str = "") -> str:
    if not page:
        return default
    try:
        if page.is_closed():
            return default
    except Exception:
        pass
    try:
        return await page.title()
    except Exception:
        return default


def _looks_like_manual_verification(text: str) -> bool:
    lowered = text.lower()
    return any(token in lowered for token in ("captcha", "mfa", "two-factor", "verification required", "human verification"))


def _normalize_action_text(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""

    text = text.replace(" ", "")
    text = re.sub(r"^[A-Za-z0-9]+-\s*", "", text)
    text = re.sub(r"^(點選|點擊|進入|選擇|選取|按下|開啟|展開|切換到|前往|執行|選用|點開)", "", text)
    text = text.strip(" -_：:()（）[]【】<>")
    return text.strip()


def _derive_target_text_candidates_v2(*values: Any) -> List[str]:
    candidates: List[str] = []
    for raw in values:
        text = str(raw or "").strip()
        if not text:
            continue

        normalized = _normalize_action_text(text)
        base_candidates = [text, normalized]
        for candidate in base_candidates:
            candidate = str(candidate or "").strip()
            if candidate and candidate not in candidates:
                candidates.append(candidate)

        if normalized:
            parts = re.split(r"[>/｜|、,，]", normalized)
            for part in parts:
                part = part.strip()
                if part and part not in candidates:
                    candidates.append(part)

    return candidates


def _step_match_score(failed_action_name: str, correlation_id: str, step_text: str, step_button: str, step_action: str) -> int:
    failed_action_name = str(failed_action_name or "").strip()
    correlation_id = str(correlation_id or "").strip()
    step_text = str(step_text or "").strip()
    step_button = str(step_button or "").strip()
    step_action = str(step_action or "").strip().lower()

    failed_norm = _normalize_action_text(failed_action_name)
    step_norm = _normalize_action_text(step_text)
    button_norm = _normalize_action_text(step_button)

    score = 0

    if failed_action_name == step_text:
        score += 1000
    if failed_action_name == step_button:
        score += 1000

    if failed_norm and failed_norm == step_norm:
        score += 900
    if failed_norm and failed_norm == button_norm:
        score += 900

    if failed_norm and button_norm and button_norm in failed_norm:
        score += 600
    if failed_norm and button_norm and failed_norm in button_norm:
        score += 400

    if failed_action_name and step_text and step_text in failed_action_name:
        score += 350
    if failed_action_name and step_button and step_button in failed_action_name:
        score += 350

    if step_action == "click" and button_norm and button_norm in failed_norm:
        score += 150

    if correlation_id and failed_action_name.startswith(correlation_id):
        score += 80

    if correlation_id and correlation_id in step_text:
        score += 20

    return score


# 頁面比對與選頁工具
def _normalize_url_parts(raw_url: str) -> Tuple[str, str, str]:
    parsed = urlparse((raw_url or "").strip())
    host = (parsed.netloc or "").lower()
    path = (parsed.path or "/").rstrip("/") or "/"
    if path != "/" and not path.startswith("/"):
        path = f"/{path}"
    return parsed.scheme.lower(), host, path


def _page_match_score(current_url: str, candidate_url: str) -> int:
    current_scheme, current_host, current_path = _normalize_url_parts(current_url)
    candidate_scheme, candidate_host, candidate_path = _normalize_url_parts(candidate_url)

    if not current_host or not candidate_host:
        return 0
    if current_host != candidate_host:
        return 0

    score = 100
    if current_scheme and candidate_scheme and current_scheme == candidate_scheme:
        score += 10
    if candidate_path == current_path:
        score += 1000
    elif candidate_path.startswith(current_path + "/"):
        score += 700 + min(len(current_path), 120)
    elif current_path.startswith(candidate_path + "/"):
        score += 500 + min(len(candidate_path), 120)
    elif current_path in candidate_path or candidate_path in current_path:
        score += 200

    return score


def _is_placeholder_current_url(value: Any) -> bool:
    normalized = str(value or "").strip().lower()
    return normalized in {"", "n/a", "na", "none", "null", "about:blank", "chrome://newtab/", "chrome://new-tab-page/"}


def _text_match_score(expected: str, actual: str) -> int:
    expected = str(expected or "").strip().lower()
    actual = str(actual or "").strip().lower()
    if not expected or not actual:
        return 0
    if expected == actual:
        return 1000
    if expected in actual or actual in expected:
        return 300
    return 0


def _latest_robin_robot_log_path() -> str:
    log_dir = os.getenv("AI_ROBIN_LOG_DIR", r"C:\ProgramData\Microsoft\Power Automate\Logs")
    try:
        if not os.path.isdir(log_dir):
            return ""

        candidates: List[str] = []
        for name in os.listdir(log_dir):
            if name.lower().endswith("robinrobot.log"):
                path = os.path.join(log_dir, name)
                if os.path.isfile(path):
                    candidates.append(path)

        if not candidates:
            return ""
        return max(candidates, key=lambda path: os.path.getmtime(path))
    except Exception:
        return ""


def _read_tail_lines(file_path: str, limit: int = 800) -> List[str]:
    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            return list(deque(f, maxlen=limit))
    except Exception:
        return []


def _summarize_robin_selector_info(selector_info: Any) -> str:
    if not isinstance(selector_info, list):
        return ""

    parts: List[str] = []
    for item in selector_info[:2]:
        if not isinstance(item, dict):
            continue

        tag = str(item.get("targetTag", "") or "").strip()
        tags_used = item.get("tagsUsed", [])
        attrs_used = item.get("attributesUsed", [])
        item_parts: List[str] = []

        if tag:
            item_parts.append(tag)
        if isinstance(tags_used, list):
            cleaned_tags = [str(tag_item).strip() for tag_item in tags_used if str(tag_item).strip()]
            if cleaned_tags:
                item_parts.append("+".join(cleaned_tags[:2]))
        if isinstance(attrs_used, list):
            cleaned_attrs = [str(attr_item).strip() for attr_item in attrs_used if str(attr_item).strip()]
            if cleaned_attrs:
                item_parts.append("attrs=" + ",".join(cleaned_attrs[:2]))

        if item_parts:
            parts.append("/".join(item_parts))

    return " | ".join(parts)


def _classify_robin_failure_reason(exception_type: str, message: str) -> str:
    text = f"{exception_type} {message}".lower()
    if not text.strip():
        return ""

    if "elementnotfound" in text or "element not found" in text or "target not found" in text:
        return "element_not_found"
    if "timeout" in text or "timed out" in text:
        return "timeout"
    if "permission" in text or "access denied" in text or "unauthorized" in text:
        return "permission_denied"
    if "dialog" in text or "popup" in text or "modal" in text or "overlay" in text:
        return "dialog_blocked"
    if "cdp" in text or "devtools" in text:
        return "cdp_connection_issue"

    if exception_type:
        short = exception_type.split(".")[-1]
        short = re.sub(r"Exception$", "", short, flags=re.IGNORECASE)
        short = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", short)
        short = re.sub(r"[^0-9A-Za-z_]+", "_", short).strip("_")
        if short:
            return short.lower()

    return "runtime_error"


def _workflow_key_from_text(text: str) -> str:
    value = str(text or "").strip()
    if not value:
        return ""

    match = re.search(r"\b[A-Za-z][A-Za-z0-9_-]{1,31}\b", value)
    if not match:
        return ""

    token = match.group(0).strip()
    if not token:
        return ""

    return token


def _infer_failed_action_from_robin_log(correlation_id: str) -> Tuple[str, str]:
    log_path = _latest_robin_robot_log_path()
    if not log_path:
        return "", ""

    for line in reversed(_read_tail_lines(log_path)):
        text = line.strip()
        if not text.startswith("{"):
            continue

        try:
            entry = json.loads(text)
        except Exception:
            continue

        event = str(entry.get("event", "") or "").lower()
        trace_level = str(entry.get("traceLevel", "") or "").lower()
        event_data = entry.get("eventData") or {}
        if not isinstance(event_data, dict):
            continue

        robin_action = event_data.get("robinActionInfo") or {}
        if not isinstance(robin_action, dict):
            continue

        action = str(robin_action.get("action", "") or "").strip()
        runtime_info = event_data.get("webAutomationRuntimeInfo") or {}
        if not isinstance(runtime_info, dict):
            runtime_info = {}

        runtime_action = str(runtime_info.get("action", "") or "").strip()
        selector_info = _summarize_robin_selector_info(runtime_info.get("selectorInfo"))
        exception_type = str(entry.get("exceptionType", "") or "").strip()
        message = str(entry.get("message", "") or "").strip()

        if "exception" not in event and trace_level != "error" and not exception_type:
            continue
        if not (action or runtime_action or selector_info or exception_type):
            continue

        parts: List[str] = []
        if correlation_id:
            parts.append(correlation_id)

        action_piece = action or runtime_action
        if action_piece:
            if runtime_action and runtime_action != action_piece:
                action_piece = f"{action_piece}/{runtime_action}"
            parts.append(action_piece)

        if selector_info:
            parts.append(selector_info)
        failure_reason = _classify_robin_failure_reason(exception_type, message)
        if failure_reason:
            parts.append(failure_reason)

        return " - ".join(parts), log_path

    return "", log_path


def _build_workflow_context(
    context: Dict[str, Any],
    browser_note: Optional[Dict[str, Any]] = None,
    context_file: str = "",
) -> Dict[str, Any]:
    correlation_id = str(context.get("CorrelationId", "") or "").strip()
    failed_action_name = str(context.get("FailedActionName", "") or "").strip()
    failed_action_source = "context" if failed_action_name else "none"
    robin_log_path = ""
    if not failed_action_name:
        inferred_action_name, robin_log_path = _infer_failed_action_from_robin_log(correlation_id)
        if inferred_action_name:
            failed_action_name = inferred_action_name
            failed_action_source = "robin_log"

    if not correlation_id:
        correlation_id = _workflow_key_from_text(failed_action_name)

    failed_action_candidates = _derive_target_text_candidates_v2(failed_action_name, correlation_id)
    browser_hint_candidates = _collect_browser_hint_candidates(browser_note)
    failure_reason = _classify_robin_failure_reason(failed_action_name, "")

    if not WORKFLOW_GUIDES:
        notes = [
            "Log-first diagnosis mode is enabled for this test run.",
            "Use CurrentURL, ScreenshotPath, browser state, and RobinRobot.log as the primary recovery signals.",
            "Do not rely on a prebuilt workflow guide; infer recovery from the current page state and logs.",
        ]
        return {
            "correlation_id": correlation_id,
            "workflow_name": "",
            "entry_point": "",
            "failed_action_name": failed_action_name,
            "failed_action_source": failed_action_source,
            "failed_action_log_path": robin_log_path,
            "failed_action_candidates": failed_action_candidates,
            "current_step": {},
            "current_step_action": "",
            "current_step_button": "",
            "current_step_is_menu_click": False,
            "next_step": {},
            "steps": [],
            "notes": notes,
        }

    guide = WORKFLOW_GUIDES.get(correlation_id, {})
    if not guide:
        fallback_key = _workflow_key_from_text(failed_action_name)
        if fallback_key:
            guide = WORKFLOW_GUIDES.get(fallback_key, {})
            if guide and not correlation_id:
                correlation_id = fallback_key
    steps = guide.get("steps", [])

    current_index = -1
    browser_visible_index = -1
    if failure_reason == "element_not_found" and browser_hint_candidates:
        for index, step in enumerate(steps):
            step_text = str(step.get("step", "") or "").strip()
            step_button = str(step.get("button", "") or "").strip()
            if any(
                _text_match_score(candidate, step_text) > 0 or _text_match_score(candidate, step_button) > 0
                for candidate in browser_hint_candidates
            ):
                browser_visible_index = index
                break

    if browser_visible_index >= 0:
        current_index = browser_visible_index
    elif failed_action_name:
        best_score = -1
        for index, step in enumerate(steps):
            step_text = str(step.get("step", "") or "").strip()
            step_button = str(step.get("button", "") or "").strip()
            step_action = str(step.get("action", "") or "").strip().lower()
            step_score = _step_match_score(failed_action_name, correlation_id, step_text, step_button, step_action)
            if browser_hint_candidates:
                for candidate in browser_hint_candidates:
                    candidate = str(candidate or "").strip()
                    if candidate and candidate in step_text:
                        step_score += 120
                    if candidate and candidate in step_button:
                        step_score += 220
            if failed_action_candidates:
                for candidate in failed_action_candidates:
                    candidate = str(candidate or "").strip()
                    if candidate and candidate in step_text:
                        step_score += 25
                    if candidate and candidate in step_button:
                        step_score += 50
            if step_score > best_score:
                best_score = step_score
                current_index = index

    next_step = steps[0] if steps else {}
    if 0 <= current_index < len(steps) - 1:
        next_step = steps[current_index + 1]

    notes = [
        "Log-first diagnosis mode is enabled.",
        "If FailedActionName is missing, infer the current step from the screenshot and browser state before making a recovery decision.",
        "If the failure hint comes from RobinRobot.log, treat it as a short technical clue such as element_not_found or timeout, not the final business step name. Do not copy raw exception class names or stack traces into the answer.",
        "If browser_note exposes visible candidates or visible snapshot elements, prefer the earliest visible target over log-derived later steps.",
        "If the page already shows a later state, continue from that later state instead of repeating earlier clicks.",
        "Use safe visible-page recovery only; do not invent targets that are not present in the current page state.",
        "If the page is still on the menu layer, the next safe action is usually the click itself or a bounded 10-20 second wait, not human escalation.",
    ]

    return {
        "correlation_id": correlation_id,
        "workflow_name": guide.get("workflow_name", ""),
        "entry_point": guide.get("entry_point", ""),
        "failed_action_name": failed_action_name,
        "failed_action_source": failed_action_source,
        "failed_action_log_path": robin_log_path,
        "failed_action_candidates": failed_action_candidates,
        "current_step": steps[current_index] if 0 <= current_index < len(steps) else {},
        "current_step_action": str((steps[current_index] or {}).get("action", "")).strip() if 0 <= current_index < len(steps) else "",
        "current_step_button": str((steps[current_index] or {}).get("button", "")).strip() if 0 <= current_index < len(steps) else "",
        "current_step_is_menu_click": bool(
            0 <= current_index < len(steps)
            and str((steps[current_index] or {}).get("action", "")).strip().lower() == "click"
        ),
        "next_step": next_step,
        "steps": steps,
        "notes": notes,
    }


# AI 與 log 的設定
def _resolve_api_settings() -> Dict[str, str]:
    api_key = os.getenv("AI_API_KEY", "").strip() or os.getenv("OPENAI_API_KEY", "").strip()
    # Customer deployment is restricted to the internal AI gateway.
    base_url = "http://172.22.8.15:8080"
    model = os.getenv("AI_MODEL", "").strip() or os.getenv("OPENAI_MODEL", "").strip() or "gpt-5.4-nano"
    system_name = os.getenv("AI_SYSTEM_NAME", "").strip() or "PAD_Self_Healing_System"
    vision_detail = os.getenv("AI_VISION_DETAIL", "").strip() or "auto"

    return {
        "api_key": api_key,
        "base_url": base_url.rstrip("/"),
        "model": model,
        "system_name": system_name,
        "vision_detail": vision_detail,
    }


def _secret_fingerprint(value: str) -> str:
    if not value:
        return ""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _truncate_text(value: Any, limit: int = 400) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _exception_diagnostics(exc: BaseException) -> Dict[str, Any]:
    """Return safe, detailed exception data without logging secrets or payloads."""
    chain: List[Dict[str, Any]] = []
    seen: set[int] = set()
    current: Optional[BaseException] = exc

    while current is not None and id(current) not in seen and len(chain) < 8:
        seen.add(id(current))
        entry: Dict[str, Any] = {
            "type": type(current).__name__,
            "message": _truncate_text(str(current), 1200),
            "repr": _truncate_text(repr(current), 1200),
        }
        if isinstance(current, OSError):
            entry["errno"] = getattr(current, "errno", None)
            entry["winerror"] = getattr(current, "winerror", None)
            entry["strerror"] = _truncate_text(getattr(current, "strerror", ""), 500)
        if isinstance(current, httpx.RequestError):
            request = getattr(current, "request", None)
            if request is not None:
                entry["request_method"] = getattr(request, "method", "")
                entry["request_url"] = _truncate_text(str(getattr(request, "url", "")), 300)
        chain.append(entry)
        current = current.__cause__ or current.__context__

    summary = _truncate_text(str(exc), 1200)
    if not summary:
        for entry in chain[1:]:
            candidate = str(entry.get("message", "") or "").strip()
            if candidate:
                summary = f"{entry.get('type', 'Exception')}: {candidate}"
                break
    if not summary:
        summary = _truncate_text(repr(exc), 1200) or type(exc).__name__

    return {
        "summary": summary,
        "exception_type": type(exc).__name__,
        "exception_message": _truncate_text(str(exc), 1200),
        "exception_repr": _truncate_text(repr(exc), 1200),
        "exception_chain": chain,
    }


def _http_transport_diagnostics(trust_env: bool) -> Dict[str, Any]:
    """Describe HTTP transport configuration without logging proxy credentials."""
    return {
        "httpx_version": getattr(httpx, "__version__", ""),
        "trust_env": trust_env,
        "http_proxy_configured": bool(os.getenv("HTTP_PROXY", "").strip() or os.getenv("http_proxy", "").strip()),
        "https_proxy_configured": bool(os.getenv("HTTPS_PROXY", "").strip() or os.getenv("https_proxy", "").strip()),
        "all_proxy_configured": bool(os.getenv("ALL_PROXY", "").strip() or os.getenv("all_proxy", "").strip()),
        "no_proxy_configured": bool(os.getenv("NO_PROXY", "").strip() or os.getenv("no_proxy", "").strip()),
    }


def _bridge_log_path() -> str:
    env_bridge_log = os.getenv("AI_BRIDGE_LOG_PATH", "").strip()
    if env_bridge_log:
        return env_bridge_log

    artifact_dir = os.getenv("AI_ARTIFACT_DIR", "").strip()
    if artifact_dir:
        return os.path.join(artifact_dir, "bridge.log")

    return os.path.join(os.getcwd(), "bridge.log")


def _append_bridge_log(event: str, **fields: Any) -> None:
    try:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": event,
            **fields,
        }
        log_path = _bridge_log_path()
        log_dir = os.path.dirname(log_path)
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")))
            f.write("\n")
    except Exception:
        pass


# 原子性寫入 result.json，避免 PAD 讀到半截 JSON。
def _write_json_atomic(target_path: str, data: Dict[str, Any]) -> None:
    directory = os.path.dirname(target_path)
    if directory:
        os.makedirs(directory, exist_ok=True)

    temp_path = f"{target_path}.tmp"
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    with open(temp_path, "w", encoding="utf-8") as f:
        f.write(payload)
    os.replace(temp_path, target_path)


# 瀏覽器頁面檢視工具
async def _summarize_pages(browser: Browser) -> List[Dict[str, Any]]:
    summary: List[Dict[str, Any]] = []
    for ctx_index, ctx in enumerate(browser.contexts):
        for page_index, page in enumerate(ctx.pages):
            title = await _safe_page_title(page)
            summary.append(
                {
                    "context_index": ctx_index,
                    "page_index": page_index,
                    "url": _truncate_text(page.url, 220),
                    "title": _truncate_text(title, 120),
                }
            )
    return summary


# 組出要送給 AI 的 prompt。
# 內容包含當前失敗資訊、瀏覽器狀態，以及已知的流程導引。
def _build_prompt_text(context: Dict[str, Any], browser_note: Dict[str, Any], context_file: str = "") -> str:
    workflow_context = _build_workflow_context(context, browser_note, context_file=context_file)
    effective_current_url = _resolve_effective_current_url(context, browser_note)
    popup_only_test = str(os.getenv("AI_POPUP_ONLY_TEST", "")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    prompt_payload = {
        "current_url": _truncate_text(effective_current_url, 200),
        "screenshot_path": _truncate_text(context.get("ScreenshotPath", ""), 200),
        "browser_note": {
            "connected": bool(browser_note.get("connected", False)),
            "page_count": browser_note.get("page_count", 0),
            "matched_page_url": _truncate_text(browser_note.get("matched_page_url", ""), 200),
            "matched_page_title": _truncate_text(browser_note.get("matched_page_title", ""), 120),
            "target_url_hint": _truncate_text(browser_note.get("target_url_hint", ""), 200),
            "target_title_hint": _truncate_text(browser_note.get("target_title_hint", ""), 120),
            "page_state": browser_note.get("page_state", {}),
            "pages": browser_note.get("pages", []),
            "page_snapshot": browser_note.get("page_snapshot", {}),
        },
        "workflow_context": workflow_context,
        "popup_only_test": popup_only_test,
        "allowed_actions": sorted(ALLOWED_ACTIONS),
        "output_schema": {
            "HealStatus": "SUCCESS|FAILED|TIMEOUT",
            "HealMessage": "用繁體中文直接說明錯在哪、可能原因、採取的修復，以及驗證結果",
            "TechnicalDetail": "diagnostic detail",
            "NewSelector": "selector string if available",
            "RecoveredStep": "recovered step name if available",
            "NeedHuman": "boolean",
            "ActionPlan": [
                {
                    "action": "click|acceptdialog|fill|press|selectoption|wait|scroll|refresh|goback|openurl|switchtab|closetab|hover",
                    "target": "selector or text",
                    "value": "optional value",
                    "ms": 0,
                    "notes": "optional note",
                }
            ],
            "Verification": {
                "type": "url_contains|url_equals|title_contains|selector_visible|text_visible|page_ready|dialog_accepted|action_executed",
                "value": "verification target",
            },
        },
        "instructions": [
        "Return only JSON.",
        "Keep HealStatus SUCCESS only when the recovery is complete.",
        "Use ActionPlan only for safe allowlisted actions.",
        "Prefer automatic recovery first. Only set NeedHuman to true when the current page state cannot be safely recovered by the allowed actions.",
        "If the issue requires a human, set NeedHuman to true and HealStatus to FAILED.",
        "Use log-first diagnosis: rely on CurrentURL, ScreenshotPath, browser state, and RobinRobot.log before making a recovery decision.",
        "Use the screenshot as the primary signal for the current UI state. If the screenshot and page_state disagree, trust the screenshot more than the probes.",
            "When the screenshot suggests a menu or report area, attempt one safe reroute based on the visible page state before asking for human help.",
            "When multiple visible targets look plausible, prefer the one that sits inside the denser interactive region of the screen instead of a sparse global header area, unless the screenshot clearly points elsewhere.",
            "If the current page still looks like it is loading, prefer a safe wait or a single retry before escalating.",
            "Treat browser_note.page_state as a helpful hint, not a hard gate. If visible_candidate_count is low or target_visible is null or false, that only means the probe was inconclusive, not that the step must fail.",
            "If browser_note.page_state.target_visible is true, prefer an ActionPlan that clicks the visible target and then waits for the next state instead of returning NeedHuman.",
            "If browser_note.page_state.overlay_present is true, try Escape or a dismiss control before giving up, then continue with the next workflow step if the overlay clears.",
            "If browser_note.page_state is inconclusive but browser_note.page_snapshot still shows a likely target, try one click using the snapshot candidates before returning NeedHuman.",
            "Do not set NeedHuman only because overlay_present is true or target_visible is null; these probes can be inconclusive. If the screenshot or page snapshot supports a bounded safe ActionPlan and a real verification rule, execute that plan and let verification decide.",
            "When a safe ActionPlan is available, prefer HealStatus SUCCESS with NeedHuman false; reserve FAILED/NeedHuman for missing page evidence, unsafe actions, or no verifiable recovery path.",
            "Prefer the control whose visual position on the screenshot best matches the intended action, then map that visual target back to the DOM element.",
            "Do not require exact text equality between the step name and the target; normalize action verbs such as 點選, 點擊, 進入, 選擇, 按下, 開啟, 展開, 前往, 執行, then match the remaining target text.",
            "For a browser-native JavaScript alert, use acceptdialog instead of clicking a DOM selector; the dialog may not exist in the page DOM.",
        "When the recovery objective is only to close a browser-native JavaScript alert, use Verification type dialog_accepted. Do not require the original business action to succeed for that popup-only test.",
            "If popup_only_test is true, test only whether the visible native JavaScript alert can be accepted: use acceptdialog and Verification type dialog_accepted, without filling fields or continuing the business workflow.",
            "If the screenshot or page state already shows the expected post-recovery state, return an empty ActionPlan with a concrete URL, title, selector, text, or page_ready Verification; the runtime will verify it before taking any action.",
            "For any workflow step, if the expected target is not yet visible or page_settled is false, prefer a bounded 10-20 second wait and re-check before declaring failure.",
            "Do not let a missing probe value alone block a trial click when the screenshot still looks like the correct menu area.",
            "Do not treat a missing target as a hard failure until at least one safe wait/retry has been considered.",
            "Do not jump to unrelated recovery ideas if the current workflow step can still be completed.",
            "If the page is already past the failed action, resume from the earliest unfinished step that still matches the visible UI state.",
            "If the page already shows a later UI state, continue from that state instead of repeating earlier clicks.",
            "For a loading or still-running page, use a wait action between 10 and 20 seconds. If ms is omitted, use 10000. Do not wait longer than 20000 ms in one action.",
            "Prefer a single direct click or one bounded wait over repeated retry loops when the target is already visible.",
            "When the exact button text is not present, judge the intended control by semantic meaning and nearby context. Use visible text, aria-label, title, role, and nearby labels when they clearly refer to the same UI target.",
            "Do not require an exact word-for-word match if the screenshot or page structure clearly points to the same control.",
        ],
    }
    return "Analyze the failure and return only JSON.\n" + json.dumps(prompt_payload, ensure_ascii=False, separators=(",", ":"))


# 執行前先標準化並檢查 AI 的動作計畫。
def _coerce_action_plan(raw_plan: Any) -> List[Dict[str, Any]]:
    if not isinstance(raw_plan, list):
        return []

    normalized: List[Dict[str, Any]] = []
    for item in raw_plan[:12]:
        if not isinstance(item, dict):
            continue

        action = str(item.get("action", "")).strip().lower()
        if action not in ALLOWED_ACTIONS:
            continue

        normalized.append(
            {
                "action": action,
                "target": str(item.get("target", "")).strip(),
                "value": item.get("value", ""),
                "ms": item.get("ms", 0),
                "notes": _truncate_text(item.get("notes", ""), 200),
            }
        )

    return normalized


def _can_run_soft_recovery_plan(
    ai_result: Dict[str, Any],
    browser_note: Dict[str, Any],
    image_file: str,
) -> bool:
    """Allow a safe, verifiable plan even when the model confidence is low."""
    action_plan = _coerce_action_plan(ai_result.get("ActionPlan", []))
    if not action_plan or any(step.get("action") not in SOFT_RECOVERY_ACTIONS for step in action_plan):
        return False

    verification = ai_result.get("Verification")
    if not isinstance(verification, dict):
        return False
    verification_type = str(verification.get("type", "") or "").strip().lower()
    if verification_type not in SOFT_RECOVERY_VERIFICATIONS:
        return False
    if verification_type != "page_ready" and not str(verification.get("value", "") or "").strip():
        return False

    if not bool(browser_note.get("connected")):
        return False

    page_state = browser_note.get("page_state")
    page_snapshot = browser_note.get("page_snapshot")
    has_page_evidence = bool(
        isinstance(page_state, dict)
        and (
            page_state.get("visible_candidates")
            or page_state.get("target_candidates")
            or page_state.get("dismiss_candidates")
            or page_state.get("page_settled")
        )
    ) or bool(isinstance(page_snapshot, dict) and page_snapshot)
    return has_page_evidence or os.path.isfile(image_file)


# 呼叫 AI 取得復原決策，並解析回傳的 JSON。
async def _call_openai_ai(
    context: Dict[str, Any],
    image_file: str,
    browser_note: Dict[str, Any],
    context_file: str = "",
) -> Dict[str, Any]:
    api_settings = _resolve_api_settings()
    api_key = api_settings["api_key"]
    if not api_key:
        raise RuntimeError("AI_API_KEY or OPENAI_API_KEY environment variable is not set.")

    model = api_settings["model"]
    base_url = api_settings["base_url"]
    system_name = api_settings["system_name"]
    vision_detail = api_settings["vision_detail"]
    image_data_url = _read_image_as_data_url(image_file)
    use_internal_api = not base_url.startswith("https://api.openai.com")
    prompt_text = _build_prompt_text(context, browser_note, context_file=context_file)
    effective_current_url = _resolve_effective_current_url(context, browser_note)

    _append_bridge_log(
        "ai_request_prepared",
        mode="internal" if use_internal_api else "openai",
        endpoint=(f"{base_url}/ai_api_platform_API/api/v1/vision" if use_internal_api else f"{base_url}/chat/completions"),
        model=model,
        system_name=system_name,
        vision_detail=vision_detail,
        prompt_length=len(prompt_text),
        current_url=_truncate_text(effective_current_url, 200),
        api_key_fingerprint=_secret_fingerprint(api_key),
        transport=_http_transport_diagnostics(trust_env=False),
    )

    system_prompt = (
        "You are a Power Automate Desktop self-healing assistant. "
        "Return only valid JSON. Do not use Markdown or code fences. "
        "Follow the provided schema exactly. "
        "Write HealMessage and TechnicalDetail in Traditional Chinese (繁體中文). "
        "HealMessage must directly explain which step failed, the likely cause, what recovery was attempted, and whether it was verified. "
        "If recovery is not safe, set NeedHuman to true and keep ActionPlan empty. "
        "Use semantic matching for labels and buttons; treat obvious synonyms or near-synonyms as equivalent when the page evidence supports it."
    )

    if use_internal_api:
        payload = {
            "model": model,
            "text": prompt_text,
            "image": image_data_url,
            "detail": vision_detail,
            "temperature": 0,
            "top_p": 1.0,
            "systemname": system_name,
        }
        headers = {"X-API-Key": api_key, "Content-Type": "application/json"}
        endpoint = f"{base_url}/ai_api_platform_API/api/v1/vision"
    else:
        payload = {
            "model": model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": prompt_text,
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": image_data_url, "detail": vision_detail},
                        },
                    ],
                },
            ],
        }
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        endpoint = f"{base_url}/chat/completions"

    request_started = time.monotonic()
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0), trust_env=False) as client:
        try:
            response = await client.post(endpoint, headers=headers, json=payload)
        except Exception as exc:
            diagnostics = _exception_diagnostics(exc)
            _append_bridge_log(
                "ai_transport_failed",
                endpoint=endpoint,
                elapsed_ms=round((time.monotonic() - request_started) * 1000, 1),
                **_http_transport_diagnostics(trust_env=False),
                **diagnostics,
            )
            raise
        primary_response = response
        fallback_error = ""
        _append_bridge_log(
            "ai_response_received",
            endpoint=endpoint,
            status_code=response.status_code,
            reason_phrase=response.reason_phrase,
            is_error=response.is_error,
            response_preview=_truncate_text(response.text, 800),
        )

        if response.is_error and use_internal_api:
            openai_api_key = os.getenv("OPENAI_API_KEY", "").strip()
            if not _allow_openai_fallback():
                _append_bridge_log("ai_fallback_skipped", reason="disabled_by_default")
            elif not openai_api_key:
                _append_bridge_log("ai_fallback_skipped", reason="missing_openai_api_key")
            else:
                _append_bridge_log(
                    "ai_fallback_attempt",
                    fallback_endpoint="https://api.openai.com/v1/chat/completions",
                    fallback_model=model,
                    fallback_detail="low",
                )
                fallback_payload = {
                    "model": model,
                    "temperature": 0,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "text",
                                    "text": prompt_text,
                                },
                                {
                                    "type": "image_url",
                                    "image_url": {"url": image_data_url, "detail": "low"},
                                },
                            ],
                        },
                    ],
                }
                fallback_headers = {"Authorization": f"Bearer {openai_api_key}", "Content-Type": "application/json"}
                fallback_endpoint = "https://api.openai.com/v1/chat/completions"
                try:
                    response = await client.post(fallback_endpoint, headers=fallback_headers, json=fallback_payload)
                    _append_bridge_log(
                        "ai_fallback_response_received",
                        endpoint=fallback_endpoint,
                        status_code=response.status_code,
                        reason_phrase=response.reason_phrase,
                        is_error=response.is_error,
                        response_preview=_truncate_text(response.text, 800),
                    )
                except Exception as exc:
                    fallback_diagnostics = _exception_diagnostics(exc)
                    fallback_error = fallback_diagnostics["summary"]
                    _append_bridge_log(
                        "ai_fallback_failed",
                        endpoint=fallback_endpoint,
                        **_http_transport_diagnostics(trust_env=False),
                        **fallback_diagnostics,
                    )
                    response = primary_response

        if response.is_error:
            error_text = (
                f"AI API request failed with HTTP {response.status_code} {response.reason_phrase}: "
                f"{_truncate_text(response.text, 1000)}"
            )
            if fallback_error:
                error_text += f" Fallback to OpenAI also failed: {fallback_error}"
            _append_bridge_log(
                "ai_request_failed",
                endpoint=endpoint,
                status_code=response.status_code,
                reason_phrase=response.reason_phrase,
                base_url=base_url,
                model=model,
                response_preview=_truncate_text(response.text, 1200),
            )
            raise RuntimeError(error_text)

        try:
            data = response.json()
        except Exception as exc:
            raise RuntimeError(
                f"AI API returned non-JSON response: {_truncate_text(response.text, 1000)}"
            ) from exc

    content = data["choices"][0]["message"]["content"]
    if isinstance(content, list):
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))

    parsed = _extract_json_object(str(content))
    heal_message = _truncate_text(parsed.get("HealMessage", ""), 500) or "AI 無法提供錯誤分析。"
    technical_detail = _truncate_text(parsed.get("TechnicalDetail", ""), 800)
    need_human = _normalize_bool(parsed.get("NeedHuman"))

    _append_bridge_log(
        "ai_response_parsed",
        heal_status=_normalize_status(parsed.get("HealStatus")),
        need_human=need_human,
        action_plan_count=len(parsed.get("ActionPlan", [])) if isinstance(parsed.get("ActionPlan", []), list) else 0,
        heal_message=_truncate_text(heal_message, 300),
        technical_detail=_truncate_text(technical_detail, 800),
        new_selector=_truncate_text(parsed.get("NewSelector", ""), 300),
        recovered_step=_truncate_text(parsed.get("RecoveredStep", ""), 120),
    )

    if _looks_like_manual_verification(heal_message) or _looks_like_manual_verification(technical_detail):
        need_human = True
        if _normalize_status(parsed.get("HealStatus")) == "SUCCESS":
            parsed["HealStatus"] = "FAILED"
        if not technical_detail:
            technical_detail = "Manual intervention required."

    return {
        "HealStatus": _normalize_status(parsed.get("HealStatus")),
        "HealMessage": heal_message,
        "TechnicalDetail": technical_detail,
        "NewSelector": _truncate_text(parsed.get("NewSelector", ""), 300),
        "RecoveredStep": _truncate_text(parsed.get("RecoveredStep", ""), 120),
        "NeedHuman": need_human,
        "ActionPlan": _coerce_action_plan(parsed.get("ActionPlan", [])),
        "Verification": parsed.get("Verification", {}) if isinstance(parsed.get("Verification", {}), dict) else {},
    }


# 根據目前失敗情境挑選最合適的瀏覽器頁面。
# 先偏好 URL / path 相符的頁面，找不到時可接管空白頁再導向目標頁。
async def _find_target_page(
    browser: Browser,
    current_url: str,
    target_url_hint: str = "",
    target_title_hint: str = "",
) -> Optional[Page]:
    pages: List[Page] = []
    for ctx in browser.contexts:
        pages.extend(ctx.pages)

    if not pages:
        return None

    if _is_placeholder_current_url(current_url):
        best_page: Optional[Page] = None
        best_score: Optional[int] = None
        for index, page in enumerate(pages):
            page_url = page.url or ""
            page_url_lc = page_url.strip().lower()
            score = 0

            if page_url_lc in {"about:blank", "chrome://newtab/", "chrome://new-tab-page/"}:
                score -= 200
            elif page_url_lc.startswith(("http://", "https://")):
                score += 200
            elif page_url_lc:
                score += 100

            if target_url_hint.strip():
                score += _text_match_score(target_url_hint, page_url)
            if target_title_hint.strip():
                page_title = await _safe_page_title(page)
                score += _text_match_score(target_title_hint, page_title)

            # 在沒有 CurrentURL 時，讓頁面順序只影響平手情況。
            score += max(0, len(pages) - index)

            if best_score is None or score > best_score:
                best_page = page
                best_score = score

        return best_page

    best_page: Optional[Page] = None
    best_score = 0

    for page in pages:
        page_url = page.url or ""
        score = _page_match_score(current_url, page_url)
        if target_url_hint.strip():
            score += _text_match_score(target_url_hint, page_url)
        if target_title_hint.strip():
            page_title = await _safe_page_title(page)
            score += _text_match_score(target_title_hint, page_title)
        if score > best_score:
            best_page = page
            best_score = score

    if best_score >= 100:
        return best_page

    return None


def _resolve_effective_current_url(context: Dict[str, Any], browser_note: Dict[str, Any]) -> str:
    raw_current_url = str(context.get("CurrentURL", "") or "").strip()
    if not _is_placeholder_current_url(raw_current_url):
        return raw_current_url

    matched_page_url = str(browser_note.get("matched_page_url", "") or "").strip()
    if not _is_placeholder_current_url(matched_page_url):
        return matched_page_url

    pages = browser_note.get("pages", [])
    if isinstance(pages, list):
        for item in pages:
            if isinstance(item, dict):
                candidate_url = str(item.get("url", "") or "").strip()
                if not _is_placeholder_current_url(candidate_url):
                    return candidate_url

    return raw_current_url


async def _find_blank_page(browser: Browser) -> Optional[Page]:
    for ctx in browser.contexts:
        for page in ctx.pages:
            if (page.url or "").strip().lower() in {"about:blank", "chrome://newtab/", "chrome://new-tab-page/"}:
                return page
    return None


# 多看一層頁面狀態，避免只靠截圖外觀就把「右側查詢頁尚未出現」當成真正失敗。
async def _probe_page_state(page: Optional[Page], correlation_id: str, failed_action_name: str = "") -> Dict[str, Any]:
    if not page:
        return {}

    state: Dict[str, Any] = {}

    async def _is_text_visible(candidate: str) -> bool:
        candidate = str(candidate or "").strip()
        if not candidate:
            return False

        try:
            locator = page.get_by_text(candidate, exact=False).first
            if await locator.is_visible():
                return True
        except Exception:
            pass

        for frame in list(getattr(page, "frames", []))[:PAGE_STATE_FRAME_LIMIT]:
            try:
                locator = frame.get_by_text(candidate, exact=False).first
                if await locator.is_visible():
                    return True
            except Exception:
                continue

        return False

    try:
        state["url"] = _truncate_text(page.url, 200)
    except Exception:
        state["url"] = ""

    try:
        state["title"] = _truncate_text(await _safe_page_title(page), 120)
    except Exception:
        state["title"] = ""

    try:
        state["ready_state"] = await page.evaluate("document.readyState")
    except Exception:
        state["ready_state"] = ""

    state["page_settled"] = state.get("ready_state") == "complete"
    state["frame_count"] = len(getattr(page, "frames", []))

    target_candidates = _derive_target_text_candidates_v2(failed_action_name, correlation_id, page.url if page else "")
    state["target_candidates"] = target_candidates[:PAGE_STATE_TARGET_LIMIT]
    state["visible_candidates"] = []
    state["visible_candidate_count"] = 0
    state["failed_action_visible"] = None
    state["target_visible"] = None
    state["target_visible_text"] = ""

    for candidate in target_candidates[:PAGE_STATE_TARGET_LIMIT]:
        if await _is_text_visible(candidate):
            state["visible_candidates"].append(candidate)
            state["target_visible"] = True
            state["failed_action_visible"] = True
            state["target_visible_text"] = candidate
            break

    state["visible_candidate_count"] = len(state["visible_candidates"])
    state["query_form_visible"] = None
    state["overlay_present"] = False
    state["overlay_clues"] = []
    state["dismiss_candidates"] = []

    async def _is_text_visible_in_scope(scope: Any, candidate: str) -> bool:
        candidate = str(candidate or "").strip()
        if not candidate:
            return False

        try:
            locator = scope.get_by_text(candidate, exact=False).first
            return await locator.is_visible()
        except Exception:
            return False

    async def _probe_overlay_in_scope(scope: Any, scope_label: str) -> None:
        try:
            overlay_info = await scope.evaluate(
                """() => {
                    const isVisible = (el) => {
                        if (!el) return false;
                        const style = window.getComputedStyle(el);
                        if (!style) return false;
                        const rect = el.getBoundingClientRect();
                        return style.display !== 'none' && style.visibility !== 'hidden' &&
                               style.opacity !== '0' && rect.width > 0 && rect.height > 0;
                    };
                    const selectors = [
                        '[aria-modal="true"]',
                        '[role="dialog"]',
                        '[role="alertdialog"]',
                        'dialog',
                        '[class*="modal" i]',
                        '[class*="dialog" i]',
                        '[class*="overlay" i]',
                        '[class*="backdrop" i]',
                        '[class*="popup" i]',
                        '[class*="mask" i]',
                        '[class*="prompt" i]'
                    ];
                    const hitSelectors = [];
                    for (const selector of selectors) {
                        for (const el of document.querySelectorAll(selector)) {
                            if (isVisible(el)) {
                                hitSelectors.push(selector);
                                break;
                            }
                        }
                    }
                    const bodyOverflow = document.body ? window.getComputedStyle(document.body).overflow : '';
                    const htmlOverflow = document.documentElement ? window.getComputedStyle(document.documentElement).overflow : '';
                    return { hitSelectors, bodyOverflow, htmlOverflow };
                }"""
            )
        except Exception:
            overlay_info = {}

        hit_selectors = overlay_info.get("hitSelectors", []) if isinstance(overlay_info, dict) else []
        if hit_selectors:
            state["overlay_present"] = True
            for selector in hit_selectors:
                clue = f"{scope_label}:{selector}"
                if clue not in state["overlay_clues"]:
                    state["overlay_clues"].append(clue)

        for candidate in ("關閉", "關閉視窗", "取消", "確定", "我知道了", "知道了", "Close", "close", "OK", "Ok", "×", "✕", "X", "略過", "跳過"):
            if await _is_text_visible_in_scope(scope, candidate):
                state["overlay_present"] = True
                if candidate not in state["dismiss_candidates"]:
                    state["dismiss_candidates"].append(candidate)

    await _probe_overlay_in_scope(page, "page")
    for frame_index, frame in enumerate(list(getattr(page, "frames", []))[:OVERLAY_FRAME_LIMIT]):
        try:
            await _probe_overlay_in_scope(frame, f"frame{frame_index}")
        except Exception:
            continue

    if state["dismiss_candidates"]:
        state["overlay_present"] = True

    return state


def _read_socket_exact(sock: socket.socket, size: int) -> bytes:
    chunks: List[bytes] = []
    remaining = size
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            raise ConnectionError("CDP WebSocket closed before the frame was complete.")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _encode_websocket_text_frame(text: str) -> bytes:
    payload = text.encode("utf-8")
    mask = os.urandom(4)
    masked_payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    length = len(payload)
    if length < 126:
        header = bytes([0x81, 0x80 | length])
    elif length < 65536:
        header = bytes([0x81, 0x80 | 126]) + struct.pack("!H", length)
    else:
        header = bytes([0x81, 0x80 | 127]) + struct.pack("!Q", length)
    return header + mask + masked_payload


def _send_cdp_websocket_command(
    websocket_url: str,
    method: str,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    parsed = urlparse(websocket_url)
    if parsed.scheme != "ws" or not parsed.hostname:
        raise RuntimeError(f"Unsupported CDP WebSocket URL: {websocket_url}")

    port = parsed.port or 80
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"

    request = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {parsed.hostname}:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {base64.b64encode(os.urandom(16)).decode('ascii')}\r\n"
        "Sec-WebSocket-Version: 13\r\n\r\n"
    ).encode("ascii")

    command = json.dumps(
        {"id": 1, "method": method, "params": params or {}},
        ensure_ascii=False,
        separators=(",", ":"),
    )

    with socket.create_connection((parsed.hostname, port), timeout=3.0) as sock:
        sock.settimeout(3.0)
        sock.sendall(request)
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("CDP WebSocket closed during handshake.")
            response += chunk
            if len(response) > 32768:
                raise ConnectionError("CDP WebSocket handshake response is too large.")

        if not response.startswith(b"HTTP/1.1 101"):
            status_line = response.split(b"\r\n", 1)[0].decode("latin-1", "replace")
            raise ConnectionError(f"CDP WebSocket handshake rejected: {status_line}")

        sock.sendall(_encode_websocket_text_frame(command))
        while True:
            first_byte, second_byte = _read_socket_exact(sock, 2)
            fin = bool(first_byte & 0x80)
            opcode = first_byte & 0x0F
            masked = bool(second_byte & 0x80)
            length = second_byte & 0x7F
            if length == 126:
                length = struct.unpack("!H", _read_socket_exact(sock, 2))[0]
            elif length == 127:
                length = struct.unpack("!Q", _read_socket_exact(sock, 8))[0]

            mask = _read_socket_exact(sock, 4) if masked else b""
            payload = bytearray(_read_socket_exact(sock, length))
            if masked:
                for index in range(len(payload)):
                    payload[index] ^= mask[index % 4]

            if opcode == 0x9:
                pong = bytes([0x8A, len(payload)]) + bytes(payload)
                sock.sendall(pong)
                continue
            if opcode == 0x8:
                raise ConnectionError("CDP WebSocket closed before returning a response.")
            if opcode != 0x1 or not fin:
                continue

            decoded = json.loads(bytes(payload).decode("utf-8"))
            if decoded.get("id") == 1:
                return decoded


async def _list_cdp_page_targets() -> List[Dict[str, Any]]:
    port = _resolve_cdp_port()
    async with httpx.AsyncClient(timeout=httpx.Timeout(3.0), trust_env=False) as client:
        response = await client.get(f"http://127.0.0.1:{port}/json/list")
        response.raise_for_status()
        payload = response.json()
    return [item for item in payload if isinstance(item, dict) and item.get("type") == "page"]


def _dismiss_native_dialogs_with_windows_ui_automation(port: int) -> int:
    """Accept Chrome's visible native JavaScript dialog before Playwright attach.

    ``Page.handleJavaScriptDialog`` uses the page CDP target, but an already
    visible Chrome modal can make that target unresponsive.  On Windows use
    UI Automation against only the process that owns the configured CDP port.
    This keeps the action scoped to the managed Chrome session and avoids
    sending a blind keystroke to the user's active window.
    """
    if os.name != "nt":
        return 0

    powershell_script = rf'''
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes

$owners = @()
try {{
    $owners = @(Get-NetTCPConnection -LocalPort {int(port)} -State Listen -ErrorAction Stop |
        Select-Object -ExpandProperty OwningProcess -Unique)
}} catch {{
    $owners = @()
}}

$root = [System.Windows.Automation.AutomationElement]::RootElement
$dismissed = 0
foreach ($owner in $owners) {{
    try {{
        $pidCondition = [System.Windows.Automation.PropertyCondition]::new(
            [System.Windows.Automation.AutomationElement]::ProcessIdProperty,
            [int]$owner)
        $buttonCondition = [System.Windows.Automation.PropertyCondition]::new(
            [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
            [System.Windows.Automation.ControlType]::Button)
        $condition = [System.Windows.Automation.AndCondition]::new($pidCondition, $buttonCondition)
        $buttons = $root.FindAll([System.Windows.Automation.TreeScope]::Descendants, $condition)
        for ($index = 0; $index -lt $buttons.Count; $index++) {{
            $button = $buttons.Item($index)
            $name = [string]$button.Current.Name
            $className = [string]$button.Current.ClassName
            $isAcceptButton = $name -in @("確定", "OK", "Ok", "ok")
            $isChromeDialogButton = $className -eq "MdTextButton" -or
                $className -eq "Chrome_WidgetWin_1"
            if ($isAcceptButton -and $isChromeDialogButton) {{
                try {{
                    $pattern = $button.GetCurrentPattern(
                        [System.Windows.Automation.InvokePattern]::Pattern)
                    $pattern.Invoke()
                    $dismissed++
                    break
                }} catch {{}}
            }}
        }}
    }} catch {{}}
}}
Write-Output ("uia_dismissed=" + $dismissed)
'''
    encoded_script = base64.b64encode(powershell_script.encode("utf-16le")).decode("ascii")
    try:
        completed = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-EncodedCommand",
                encoded_script,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
    except Exception as exc:
        _append_bridge_log(
            "native_dialog_uia_failed",
            error=f"{type(exc).__name__}: {exc}",
        )
        return 0

    stdout = (completed.stdout or "").strip()
    stderr = (completed.stderr or "").strip()
    if completed.returncode != 0:
        _append_bridge_log(
            "native_dialog_uia_failed",
            return_code=completed.returncode,
            stdout=_truncate_text(stdout, 500),
            stderr=_truncate_text(stderr, 500),
        )
        return 0

    match = re.search(r"uia_dismissed=(\d+)", stdout)
    dismissed = int(match.group(1)) if match else 0
    _append_bridge_log(
        "native_dialog_uia_check",
        cdp_port=port,
        dismissed_count=dismissed,
        stdout=_truncate_text(stdout, 500),
    )
    return dismissed


async def _dismiss_existing_native_dialogs() -> int:
    """Dismiss already-open JavaScript dialogs before Playwright connects."""
    dismissed = 0
    try:
        targets = await _list_cdp_page_targets()
    except Exception as exc:
        _append_bridge_log("native_dialog_probe_failed", error=f"{type(exc).__name__}: {exc}")
        return 0

    for target in targets:
        websocket_url = str(target.get("webSocketDebuggerUrl", "") or "").strip()
        if not websocket_url:
            continue
        try:
            # Chrome can expose an already-open alert/confirm/prompt while the
            # page target is still reachable.  Enable the Page domain first;
            # otherwise Page.handleJavaScriptDialog may report "No dialog is
            # showing" even though the visible browser UI is modal, and
            # Playwright can remain stuck during connect_over_cdp attach.
            enable_response = await asyncio.to_thread(
                _send_cdp_websocket_command,
                websocket_url,
                "Page.enable",
                {},
            )
            enable_error = enable_response.get("error") if isinstance(enable_response, dict) else None
            if enable_error:
                _append_bridge_log(
                    "native_dialog_page_enable_failed",
                    page_url=str(target.get("url", "")),
                    error=enable_error,
                )
            response = await asyncio.to_thread(
                _send_cdp_websocket_command,
                websocket_url,
                "Page.handleJavaScriptDialog",
                {"accept": True},
            )
            error = response.get("error") if isinstance(response, dict) else None
            if error:
                _append_bridge_log(
                    "native_dialog_not_present",
                    page_url=str(target.get("url", "")),
                    error=error,
                )
            else:
                dismissed += 1
                _append_bridge_log(
                    "native_dialog_dismissed_before_cdp",
                    page_url=str(target.get("url", "")),
                )
        except Exception as exc:
            _append_bridge_log(
                "native_dialog_dismiss_failed",
                page_url=str(target.get("url", "")),
                error=f"{type(exc).__name__}: {exc}",
            )
    return dismissed


async def _resolve_page_websocket_url(page: Page) -> Optional[str]:
    try:
        targets = await _list_cdp_page_targets()
    except Exception:
        return None

    page_url = str(getattr(page, "url", "") or "")
    for target in targets:
        if str(target.get("url", "") or "") == page_url:
            websocket_url = str(target.get("webSocketDebuggerUrl", "") or "").strip()
            if websocket_url:
                return websocket_url
    return None


async def _accept_native_dialog(page: Page) -> bool:
    websocket_url = await _resolve_page_websocket_url(page)
    if not websocket_url:
        return False
    response = await asyncio.to_thread(
        _send_cdp_websocket_command,
        websocket_url,
        "Page.handleJavaScriptDialog",
        {"accept": True},
    )
    return not bool(response.get("error"))


async def _accept_playwright_dialog(dialog: Any) -> None:
    try:
        _append_bridge_log(
            "native_dialog_detected",
            dialog_type=str(getattr(dialog, "type", "")),
            message=_truncate_text(str(getattr(dialog, "message", "")), 300),
        )
        await dialog.accept()
    except Exception as exc:
        _append_bridge_log("native_dialog_accept_failed", error=f"{type(exc).__name__}: {exc}")


def _install_dialog_handlers(browser: Browser) -> None:
    for context in browser.contexts:
        for page in context.pages:
            page.on("dialog", _accept_playwright_dialog)


def _resolve_cdp_port() -> int:
    raw_port = str(os.getenv("AI_CDP_PORT", "") or "").strip()
    if not raw_port:
        return 9222
    try:
        return int(raw_port)
    except Exception:
        return 9222


def _resolve_cdp_connect_timeout_ms() -> int:
    raw = str(os.getenv("AI_CDP_CONNECT_TIMEOUT_MS", "") or "").strip()
    try:
        value = int(raw) if raw else CDP_CONNECT_TIMEOUT_MS
    except Exception:
        value = CDP_CONNECT_TIMEOUT_MS
    return max(5000, min(value, 60000))


async def _resolve_cdp_websocket_url() -> Optional[str]:
    port = _resolve_cdp_port()
    candidates = (
        f"http://127.0.0.1:{port}/json/version",
        f"http://localhost:{port}/json/version",
    )

    async with httpx.AsyncClient(timeout=httpx.Timeout(2.0), trust_env=False) as client:
        for attempt in range(1, 6):
            for endpoint in candidates:
                try:
                    response = await client.get(endpoint)
                    response.raise_for_status()
                    payload = response.json()
                    websocket_url = str(payload.get("webSocketDebuggerUrl", "")).strip()
                    if websocket_url:
                        _append_bridge_log(
                            "cdp_websocket_resolved",
                            attempt=attempt,
                            endpoint=endpoint,
                            websocket_url=websocket_url,
                        )
                        return websocket_url
                except Exception as exc:
                    _append_bridge_log(
                        "cdp_websocket_probe_failed",
                        attempt=attempt,
                        endpoint=endpoint,
                        error=f"{type(exc).__name__}: {exc}",
                    )

            if attempt < 5:
                await asyncio.sleep(1)

    return None


async def _connect_browser_over_cdp(playwright: Any) -> Browser:
    """Connect to CDP only after the browser WebSocket passes a real probe."""
    port = _resolve_cdp_port()
    timeout_ms = _resolve_cdp_connect_timeout_ms()
    last_error: Optional[BaseException] = None

    dialog_recovery_attempted = False

    for attempt in range(1, CDP_CONNECT_ATTEMPTS + 1):
        websocket_url = await _resolve_cdp_websocket_url()
        if not websocket_url:
            last_error = RuntimeError(f"CDP websocket URL was not available on port {port}.")
            _append_bridge_log(
                "cdp_preflight_failed",
                attempt=attempt,
                error=str(last_error),
            )
            if attempt < CDP_CONNECT_ATTEMPTS:
                await asyncio.sleep(
                    CDP_RETRY_DELAYS_SECONDS[
                        min(attempt - 1, len(CDP_RETRY_DELAYS_SECONDS) - 1)
                    ]
                )
            continue

        # /json/version 只提供端點資訊；這個 command 會實際驗證瀏覽器層
        # WebSocket 能否收發 CDP 訊息，避免 port 開著卻讓 Playwright 卡住。
        _append_bridge_log(
            "cdp_preflight_start",
            attempt=attempt,
            timeout_seconds=CDP_PREFLIGHT_TIMEOUT_SECONDS,
            websocket_url=websocket_url,
        )
        try:
            preflight_response = await asyncio.wait_for(
                asyncio.to_thread(
                    _send_cdp_websocket_command,
                    websocket_url,
                    "Browser.getVersion",
                    {},
                ),
                timeout=CDP_PREFLIGHT_TIMEOUT_SECONDS,
            )
            preflight_error = (
                preflight_response.get("error")
                if isinstance(preflight_response, dict)
                else None
            )
            if preflight_error:
                raise RuntimeError(
                    f"Browser.getVersion returned CDP error: {preflight_error}"
                )

            product = ""
            if isinstance(preflight_response, dict):
                result = preflight_response.get("result")
                if isinstance(result, dict):
                    product = str(result.get("product", "") or "")
            _append_bridge_log(
                "cdp_preflight_success",
                attempt=attempt,
                product=product,
            )
        except Exception as exc:
            last_error = exc
            _append_bridge_log(
                "cdp_preflight_failed",
                attempt=attempt,
                timeout_seconds=CDP_PREFLIGHT_TIMEOUT_SECONDS,
                error=f"{type(exc).__name__}: {exc}",
            )
            if attempt < CDP_CONNECT_ATTEMPTS:
                await asyncio.sleep(
                    CDP_RETRY_DELAYS_SECONDS[
                        min(attempt - 1, len(CDP_RETRY_DELAYS_SECONDS) - 1)
                    ]
                )
            continue

        try:
            _append_bridge_log(
                "cdp_connect_start",
                attempt=attempt,
                timeout_ms=timeout_ms,
                websocket_url=websocket_url,
            )
            browser = await playwright.chromium.connect_over_cdp(
                websocket_url,
                timeout=timeout_ms,
            )
            _install_dialog_handlers(browser)
            _append_bridge_log(
                "cdp_connect_success",
                attempt=attempt,
                page_count=sum(len(context.pages) for context in browser.contexts),
            )
            return browser
        except Exception as exc:
            last_error = exc
            _append_bridge_log(
                "cdp_connect_failed",
                attempt=attempt,
                timeout_ms=timeout_ms,
                error=f"{type(exc).__name__}: {exc}",
            )
            if not dialog_recovery_attempted:
                dismissed = await _dismiss_existing_native_dialogs()
                dialog_recovery_attempted = True
                _append_bridge_log(
                    "native_dialog_connect_fallback",
                    dismissed_count=dismissed,
                )
            if attempt < CDP_CONNECT_ATTEMPTS:
                await asyncio.sleep(
                    CDP_RETRY_DELAYS_SECONDS[
                        min(attempt - 1, len(CDP_RETRY_DELAYS_SECONDS) - 1)
                    ]
                )

    detail = f"{type(last_error).__name__}: {last_error}" if last_error else "unknown CDP error"
    raise RuntimeError(
        f"CDP browser handshake failed on port {port} after {CDP_CONNECT_ATTEMPTS} attempts. "
        f"The port may be listening while the browser WebSocket is unresponsive. {detail}"
    ) from last_error


# ActionPlan 執行時使用的切換分頁工具。
async def _resolve_switch_page(page: Page, target: str) -> Optional[Page]:
    pages = page.context.pages
    if not pages:
        return None

    target = target.strip()
    if target.isdigit():
        index = max(0, min(int(target), len(pages) - 1))
        return pages[index]

    for candidate in pages:
        try:
            title = await candidate.title()
        except Exception:
            title = ""
        if target in candidate.url or target in title:
            return candidate
    return None


async def _wait_for_new_top_level_page(
    page: Page,
    pages_before: List[Page],
    timeout_ms: int = 1500,
) -> Optional[Page]:
    """Return a newly opened browser tab, never an iframe/frame."""
    known_page_ids = {id(candidate) for candidate in pages_before}
    deadline = time.monotonic() + max(0, timeout_ms) / 1000

    while True:
        current_pages = list(page.context.pages)
        new_pages = [candidate for candidate in current_pages if id(candidate) not in known_page_ids]
        if new_pages:
            # Playwright preserves tab creation order in context.pages. Prefer
            # the newest non-blank top-level tab, then fall back to the newest.
            non_blank = [
                candidate
                for candidate in new_pages
                if (candidate.url or "").strip().lower()
                not in {"", "about:blank", "chrome://newtab/", "chrome://new-tab-page/"}
            ]
            return (non_blank or new_pages)[-1]

        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(0.05)


async def _run_action_and_capture_new_page(page: Page, action_callback: Any) -> Optional[Page]:
    """Run a click/press and capture a newly-created top-level tab if one appears."""
    action_completed = False
    try:
        async with page.context.expect_page(timeout=ACTION_NEW_PAGE_TIMEOUT_MS) as page_info:
            await action_callback()
            action_completed = True
    except PlaywrightTimeoutError:
        # The action itself may have timed out. Only treat the timeout as
        # "no popup" after the action callback completed successfully.
        if not action_completed:
            raise
        return None

    return await page_info.value


async def _activate_top_level_page(page: Page, candidate: Optional[Page]) -> Page:
    """Bring a top-level tab forward and wait briefly for its document."""
    if candidate is None or candidate is page:
        return page

    try:
        await candidate.bring_to_front()
    except Exception:
        pass

    try:
        await candidate.wait_for_load_state("domcontentloaded", timeout=ACTION_NAVIGATION_TIMEOUT_MS)
    except Exception:
        pass

    try:
        await candidate.wait_for_load_state("load", timeout=ACTION_NAVIGATION_TIMEOUT_MS)
    except Exception:
        pass

    return candidate


def _split_frame_locator_target(target: str) -> Tuple[str, str]:
    text = str(target or "").strip()
    if not text:
        return "", ""

    match = re.match(r"^(?P<frame>(?:frame|iframe)\[[^\]]+\])\s*(?P<inner>.+)$", text, flags=re.IGNORECASE)
    if match:
        return match.group("frame").strip(), match.group("inner").strip()

    match = re.match(r"^(?P<frame>(?:frame|iframe)\[[^\]]+\])\s*>>\s*(?P<inner>.+)$", text, flags=re.IGNORECASE)
    if match:
        return match.group("frame").strip(), match.group("inner").strip()

    return "", text


def _expand_frame_selector_candidates(frame_selector: str) -> List[str]:
    selector = str(frame_selector or "").strip()
    if not selector:
        return []

    candidates = [selector]
    match = re.match(
        r"^(?P<tag>frame|iframe)\[\s*name\s*=\s*['\"](?P<value>[^'\"]+)['\"]\s*\]$",
        selector,
        flags=re.IGNORECASE,
    )
    if match:
        tag = match.group("tag")
        value = match.group("value").strip()
        for extra in (
            f"{tag}[id='{value}']",
            f"{tag}#{value}",
            f"{tag}[name='{value}']",
        ):
            if extra not in candidates:
                candidates.append(extra)
    return candidates


def _looks_like_raw_css_selector(value: Any) -> bool:
    """判斷未加 css= 前綴的目標是否看起來像 CSS selector。

    AI 常會回傳 `a[href*='...']`、`.class` 或 `[aria-label='...']`，
    這些都是 Playwright 可直接接受的 CSS，但不能當成頁面文字搜尋。
    只辨識具有 CSS 語法特徵的值，避免把一般英文按鈕文字誤判成 selector。
    """
    value = str(value or "").strip()
    if not value or value.lower().startswith(("css=", "xpath=", "text=")):
        return False

    return bool(
        re.match(r"^(?:[#.][\w-]+|\[[^\]]+\]|\*|[A-Za-z][\w:-]*\s*[.#:\[>+~])", value)
        or re.match(r"^[A-Za-z][\w:-]*\s+[A-Za-z][\w:-]*(?:[.#:\[>+~]|$)", value)
    )


# 通用 locator 解決器：先查目前頁面，再查所有 frames。
async def _resolve_locator_in_page_or_frames(page: Page, target: str):
    target = str(target or "").strip()
    if not target:
        return None

    # 允許用 `||` 串接多個候選 selector，先試最精準的，再退回文字比對。
    search_targets: List[str] = []
    for chunk in re.split(r"\s*\|\|\s*", target):
        chunk = str(chunk or "").strip()
        if chunk and chunk not in search_targets:
            search_targets.append(chunk)

    if not search_targets:
        search_targets = [target]

    expanded_targets: List[str] = []
    for candidate in search_targets:
        if candidate not in expanded_targets:
            expanded_targets.append(candidate)
        lowered = candidate.lower()
        if lowered.startswith("text="):
            raw_text = candidate[5:].strip()
            if raw_text and raw_text not in expanded_targets:
                expanded_targets.append(raw_text)
        elif lowered.startswith("css="):
            raw_css = candidate[4:].strip()
            if raw_css and raw_css not in expanded_targets:
                expanded_targets.append(raw_css)
        elif lowered.startswith("xpath="):
            raw_xpath = candidate[6:].strip()
            if raw_xpath and raw_xpath not in expanded_targets:
                expanded_targets.append(raw_xpath)

    search_targets = expanded_targets

    async def try_scope(scope: Any, candidate_target: str, timeout_ms: int = ACTION_VISIBLE_TIMEOUT_MS):
        candidate_target = str(candidate_target or "").strip()
        if not candidate_target:
            return None

        lowered = candidate_target.lower()
        strategies: List[Tuple[str, Any]] = []

        if lowered.startswith(("css=", "xpath=", "text=")):
            strategies.append(("locator", candidate_target))
            if lowered.startswith("text="):
                raw_text = candidate_target[5:].strip()
                if raw_text:
                    strategies.append(("text_exact", raw_text))
                    strategies.append(("text", raw_text))
        elif _looks_like_raw_css_selector(candidate_target):
            # AI 回傳的 raw CSS（例如 a[href*='...']）不一定帶 css=。
            # 先用 CSS 定位，失敗後仍保留文字/ARIA 語意後備，維持跨流程相容性。
            strategies.extend(
                [
                    ("locator", f"css={candidate_target}"),
                    ("locator", candidate_target),
                    ("text_exact", candidate_target),
                    ("text", candidate_target),
                ]
            )
        else:
            strategies.extend(
                [
                    ("text_exact", candidate_target),
                    ("text", candidate_target),
                    ("role", ("link", candidate_target)),
                    ("role", ("button", candidate_target)),
                    ("role", ("menuitem", candidate_target)),
                    ("role", ("treeitem", candidate_target)),
                    ("role", ("tab", candidate_target)),
                    ("locator", f"text={candidate_target}"),
                ]
            )

        for kind, payload in strategies:
            try:
                if kind == "locator":
                    locator = scope.locator(str(payload)).first
                elif kind == "text_exact":
                    locator = scope.get_by_text(str(payload), exact=True).first
                elif kind == "text":
                    locator = scope.get_by_text(str(payload), exact=False).first
                elif kind == "role":
                    role_name, role_target = payload
                    locator = scope.get_by_role(str(role_name), name=str(role_target), exact=False).first
                else:
                    continue

                if await locator.is_visible():
                    return locator
            except Exception:
                continue

        return None

    frame_selector, inner_target = _split_frame_locator_target(target)
    if frame_selector:
        for candidate_frame_selector in _expand_frame_selector_candidates(frame_selector):
            try:
                frame_scope = page.frame_locator(candidate_frame_selector)
                locator = await try_scope(frame_scope, inner_target)
                if locator:
                    return locator
            except Exception:
                continue

    for candidate_target in search_targets:
        try:
            locator = await try_scope(page, candidate_target)
            if locator:
                return locator
        except Exception:
            pass

        for frame in getattr(page, "frames", []):
            try:
                locator = await try_scope(frame, candidate_target)
                if locator:
                    return locator
            except Exception:
                continue

    return None


def _pick_primary_selector_candidate(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""

    for chunk in re.split(r"\s*\|\|\s*", text):
        chunk = str(chunk or "").strip()
        if chunk:
            return chunk

    return text


# 依照 AI 產生的動作計畫逐步執行 UI 操作。
# 回傳執行紀錄與最後作用中的頂層分頁，避免點擊開新分頁後仍操作舊頁面。
async def _execute_action_plan(
    page: Page,
    action_plan: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], Page]:
    execution_log: List[Dict[str, Any]] = []

    for index, step in enumerate(action_plan, start=1):
        action = step["action"]
        target = step.get("target", "")
        value = step.get("value", "")
        ms = int(step.get("ms") or 0)
        entry = {"step": index, "action": action, "target": target, "ok": False, "detail": ""}

        try:
            pages_before = list(page.context.pages)
            new_page: Optional[Page] = None

            if action == "wait":
                wait_ms = max(
                    ACTION_WAIT_MIN_MS,
                    min(ms or ACTION_WAIT_MIN_MS, ACTION_WAIT_MAX_MS),
                )
                await page.wait_for_timeout(wait_ms)
            elif action == "refresh":
                await page.reload(wait_until="domcontentloaded", timeout=ACTION_NAVIGATION_TIMEOUT_MS)
            elif action == "goback":
                await page.go_back(wait_until="domcontentloaded", timeout=ACTION_NAVIGATION_TIMEOUT_MS)
            elif action == "openurl":
                await page.goto(target, wait_until="domcontentloaded", timeout=ACTION_NAVIGATION_TIMEOUT_MS)
            elif action == "switchtab":
                target_page = await _resolve_switch_page(page, target)
                if not target_page:
                    raise RuntimeError(f"Target tab not found: {target}")
                await target_page.bring_to_front()
                page = target_page
            elif action == "closetab":
                await page.close()
            elif action == "scroll":
                direction = str(value or target or "down").strip().lower()
                delta = -600 if direction in {"up", "-1", "-600"} else 600
                if str(value).strip().lstrip("-").isdigit():
                    delta = int(value)
                await page.mouse.wheel(0, delta)
            elif action == "hover":
                locator = await _resolve_locator_in_page_or_frames(page, target)
                if not locator:
                    raise RuntimeError(f"Target not found for hover: {target}")
                await locator.hover(timeout=ACTION_LOCATOR_TIMEOUT_MS)
            elif action == "acceptdialog":
                if await _accept_native_dialog(page):
                    entry["detail"] = "Accepted native JavaScript dialog through CDP."
                else:
                    # AI may include a dialog dismissal as a safe fallback even
                    # when the dialog has already disappeared or was never open.
                    # Treat that case as a no-op and continue with the remaining
                    # recovery actions; verification remains the final authority.
                    entry["skipped"] = True
                    entry["detail"] = "No active native JavaScript dialog; skipped."
            elif action == "click":
                async def click_current_page() -> None:
                    try:
                        locator = await _resolve_locator_in_page_or_frames(page, target)
                        if not locator:
                            raise RuntimeError(f"Target not found for click: {target}")
                        await locator.click(timeout=ACTION_LOCATOR_TIMEOUT_MS)
                    except Exception as click_error:
                        # A browser-native alert or an overlay may hide the DOM target.
                        # Send one Escape as a bounded fallback, then let verification
                        # decide whether the page actually recovered.
                        try:
                            await page.keyboard.press("Escape")
                            entry["fallback"] = "escape"
                            entry["detail"] = (
                                f"Click failed ({click_error}); Escape fallback sent."
                            )
                        except Exception as escape_error:
                            raise click_error from escape_error

                new_page = await _run_action_and_capture_new_page(page, click_current_page)
            elif action == "fill":
                locator = await _resolve_locator_in_page_or_frames(page, target)
                if not locator:
                    raise RuntimeError(f"Target not found for fill: {target}")
                await locator.fill(str(value), timeout=ACTION_LOCATOR_TIMEOUT_MS)
            elif action == "press":
                async def press_current_page() -> None:
                    key = str(value or target or "Enter")
                    if target:
                        locator = await _resolve_locator_in_page_or_frames(page, target)
                        if not locator:
                            raise RuntimeError(f"Target not found for press: {target}")
                        await locator.press(key, timeout=ACTION_LOCATOR_TIMEOUT_MS)
                    else:
                        await page.keyboard.press(key)

                new_page = await _run_action_and_capture_new_page(page, press_current_page)
            elif action == "selectoption":
                locator = await _resolve_locator_in_page_or_frames(page, target)
                if not locator:
                    raise RuntimeError(f"Target not found for selectoption: {target}")
                if isinstance(value, list):
                    await locator.select_option([str(v) for v in value], timeout=ACTION_LOCATOR_TIMEOUT_MS)
                else:
                    await locator.select_option(str(value), timeout=ACTION_LOCATOR_TIMEOUT_MS)
            else:
                raise RuntimeError(f"Unsupported action: {action}")

            if action in {"openurl", "refresh", "goback", "switchtab"}:
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=ACTION_NAVIGATION_TIMEOUT_MS)
                except Exception:
                    pass
            elif action in {"click", "acceptdialog", "fill", "press", "selectoption", "hover"}:
                try:
                    await page.wait_for_timeout(ACTION_POST_CLICK_DELAY_MS)
                except Exception:
                    pass

            if action in {"click", "press"}:
                if new_page is None:
                    # Keep a short polling fallback for browsers that emit the
                    # page event just after expect_page's timeout boundary.
                    new_page = await _wait_for_new_top_level_page(
                        page,
                        pages_before,
                        timeout_ms=ACTION_NEW_PAGE_FALLBACK_TIMEOUT_MS,
                    )
                if new_page is not None:
                    page = await _activate_top_level_page(page, new_page)
                    entry["detail"] = f"Executed successfully; active top-level page: {page.url}"

            entry["ok"] = True
            if not entry["detail"]:
                entry["detail"] = "Executed successfully."
        except Exception as exc:
            entry["detail"] = str(exc)
            execution_log.append(entry)
            raise

        execution_log.append(entry)

    return execution_log, page


async def _verify_recovery(
    page: Page,
    verification: Dict[str, Any],
    execution_log: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[bool, str]:
    if not isinstance(verification, dict) or not verification:
        return False, "Missing verification rule."

    verify_type = str(verification.get("type", "")).strip().lower()
    verify_value = _pick_primary_selector_candidate(verification.get("value", ""))
    verification_timeout = 8000
    if verify_type == "page_ready":
        verification_timeout = 10000
    elif verify_type in {"selector_visible", "text_visible"}:
        verification_timeout = 8000

    try:
        if verify_type == "action_executed":
            return True, "action_executed"
        if verify_type == "dialog_accepted":
            accepted = any(
                isinstance(entry, dict)
                and str(entry.get("action", "")).strip().lower() == "acceptdialog"
                and bool(entry.get("ok"))
                and not bool(entry.get("skipped"))
                for entry in (execution_log or [])
            )
            return accepted, "dialog_accepted" if accepted else "dialog_not_accepted"
        if verify_type == "url_contains":
            return verify_value in page.url, f"page.url={page.url}"
        if verify_type == "url_equals":
            return page.url == verify_value, f"page.url={page.url}"
        if verify_type == "title_contains":
            title = await _safe_page_title(page)
            return verify_value in title, f"title={title}"
        if verify_type == "selector_visible":
            locator = page.locator(verify_value).first
            await locator.wait_for(state="visible", timeout=verification_timeout)
            return True, f"selector={verify_value}"
        if verify_type == "text_visible":
            locator = page.get_by_text(verify_value, exact=False).first
            await locator.wait_for(state="visible", timeout=verification_timeout)
            return True, f"text={verify_value}"
        if verify_type == "page_ready":
            ready_state = await page.evaluate("document.readyState")
            if ready_state == "complete":
                return True, f"readyState={ready_state}"
            await page.wait_for_load_state("domcontentloaded", timeout=verification_timeout)
            ready_state = await page.evaluate("document.readyState")
            return ready_state == "complete", f"readyState={ready_state}"
    except Exception as exc:
        return False, str(exc)

    return False, f"Unsupported verification type: {verify_type}"


def _can_preverify_recovery(verification: Any) -> bool:
    """Return whether a verification rule can be checked without an action."""
    if not isinstance(verification, dict):
        return False

    verify_type = str(verification.get("type", "") or "").strip().lower()
    if verify_type not in {
        "url_contains",
        "url_equals",
        "title_contains",
        "selector_visible",
        "text_visible",
        "page_ready",
    }:
        return False

    if verify_type == "page_ready":
        return True
    return bool(_pick_primary_selector_candidate(verification.get("value", "")))


# 主流程：
# 讀 context、連瀏覽器、選頁或開頁、呼叫 AI、執行動作、驗證結果，最後回傳總結。
async def run_webwright_agent(context_file: str, image_file: str) -> Dict[str, Any]:
    _append_bridge_log("bridge_start", context_file=context_file, image_file=image_file, bridge_log_path=_bridge_log_path())
    api_settings = _resolve_api_settings()
    _append_bridge_log(
        "bridge_env_snapshot",
        api_key_fingerprint=_secret_fingerprint(api_settings.get("api_key", "")),
        base_url=api_settings.get("base_url", ""),
        model=api_settings.get("model", ""),
    )

    try:
        context = _load_context(context_file)
    except FileNotFoundError:
        _append_bridge_log("context_missing", context_file=context_file)
        return _finalize_result({
            "HealStatus": "FAILED",
            "HealMessage": "Context file is missing.",
            "TechnicalDetail": "",
            "NewSelector": "",
            "RecoveredStep": "",
            "NeedHuman": True,
            "ActionPlan": [],
            "Verification": {},
            "VerificationResult": {},
            "ExecutionLog": [],
        }, image_file, include_debug=True)
    except json.JSONDecodeError:
        _append_bridge_log("context_invalid_json", context_file=context_file)
        return _finalize_result({
            "HealStatus": "FAILED",
            "HealMessage": "Context JSON is invalid.",
            "TechnicalDetail": "",
            "NewSelector": "",
            "RecoveredStep": "",
            "NeedHuman": True,
            "ActionPlan": [],
            "Verification": {},
            "VerificationResult": {},
            "ExecutionLog": [],
        }, image_file, include_debug=True)

    current_url = str(context.get("CurrentURL", "about:blank"))
    correlation_id = str(context.get("CorrelationId", "") or "").strip()
    target_url_hint = str(
        context.get("TargetPageURL", "")
        or os.getenv("AI_TARGET_PAGE_URL", "").strip()
        or ""
    )
    target_title_hint = str(
        context.get("TargetPageTitle", "")
        or os.getenv("AI_TARGET_PAGE_TITLE", "").strip()
        or ""
    )
    artifact_dir = os.getenv("AI_ARTIFACT_DIR", "").strip() or os.path.dirname(context_file) or os.getcwd()
    _cleanup_html_artifacts(artifact_dir)
    page_snapshot: Dict[str, Any] = {}
    browser_note: Dict[str, Any] = {}
    probe_workflow_context = _build_workflow_context(context, context_file=context_file)
    probe_failed_action_name = str(probe_workflow_context.get("failed_action_name", "") or "")

    async with async_playwright() as p:
        browser: Optional[Browser] = None
        try:
            # A native JavaScript dialog can block Playwright's browser-level
            # attach even though the CDP port and Browser.getVersion probe
            # are healthy.  Clear it before the first connect attempt, not
            # only after a timeout.
            cdp_port = _resolve_cdp_port()
            uia_dismissed = await asyncio.to_thread(
                _dismiss_native_dialogs_with_windows_ui_automation,
                cdp_port,
            )
            cdp_dismissed = await _dismiss_existing_native_dialogs()
            preconnect_dismissed = uia_dismissed + cdp_dismissed
            _append_bridge_log(
                "native_dialog_preconnect_check",
                uia_dismissed_count=uia_dismissed,
                cdp_dismissed_count=cdp_dismissed,
                dismissed_count=preconnect_dismissed,
            )
            browser = await _connect_browser_over_cdp(p)
            page_inventory = await _summarize_pages(browser)
            _append_bridge_log("browser_pages_detected", current_url=current_url, page_count=len(page_inventory), pages=page_inventory)
            page = await _find_target_page(browser, current_url, target_url_hint=target_url_hint, target_title_hint=target_title_hint)

            browser_note = {
                "connected": True,
                "message": "Connected to CDP browser session.",
                "matched_page_url": page.url if page else "",
                "matched_page_title": await _safe_page_title(page),
                "page_count": sum(len(ctx.pages) for ctx in browser.contexts),
                "target_url_hint": target_url_hint,
                "target_title_hint": target_title_hint,
                "pages": page_inventory,
            }
            browser_note["page_state"] = await _probe_page_state(
                page,
                correlation_id,
                probe_failed_action_name,
            )
            if page:
                page_snapshot = await _capture_page_snapshot(page, artifact_dir)
                browser_note["page_snapshot_raw"] = page_snapshot
                browser_note["page_snapshot"] = _build_snapshot_prompt_view(page_snapshot)
            workflow_context = _build_workflow_context(context, browser_note, context_file=context_file)
            failed_action_name = str(workflow_context.get("failed_action_name", "") or "")
            resolved_current_url = page.url if page else current_url
            ai_result = None
            _append_bridge_log(
                "browser_page_selected",
                current_url=current_url,
                matched=bool(page),
                matched_page_url=page.url if page else "",
                matched_page_title=await _safe_page_title(page),
                target_url_hint=target_url_hint,
                target_title_hint=target_title_hint,
                page_state=browser_note["page_state"],
                target_candidates=browser_note["page_state"].get("target_candidates", []),
                target_visible=browser_note["page_state"].get("target_visible", False),
                target_visible_text=browser_note["page_state"].get("target_visible_text", ""),
            )

            if not page:
                blank_page = await _find_blank_page(browser)
                if blank_page and current_url.strip():
                    _append_bridge_log(
                        "browser_page_fallback_navigate",
                        current_url=current_url,
                        fallback_url=current_url,
                        blank_page_url=blank_page.url,
                    )
                    try:
                        await blank_page.goto(current_url, wait_until="domcontentloaded", timeout=30000)
                        page = blank_page
                        browser_note["matched_page_url"] = page.url
                        browser_note["matched_page_title"] = await _safe_page_title(page)
                        _append_bridge_log(
                            "browser_page_navigated",
                            current_url=current_url,
                            navigated_page_url=page.url,
                            navigated_page_title=await _safe_page_title(page),
                        )
                    except Exception as exc:
                        _append_bridge_log(
                            "browser_page_navigation_failed",
                            current_url=current_url,
                            error=f"{type(exc).__name__}: {exc}",
                        )

            try:
                ai_result = await _call_openai_ai(context, image_file, browser_note, context_file=context_file)
            except Exception as exc:
                diagnostics = _exception_diagnostics(exc)
                ai_error_text = diagnostics["summary"]
                _append_bridge_log(
                    "ai_request_soft_failed",
                    current_url=current_url,
                    failed_action_name=failed_action_name,
                    error=ai_error_text,
                    **diagnostics,
                )
                ai_result = {
                    "HealStatus": "FAILED",
                    "HealMessage": "AI request failed; internal recovery unavailable.",
                    "TechnicalDetail": ai_error_text,
                    "NewSelector": "",
                    "RecoveredStep": "",
                    "NeedHuman": True,
                    "ActionPlan": [],
                    "Verification": {},
                    "VerificationResult": {},
                    "ExecutionLog": [],
                }

            ai_result["CurrentURL"] = resolved_current_url
            ai_result["CorrelationId"] = correlation_id
            ai_result["FailedActionName"] = failed_action_name
            ai_result["FailedActionSource"] = str(workflow_context.get("failed_action_source", "") or "")
            ai_result["FailedActionLogPath"] = str(workflow_context.get("failed_action_log_path", "") or "")

            if not page:
                _append_bridge_log("browser_page_missing", current_url=current_url, page_count=len(page_inventory))
                ai_result["HealStatus"] = "FAILED"
                ai_result["NeedHuman"] = True
                ai_result["HealMessage"] = "CDP connected but no matching browser page was found."
                ai_result["TechnicalDetail"] = "No page matched CurrentURL."
                ai_result["ActionPlan"] = []
                ai_result.setdefault("ExecutionLog", [])
                ai_result.setdefault("VerificationResult", {})
                return _finalize_result(ai_result, image_file, page_snapshot, include_debug=True)

            verification = ai_result.get("Verification", {})
            if _can_preverify_recovery(verification):
                pre_verified, preverification_detail = await _verify_recovery(
                    page,
                    verification,
                    [],
                )
                _append_bridge_log(
                    "pre_action_verification",
                    ok=pre_verified,
                    detail=preverification_detail,
                    verification=verification,
                    page_url=page.url,
                )
                if pre_verified:
                    ai_result["HealStatus"] = "SUCCESS"
                    ai_result["NeedHuman"] = False
                    ai_result["ActionPlan"] = []
                    ai_result["ExecutionLog"] = [{
                        "step": 0,
                        "action": "preverify",
                        "target": str(verification.get("value", "") or ""),
                        "ok": True,
                        "skipped": True,
                        "detail": "Recovery state was already satisfied; no action required.",
                    }]
                    ai_result["VerificationResult"] = {
                        "ok": True,
                        "phase": "pre_action",
                        "detail": preverification_detail,
                        "page_url": page.url,
                    }
                    ai_result["HealMessage"] = (
                        "AI 判斷：頁面在修復前已自行恢復，原本的錯誤狀態已消失，因此未執行額外操作；執行前驗證已成功。"
                    )
                    if not ai_result.get("RecoveredStep"):
                        ai_result["RecoveredStep"] = failed_action_name or "already_recovered"
                    if not ai_result.get("TechnicalDetail"):
                        ai_result["TechnicalDetail"] = preverification_detail
                    return _finalize_result(ai_result, image_file, page_snapshot, include_debug=False)

            if ai_result.get("NeedHuman") or ai_result.get("HealStatus") != "SUCCESS":
                if _can_run_soft_recovery_plan(ai_result, browser_note, image_file):
                    original_status = str(ai_result.get("HealStatus", "") or "")
                    action_plan = _coerce_action_plan(ai_result.get("ActionPlan", []))
                    _append_bridge_log(
                        "soft_confidence_plan_allowed",
                        original_status=original_status,
                        action_count=len(action_plan),
                        verification=ai_result.get("Verification", {}),
                        page_url=page.url,
                    )
                    ai_result["ActionPlan"] = action_plan
                    ai_result["HealStatus"] = "SUCCESS"
                    ai_result["NeedHuman"] = False
                    ai_result["HealMessage"] = (
                        f"{ai_result.get('HealMessage', '')} Executing the safe, verifiable recovery plan."
                    ).strip()
                    ai_result["TechnicalDetail"] = (
                        f"{ai_result.get('TechnicalDetail', '')} "
                        "Model confidence was low, but the plan contains only bounded safe actions "
                        "and a verifiable recovery condition."
                    ).strip()
                else:
                    ai_result.setdefault("ActionPlan", [])
                    ai_result.setdefault("ExecutionLog", [])
                    ai_result.setdefault("VerificationResult", {})
                    return _finalize_result(ai_result, image_file, page_snapshot, include_debug=True)

            action_plan = ai_result.get("ActionPlan", [])
            if not action_plan:
                ai_result["HealStatus"] = "FAILED"
                ai_result["NeedHuman"] = True
                ai_result["HealMessage"] = "AI returned an empty ActionPlan."
                ai_result["TechnicalDetail"] = "ActionPlan is empty."
                ai_result.setdefault("ExecutionLog", [])
                ai_result.setdefault("VerificationResult", {})
                return _finalize_result(ai_result, image_file, page_snapshot, include_debug=True)

            execution_log, page = await _execute_action_plan(page, action_plan)
            ai_result["ExecutionLog"] = execution_log

            # The active page may have changed after a click/press opened a
            # new top-level tab. Refresh artifacts so page.html and frame_*.html
            # describe the page that will actually be verified.
            try:
                page_snapshot = await _capture_page_snapshot(page, artifact_dir)
                browser_note["matched_page_url"] = page.url
                browser_note["matched_page_title"] = await _safe_page_title(page)
                browser_note["page_snapshot_raw"] = page_snapshot
                browser_note["page_snapshot"] = _build_snapshot_prompt_view(page_snapshot)
            except Exception as exc:
                _append_bridge_log(
                    "post_action_snapshot_failed",
                    page_url=page.url if page else "",
                    error=f"{type(exc).__name__}: {exc}",
                )

            verified, verification_detail = await _verify_recovery(
                page,
                verification,
                execution_log,
            )
            ai_result["VerificationResult"] = {"ok": verified, "detail": verification_detail, "page_url": page.url}

            if verified:
                ai_result["HealStatus"] = "SUCCESS"
                if not ai_result.get("RecoveredStep"):
                    ai_result["RecoveredStep"] = failed_action_name
                if not ai_result.get("TechnicalDetail"):
                    ai_result["TechnicalDetail"] = verification_detail
                return _finalize_result(ai_result, image_file, page_snapshot, include_debug=False)

            ai_result["HealStatus"] = "TIMEOUT"
            ai_result["NeedHuman"] = True
            ai_result["HealMessage"] = "Recovery verification failed."
            ai_result["TechnicalDetail"] = verification_detail
            return _finalize_result(ai_result, image_file, page_snapshot, include_debug=True)
        except Exception as exc:
            error_text = f"{type(exc).__name__}: {exc}".strip()
            traceback_text = traceback.format_exc()
            _append_bridge_log(
                "bridge_failed",
                error=error_text,
                traceback=_truncate_text(traceback_text, 4000),
                current_url=current_url,
                correlation_id=correlation_id,
            )
            return _finalize_result({
                "HealStatus": "FAILED",
                "HealMessage": "Self-healing could not complete. Please check browser CDP 9222, the Python venv, and the action plan output.",
                "TechnicalDetail": error_text,
                "NewSelector": "",
                "RecoveredStep": "",
                "NeedHuman": True,
                "ActionPlan": [],
                "Verification": {},
                "VerificationResult": {},
                "ExecutionLog": [],
                "PageSnapshot": page_snapshot if page_snapshot else {},
                "DebugArtifacts": {"screenshot_path": image_file},
            }, image_file, page_snapshot, include_debug=True)
        finally:
            # 連到既有 CDP 瀏覽器時，不主動 close，避免把 PAD 正在使用的 Edge 一起關掉。
            browser = None


# 把結果寫到磁碟，讓 PAD 可以讀到。
async def _run_and_persist(context_file: str, image_file: str, result_file: str) -> Dict[str, Any]:
    result = await run_webwright_agent(context_file, image_file)
    _write_json_atomic(result_file, result)
    return result


# 命令列入口，方便手動測試與 PAD 呼叫。
def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="PAD webwright self-healing agent")
    parser.add_argument("--context", required=True, help="Path to context.json")
    parser.add_argument("--img", required=True, help="Path to screenshot image")
    parser.add_argument(
        "--result",
        default="",
        help="Path to result.json. Defaults to <context folder>/result.json",
    )
    return parser


# 解析命令列參數、執行 bridge，並確保最後一定留下可讀的 result.json。
def main() -> int:
    args = _build_arg_parser().parse_args()
    context_file = args.context
    image_file = args.img
    result_file = args.result.strip() or os.path.join(os.path.dirname(context_file), "result.json")

    try:
        asyncio.run(_run_and_persist(context_file, image_file, result_file))
        return 0
    except Exception as exc:
        error_text = f"{type(exc).__name__}: {exc}".strip()
        _append_bridge_log(
            "bridge_main_failed",
            error=error_text,
            context_file=context_file,
            image_file=image_file,
            result_file=result_file,
        )
        fallback = {
            "HealStatus": "FAILED",
            "HealMessage": "Self-healing bridge crashed before producing a result.",
            "TechnicalDetail": error_text,
            "NewSelector": "",
            "RecoveredStep": "",
            "NeedHuman": True,
            "ActionPlan": [],
            "Verification": {},
            "VerificationResult": {},
            "ExecutionLog": [],
        }
        try:
            _write_json_atomic(result_file, fallback)
        except Exception:
            pass
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
