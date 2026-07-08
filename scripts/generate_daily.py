#!/usr/bin/env python3
"""科技日报生成器：抓取多源资讯 → Markdown → 推送飞书/钉钉"""

from __future__ import annotations

import hashlib
import hmac
import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from typing import Any

USER_AGENT = "YXDailyTechnology/1.0 (+https://github.com)"
TZ_CN = timezone(timedelta(hours=8))
FETCH_TIMEOUT = 12

# ponytail: 中文源在前、英文在后；单源失败不影响整体
CN_RSS_SOURCES: list[tuple[str, str, int]] = [
    ("36氪", "https://36kr.com/feed", 10),
    ("IT之家", "https://www.ithome.com/rss/", 10),
    ("少数派", "https://sspai.com/feed", 10),
    ("爱范儿", "https://www.ifanr.com/feed", 10),
    ("钛媒体", "https://www.tmtpost.com/rss.xml", 10),
    ("雷锋网", "https://www.leiphone.com/feed", 10),
]

EN_RSS_SOURCES: list[tuple[str, str, int]] = [
    ("Hacker News", "https://hnrss.org/frontpage", 8),
    ("TechCrunch", "https://techcrunch.com/feed/", 5),
    ("The Verge", "https://www.theverge.com/rss/index.xml", 5),
    ("Ars Technica", "https://feeds.arstechnica.com/arstechnica/index", 5),
    ("GitHub Blog", "https://github.blog/feed/", 4),
]

RSS_SOURCES = CN_RSS_SOURCES + EN_RSS_SOURCES

V2EX_HOT_URL = "https://www.v2ex.com/api/topics/hot.json"


def fetch_text(url: str, timeout: int = FETCH_TIMEOUT) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode(errors="replace")


def fetch_json(url: str, timeout: int = FETCH_TIMEOUT) -> Any:
    return json.loads(fetch_text(url, timeout))


def post_json(url: str, payload: dict, timeout: int = FETCH_TIMEOUT) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json; charset=utf-8"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode()
        result = json.loads(body) if body else {}
        if result.get("errcode", result.get("StatusCode", 0)) not in (0, None):
            raise RuntimeError(f"Webhook 返回错误: {body}")


def today_cn() -> datetime:
    return datetime.now(TZ_CN)


def _rss_tag(elem: ET.Element, local: str) -> ET.Element | None:
    if elem.tag.endswith(local):
        return elem
    for child in elem:
        if child.tag.endswith(local):
            return child
    return None


def fetch_rss(name: str, url: str, limit: int = 5) -> list[dict]:
    try:
        root = ET.fromstring(fetch_text(url))
    except (urllib.error.URLError, ET.ParseError, TimeoutError) as e:
        print(f"  跳过 {name}: {e}", file=sys.stderr)
        return []

    channel = root.find("channel")
    entries = root.findall(".//{*}item") or root.findall(".//{*}entry")
    if channel is not None:
        entries = channel.findall("{*}item") or channel.findall("{*}entry") or entries

    items = []
    for entry in entries[:limit]:
        title_el = _rss_tag(entry, "title")
        link_el = _rss_tag(entry, "link")
        title = (title_el.text or "").strip() if title_el is not None else ""
        link = ""
        if link_el is not None:
            link = (link_el.text or link_el.get("href", "")).strip()
        if title and link:
            items.append({"title": title, "url": link, "source": name})
    print(f"  {name}: {len(items)} 条")
    return items


def fetch_v2ex_hot(limit: int = 6) -> list[dict]:
    try:
        topics = fetch_json(V2EX_HOT_URL, timeout=6)
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as e:
        print(f"  跳过 V2EX: {e}", file=sys.stderr)
        return []
    items = []
    for i, t in enumerate(topics[:limit], 1):
        items.append({
            "rank": i,
            "title": t.get("title", ""),
            "url": t.get("url") or f"https://www.v2ex.com/t/{t.get('id', '')}",
            "replies": t.get("replies", 0),
            "node": (t.get("node") or {}).get("title", ""),
        })
    print(f"  V2EX: {len(items)} 条")
    return items


