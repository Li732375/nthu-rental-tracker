#!/usr/bin/env python3
"""
NTHU 國家住都中心 — 招租快訊自動爬蟲 & README 產生器
====================================================
每週自動抓取住宅區招租快訊（前五筆），與上次快照做差異比對，
並將結果寫入 README.md（含差異摘要報告）。

技術說明：
  - 使用 curl_cffi 模擬 Chrome 瀏覽器 TLS 指紋以繞過 403 防護
  - 使用 BeautifulSoup4 + lxml 解析 HTML
  - 差異比對基於 MD5 hash（標題 + URL）
"""

import json
import hashlib
import logging
import re
import sys
from datetime import datetime
from pathlib import Path

from curl_cffi import requests as curl_requests
from bs4 import BeautifulSoup

# ──────────────────────────────────────────────
#  常數設定
# ──────────────────────────────────────────────
TARGET_URL = "https://www.nthurc.org.tw/leasing-news/residential-area"
BASE_SITE  = "https://www.nthurc.org.tw"
MAX_ITEMS  = 5  # 只取前五筆

BASE_DIR    = Path(__file__).resolve().parent
DATA_FILE   = BASE_DIR / "data.json"
README_FILE = BASE_DIR / "README.md"

REQUEST_TIMEOUT = 30  # 秒

# ──────────────────────────────────────────────
#  日誌設定
# ──────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ══════════════════════════════════════════════
#  第一部分：爬蟲模組
# ══════════════════════════════════════════════

