"""sources.json に書かれた情報源から記事を集める。

情報源の追加・変更は sources.json だけで完結する(このファイルは触らない)。
"""

from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote_plus

import feedparser
import httpx

ROOT = Path(__file__).resolve().parent

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

GOOGLE_NEWS = "https://news.google.com/rss/search?q={q}&hl=ja&gl=JP&ceid=JP:ja"


@dataclass
class Item:
    title: str
    link: str
    source: str
    kind: str
    published: dt.datetime | None


def _norm(title: str) -> str:
    return re.sub(r"\s+", "", title).lower()


def _source_url(src: dict, days: int) -> str:
    if src["type"] == "google_news":
        return GOOGLE_NEWS.format(q=quote_plus(f"{src['query']} when:{days}d"))
    if src["type"] == "rss":
        return src["url"]
    raise ValueError(f"未知の type: {src['type']}")


def _published(entry) -> dt.datetime | None:
    tm = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
    if not tm:
        return None
    return dt.datetime(*tm[:6], tzinfo=dt.timezone.utc)


def collect(days: int, max_items: int, log) -> list[Item]:
    try:
        cfg = json.loads((ROOT / "sources.json").read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as e:
        raise SystemExit(
            f"sources.json の書き方に誤りがあります({e.lineno}行目のあたり)。\n"
            "  よくある原因: 行末のカンマの付け忘れ・付けすぎ、引用符の閉じ忘れ"
        ) from e
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)

    items: list[Item] = []
    seen: set[str] = set()

    with httpx.Client(headers={"User-Agent": UA}, timeout=30.0, follow_redirects=True) as cli:
        for src in cfg["sources"]:
            url = _source_url(src, days)
            try:
                resp = cli.get(url)
                resp.raise_for_status()
            except Exception as e:
                log(f"  {src['label']}: 取得失敗 ({type(e).__name__}) — スキップ")
                continue

            feed = feedparser.parse(resp.content)
            added = 0
            for e in feed.entries:
                title = (getattr(e, "title", "") or "").strip()
                link = (getattr(e, "link", "") or "").strip()
                if not title or not link:
                    continue
                pub = _published(e)
                if pub and pub < cutoff:
                    continue
                key = _norm(title)
                if key in seen:
                    continue
                seen.add(key)
                # Google News のタイトルは "見出し - 媒体名" 形式
                name = src["label"]
                if src["type"] == "google_news" and " - " in title:
                    title, _, name = title.rpartition(" - ")
                items.append(Item(title.strip(), link, name.strip(), src.get("kind", "news"), pub))
                added += 1
            log(f"  {src['label']}: {added}件")

    items.sort(key=lambda i: i.published or dt.datetime.min.replace(tzinfo=dt.timezone.utc),
               reverse=True)
    return items[:max_items]


def format_for_prompt(items: list[Item]) -> str:
    """モデルに渡す記事リスト。URLは渡さない(捏造を防ぐため機械側で解決する)。"""
    by_kind: dict[str, list[Item]] = {}
    for it in items:
        by_kind.setdefault(it.kind, []).append(it)

    out: list[str] = []
    n = 0
    for kind, group in by_kind.items():
        out.append(f"\n【{kind}】")
        for it in group:
            n += 1
            out.append(f"[{n}] {it.title} | {it.source}")
    return "\n".join(out)