def build_report(date_str: str) -> str:
    sections: dict[str, list[dict]] = {}
    v2ex: list[dict] = []

    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = {
            pool.submit(fetch_rss, name, url, limit): name
            for name, url, limit in RSS_SOURCES
        }
        futures[pool.submit(fetch_v2ex_hot)] = "V2EX"
        for fut in as_completed(futures):
            name = futures[fut]
            if name == "V2EX":
                v2ex = fut.result()
            else:
                sections[name] = fut.result()

    lines = [
        f"# 科技日报 · {date_str}",
        "",
        f"> 自动生成于 {today_cn().strftime('%Y-%m-%d %H:%M')} (北京时间)",
        "",
    ]

    for name, _, _ in CN_RSS_SOURCES:
        items = sections.get(name, [])
        if not items:
            continue
        lines += [f"## {name}", ""]
        for i, it in enumerate(items, 1):
            lines.append(f"{i}. [{it['title']}]({it['url']})")
        lines.append("")

    if v2ex:
        lines += ["## V2EX 热议", ""]
        for it in v2ex:
            node = f"「{it['node']}」" if it["node"] else ""
            lines.append(f"{it['rank']}. [{it['title']}]({it['url']}) {node} — 💬 {it['replies']}")
        lines.append("")

    for name, _, _ in EN_RSS_SOURCES:
        items = sections.get(name, [])
        if not items:
            continue
        lines += [f"## {name}", ""]
        for i, it in enumerate(items, 1):
            lines.append(f"{i}. [{it['title']}]({it['url']})")
        lines.append("")

    lines += ["---", "", "*由 YXDailyTechnology 每日自动更新*"]
    return "\n".join(lines)


def build_readme(latest_md: str, archive: list[str]) -> str:
    body = latest_md.split("\n", 1)[1] if "\n" in latest_md else latest_md
    archive_lines = "\n".join(f"- [{d}](reports/{d}.md)" for d in sorted(archive, reverse=True)[:30])
    return f"""# YX 科技日报

每日自动抓取科技前沿资讯，整理成 Markdown 日报。

## 今日日报

{body}

## 历史归档

{archive_lines or '_暂无_'}

## 快速开始

1. Fork 本仓库，启用 GitHub Actions
2. 在 **Settings → Secrets and variables → Actions** 添加：

| Secret | 说明 |
|--------|------|
| `FEISHU_WEBHOOK_URL` | 飞书群机器人 Webhook（可选） |
| `DINGTALK_WEBHOOK_URL` | 钉钉群机器人 Webhook（可选） |
| `DINGTALK_SECRET` | 钉钉加签密钥（开启加签时填写） |

3. 手动测试：**Actions → Daily Tech Report → Run workflow**

## 数据源

36氪 · IT之家 · 少数派 · 爱范儿 · 钛媒体 · 雷锋网 · V2EX（可达时） · Hacker News · TechCrunch · The Verge · Ars Technica · GitHub Blog

## 自定义

编辑 `scripts/generate_daily.py` 中的 `CN_RSS_SOURCES` / `EN_RSS_SOURCES` 列表即可增删数据源。
"""


def dingtalk_sign(secret: str) -> tuple[str, str]:
    ts = str(round(time.time() * 1000))
    string_to_sign = f"{ts}\n{secret}"
    hmac_code = hmac.new(secret.encode(), string_to_sign.encode(), hashlib.sha256).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
    return ts, sign


def push_feishu(webhook: str, title: str, md: str) -> None:
    text = md[:4000]
    payload = {
        "msg_type": "interactive",
        "card": {
            "header": {"title": {"tag": "plain_text", "content": title}},
            "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": text}}],
        },
    }
    post_json(webhook, payload)


def push_dingtalk(webhook: str, secret: str | None, title: str, md: str) -> None:
    url = webhook
    if secret:
        ts, sign = dingtalk_sign(secret)
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}timestamp={ts}&sign={sign}"
    summary_lines = [ln for ln in md.splitlines() if ln.startswith(("##", "- ", "1.", "2.", "3.", "4.", "5."))][:25]
    text = "\n\n".join(summary_lines)[:4000]
    payload = {"msgtype": "markdown", "markdown": {"title": title, "text": f"### {title}\n\n{text}"}}
    post_json(url, payload)


def main() -> int:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    reports_dir = os.path.join(root, "reports")
    os.makedirs(reports_dir, exist_ok=True)

    date_str = today_cn().strftime("%Y-%m-%d")
    report_path = os.path.join(reports_dir, f"{date_str}.md")

    print(f"生成日报: {date_str}")
    md = build_report(date_str)

    with open(report_path, "w", encoding="utf-8") as f:
        f.write(md)
    print(f"已写入 {report_path}")

    archive = [f.replace(".md", "") for f in os.listdir(reports_dir) if f.endswith(".md")]
    with open(os.path.join(root, "README.md"), "w", encoding="utf-8") as f:
        f.write(build_readme(md, archive))
    print("已更新 README.md")

    title = f"科技日报 {date_str}"
    feishu_url = os.environ.get("FEISHU_WEBHOOK_URL", "").strip()
    if feishu_url:
        try:
            push_feishu(feishu_url, title, md)
            print("已推送飞书")
        except Exception as e:
            print(f"飞书推送失败: {e}", file=sys.stderr)

    dingtalk_url = os.environ.get("DINGTALK_WEBHOOK_URL", "").strip()
    if dingtalk_url:
        try:
            secret = os.environ.get("DINGTALK_SECRET", "").strip() or None
            push_dingtalk(dingtalk_url, secret, title, md)
            print("已推送钉钉")
        except Exception as e:
            print(f"钉钉推送失败: {e}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