def fetch_page(url: str) -> str:
    """使用 curl_cffi 抓取頁面 HTML。

    模擬 Chrome 瀏覽器的 TLS 指紋以避免 403 Forbidden。
    """
    log.info("正在抓取頁面：%s", url)
    try:
        resp = curl_requests.get(
            url,
            impersonate="chrome",
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        # curl_cffi 回傳的 text 預設會自動偵測編碼
        log.info(
            "頁面抓取成功（HTTP %d），內容長度：%d bytes",
            resp.status_code,
            len(resp.text),
        )
        return resp.text
    except Exception as e:
        log.error("抓取頁面失敗：%s", e)
        raise


def parse_date(raw: str) -> str:
    """將各種日期格式統一轉為 YYYY-MM-DD。

    支援格式：
      - YYYY.MM.DD、YYYY/MM/DD、YYYY-MM-DD（西元年）
      - YYY.MM.DD（民國年，如 114.01.01 → 2025-01-01）
    """
    raw = raw.strip()
    if not raw:
        return "-"

    for sep in (".", "/", "-"):
        parts = raw.split(sep)
        if len(parts) == 3:
            try:
                year  = int(parts[0])
                month = int(parts[1])
                day   = int(parts[2])
                # 民國年轉西元年（民國年 < 1911）
                if year < 1911:
                    year += 1911
                return f"{year:04d}-{month:02d}-{day:02d}"
            except ValueError:
                continue

    return raw  # 無法解析時原樣回傳


def extract_items(html: str) -> list[dict]:
    """從 HTML 中解析招租快訊列表項目。

    頁面結構（2025 年確認）：
      <ul class="relative z-0 space-y-10 text-dark-500 mt-8">
        <li class="relative bg-white rounded-lg shadow-sm border ...">
          <div class="absolute bg-gray-200 ...">YYYY.MM.DD</div>
          <div class="px-4 sm:px-8 pt-8 pb-4 word-wrap-anywhere">
            <a href="/leasing-news/{id}">標題文字</a>
          </div>
        </li>
        ...
      </ul>

    回傳格式：[{"title": str, "date": str, "url": str}, ...]
    """
    soup = BeautifulSoup(html, "lxml")
    items: list[dict] = []
    date_re = re.compile(r"\d{2,4}[./\-]\d{1,2}[./\-]\d{1,2}")

    # ── 主策略：找到列表容器中的 <li> 項目 ──
    # 列表容器 class 含 "space-y-10"
    container = soup.find("ul", class_=lambda c: c and "space-y-10" in c)

    if container:
        log.info("找到列表容器 <ul>")
        li_items = container.find_all("li", recursive=False)
        log.info("容器中有 %d 個 <li> 項目", len(li_items))

        for li in li_items[:MAX_ITEMS]:
            # 提取日期（在第一個含日期文字的 div 中）
            date_str = "-"
            date_div = li.find("div", class_=lambda c: c and "bg-gray-200" in c)
            if date_div:
                text = date_div.get_text(strip=True)
                match = date_re.search(text)
                if match:
                    date_str = parse_date(match.group())

            # 提取標題連結（在含 word-wrap-anywhere 的 div 中）
            content_div = li.find(
                "div",
                class_=lambda c: c and "word-wrap-anywhere" in c,
            )
            if content_div:
                link = content_div.find("a", href=True)
                if link:
                    title = link.get_text(strip=True)
                    href  = link["href"]
                    if href.startswith("/"):
                        href = f"{BASE_SITE}{href}"

                    items.append({
                        "title": title,
                        "date":  date_str,
                        "url":   href,
                    })
                    continue

            # 備用：在整個 <li> 中找第一個連結
            link = li.find("a", href=True)
            if link:
                title = link.get_text(strip=True)
                href  = link["href"]
                if href.startswith("/"):
                    href = f"{BASE_SITE}{href}"
                if title and len(title) > 3:
                    items.append({
                        "title": title,
                        "date":  date_str,
                        "url":   href,
                    })

    # ── 備用策略：若主策略失敗，掃描所有 leasing-news 連結 ──
    if len(items) < 2:
        log.warning("主策略結果不足（%d 筆），啟用備用策略...", len(items))
        items.clear()
        seen = set()

        news_links = soup.find_all(
            "a",
            href=re.compile(r"/leasing-news/\d+"),
        )
        for link in news_links:
            if len(items) >= MAX_ITEMS:
                break

            title = link.get_text(strip=True)
            href  = link["href"]
            if not title or len(title) < 4 or title in seen:
                continue

            seen.add(title)
            if href.startswith("/"):
                href = f"{BASE_SITE}{href}"

            # 往上找日期
            date_str = "-"
            parent = link.parent
            for _ in range(5):
                if parent is None:
                    break
                text  = parent.get_text()
                match = date_re.search(text)
                if match:
                    date_str = parse_date(match.group())
                    break
                parent = parent.parent

            items.append({
                "title": title,
                "date":  date_str,
                "url":   href,
            })

    log.info("共解析出 %d 筆招租快訊", len(items))
    return items[:MAX_ITEMS]


def sort_items_by_date(items: list[dict]) -> list[dict]:
    """依日期由新到舊排序。無日期者排在最後。"""
    def sort_key(item: dict) -> str:
        d = item.get("date", "-")
        return d if d != "-" else "0000-00-00"
    return sorted(items, key=sort_key, reverse=True)


# ══════════════════════════════════════════════
#  第二部分：差異比對模組
# ══════════════════════════════════════════════

def item_hash(item: dict) -> str:
    """為單筆項目產生唯一 hash（基於標題 + URL）。"""
    raw = f"{item['title']}|{item['url']}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def load_previous_data() -> dict:
    """讀取上一次的資料快照。"""
    if not DATA_FILE.exists():
        log.info("尚無先前資料（%s 不存在）", DATA_FILE)
        return {"last_updated": None, "items": []}

    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        log.info(
            "已載入先前資料（%d 筆，更新時間：%s）",
            len(data.get("items", [])),
            data.get("last_updated"),
        )
        return data
    except (json.JSONDecodeError, KeyError) as e:
        log.warning("先前資料格式錯誤，將視為空資料：%s", e)
        return {"last_updated": None, "items": []}


def save_current_data(items: list[dict], now: str) -> None:
    """將本次資料存為快照。"""
    data = {
        "last_updated": now,
        "items": items,
    }
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    log.info("資料快照已儲存至 %s", DATA_FILE)


def compute_diff(
    old_items: list[dict],
    new_items: list[dict],
) -> dict:
    """比對新舊資料，回傳差異結果。

    回傳格式：
    {
        "added":           [新增項目...],
        "removed":         [移除項目...],
        "changed":         [{"old": ..., "new": ...}, ...],
        "unchanged_count": int,
    }
    """
    old_map = {item_hash(it): it for it in old_items}
    new_map = {item_hash(it): it for it in new_items}

    old_keys = set(old_map.keys())
    new_keys = set(new_map.keys())

    added   = [new_map[k] for k in (new_keys - old_keys)]
    removed = [old_map[k] for k in (old_keys - new_keys)]

    # 檢查共同項目的內容變更（例如日期修正）
    changed = []
    common_keys = old_keys & new_keys
    for k in common_keys:
        if old_map[k] != new_map[k]:
            changed.append({"old": old_map[k], "new": new_map[k]})

    unchanged_count = len(common_keys) - len(changed)

    log.info(
        "差異比對結果 — 新增：%d、移除：%d、變更：%d、未變：%d",
        len(added), len(removed), len(changed), unchanged_count,
    )

    return {
        "added":           added,
        "removed":         removed,
        "changed":         changed,
        "unchanged_count": unchanged_count,
    }


# ══════════════════════════════════════════════
#  第三部分：Markdown 產生器
# ══════════════════════════════════════════════

def generate_diff_summary(diff: dict, now: str, is_first_run: bool) -> str:
    """產生差異摘要報告的 Markdown。"""
    lines: list[str] = [
        f"# 📊 本週招租快訊更新摘要（{now}）",
        "",
    ]

    if is_first_run:
        lines += [
            "> 🆕 首次執行，無先前資料可供比對。",
            "",
            "---",
            "",
        ]
        return "\n".join(lines)

    has_changes = diff["added"] or diff["removed"] or diff["changed"]

    if not has_changes:
        lines += [
            "> ✅ 本週無新增或變動項目。",
            "",
            "---",
            "",
        ]
        return "\n".join(lines)

    # ── 新增項目 ──
    if diff["added"]:
        lines += ["## ✅ 新增項目", ""]
        for item in diff["added"]:
            date_label = f"（{item['date']}）" if item["date"] != "-" else ""
            lines.append(f"- [{item['title']}]({item['url']}){date_label}")
        lines.append("")

    # ── 已移除項目 ──
    if diff["removed"]:
        lines += ["## ❌ 已移除項目", ""]
        for item in diff["removed"]:
            date_label = f"（{item['date']}）" if item["date"] != "-" else ""
            lines.append(f"- ~~{item['title']}~~{date_label}")
        lines.append("")

    # ── 內容變更項目 ──
    if diff["changed"]:
        lines += ["## 🔄 內容變更項目", ""]
        for change in diff["changed"]:
            old_item = change["old"]
            new_item = change["new"]
            lines.append(f"- **{new_item['title']}**")
            if old_item["date"] != new_item["date"]:
                lines.append(
                    f"  - 日期：{old_item['date']} → {new_item['date']}"
                )
            if old_item["url"] != new_item["url"]:
                lines.append("  - 連結已更新")
        lines.append("")

    # ── 統計 ──
    lines += [
        f"> 📈 統計：新增 {len(diff['added'])} 筆、"
        f"移除 {len(diff['removed'])} 筆、"
        f"變更 {len(diff['changed'])} 筆、"
        f"未變 {diff['unchanged_count']} 筆",
        "",
        "---",
        "",
    ]

    return "\n".join(lines)


def generate_table(items: list[dict], now: str) -> str:
    """產生招租快訊的 Markdown 表格。"""
    lines: list[str] = [
        f"## 📋 招租快訊（更新時間：{now}）",
        "",
        "| 日期 | 標題 |",
        "|------|------|",
    ]

    for item in items:
        date  = item.get("date", "-")
        title = item.get("title", "無標題")
        url   = item.get("url", TARGET_URL)
        lines.append(f"| {date} | [{title}]({url}) |")

    lines.append("")
    return "\n".join(lines)


def generate_readme(
    items: list[dict],
    diff: dict,
    now: str,
    is_first_run: bool,
) -> str:
    """組合完整的 README.md 內容。"""
    parts: list[str] = [
        generate_diff_summary(diff, now, is_first_run),
        generate_table(items, now),
        "",
        "---",
        "",
        "## ℹ️ 關於本專案",
        "",
        "本專案透過 GitHub Actions 每週自動抓取 "
        f"[國家住都中心招租快訊]({TARGET_URL})，",
        "將最新資料轉為 Markdown 表格並更新至本 README，",
        "同時在上方顯示與上週的差異摘要。",
        "",
        "### 🔧 技術細節",
        "",
        "- **爬蟲引擎**：Python + curl_cffi + BeautifulSoup4",
        "- **差異比對**：基於 MD5 hash 的結構化比對",
        "- **自動排程**：GitHub Actions（每週一 20:00 台灣時間）",
        "- **資料快照**：`data.json` 記錄上次抓取結果",
        "",
        "本專案完全由 Antigravity 開發製作",
        f"最後更新：{now}*",
        "",
    ]
    return "\n".join(parts)


# ══════════════════════════════════════════════
#  第四部分：主程式
# ══════════════════════════════════════════════

def main() -> None:
    """主要執行流程。"""
    now = datetime.now().strftime("%Y-%m-%d")
    log.info("=" * 50)
    log.info("招租快訊自動爬蟲啟動（%s）", now)
    log.info("=" * 50)

    # 1) 抓取頁面
    try:
        html = fetch_page(TARGET_URL)
    except Exception as e:
        log.error("無法抓取頁面，程式終止：%s", e)
        sys.exit(1)

    # 2) 解析項目
    items = extract_items(html)
    if not items:
        log.warning("⚠️ 未解析到任何招租快訊項目！")
        log.warning("網站結構可能已變動，請檢查 crawler.py 的解析邏輯。")

    # 3) 排序（新→舊）
    items = sort_items_by_date(items)
    log.info("排序後的項目：")
    for i, item in enumerate(items, 1):
        log.info("  %d. [%s] %s", i, item["date"], item["title"])

    # 4) 讀取先前資料 & 差異比對
    prev_data    = load_previous_data()
    old_items    = prev_data.get("items", [])
    is_first_run = prev_data.get("last_updated") is None

    diff = compute_diff(old_items, items)

    # 5) 儲存本次資料
    save_current_data(items, now)

    # 6) 產生 README.md
    readme_content = generate_readme(items, diff, now, is_first_run)
    with open(README_FILE, "w", encoding="utf-8") as f:
        f.write(readme_content)
    log.info("README.md 已更新：%s", README_FILE)

    # 7) 輸出結果摘要
    log.info("=" * 50)
    log.info("執行完成！")
    log.info("  抓取項目：%d 筆", len(items))
    if is_first_run:
        log.info("  首次執行，無差異比對")
    else:
        log.info(
            "  新增：%d、移除：%d、變更：%d",
            len(diff["added"]),
            len(diff["removed"]),
            len(diff["changed"]),
        )
    log.info("=" * 50)


if __name__ == "__main__":
    main()
