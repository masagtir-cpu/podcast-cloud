"""docs/episodes.json からポッドキャストRSS(docs/feed.xml)を作る。

GitHub Pages で docs/ を公開すると、そのまま
  https://<ユーザー名>.github.io/<リポジトリ名>/feed.xml
をポッドキャストアプリに登録できる。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
from email.utils import format_datetime
from pathlib import Path
from xml.sax.saxutils import escape

import tts_engines as te

ROOT = Path(__file__).resolve().parent
DOCS = ROOT / "docs"
MANIFEST = DOCS / "episodes.json"
FEED = DOCS / "feed.xml"


def feed_base(cfg: dict) -> str:
    """MP3の配信元URL。設定になければ環境変数(GitHub Actionsが渡す)を見る。"""
    return (cfg["feed"].get("base_url")
            or os.environ.get("PODCAST_BASE_URL", "")).rstrip("/")


def load_episodes() -> list[dict]:
    if not MANIFEST.is_file():
        return []
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def record_episode(entry: dict, keep: int = 0) -> list[dict]:
    """1回ぶんを episodes.json に追記する(同じ日付があれば置き換える)。

    keep が1以上なら新しい方から keep 件だけ残し、あふれた分を戻り値で返す。
    呼び出し側はその音声ファイルを配信先から削除する。
    """
    DOCS.mkdir(parents=True, exist_ok=True)
    eps = [e for e in load_episodes() if e["date"] != entry["date"]]
    eps.append(entry)
    eps.sort(key=lambda e: e["date"], reverse=True)

    dropped: list[dict] = []
    if keep and len(eps) > keep:
        dropped = eps[keep:]
        eps = eps[:keep]

    MANIFEST.write_text(json.dumps(eps, ensure_ascii=False, indent=2), encoding="utf-8")
    return dropped


def _duration(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def build(cfg: dict) -> Path:
    f = cfg["feed"]
    prog = cfg["program"]
    site = (f.get("site_url") or os.environ.get("PODCAST_SITE_URL", "")).rstrip("/")
    base = feed_base(cfg)
    if not base:
        print("警告: base_url も PODCAST_BASE_URL も未設定です。enclosure のURLが不完全になります",
              file=sys.stderr)

    # 古い記録にはURLが入っていないので、ここで補って保存しておく
    # (index.html は episodes.json のURLをそのまま使うため)
    eps = load_episodes()
    if base and any(not e.get("url") for e in eps):
        for e in eps:
            e.setdefault("url", f"{base}/{e['file']}")
        MANIFEST.write_text(json.dumps(eps, ensure_ascii=False, indent=2), encoding="utf-8")

    items = []
    for e in eps:
        url = e.get("url") or f"{base}/{e['file']}"
        pub = dt.datetime.fromisoformat(e["date"]).replace(
            hour=6, tzinfo=dt.timezone(dt.timedelta(hours=9)))
        items.append(f"""    <item>
      <title>{escape(e['title'])}</title>
      <description>{escape(e.get('summary', ''))}</description>
      <pubDate>{format_datetime(pub)}</pubDate>
      <guid isPermaLink="false">{escape(e['date'])}-{escape(prog['slug'])}</guid>
      <enclosure url="{escape(url)}" length="{e['bytes']}" type="audio/mpeg"/>
      <itunes:duration>{_duration(e['seconds'])}</itunes:duration>
      <itunes:explicit>{'true' if f.get('explicit') else 'false'}</itunes:explicit>
    </item>""")

    image = f.get("image", "")
    image_tag = f'\n    <itunes:image href="{escape(image)}"/>' if image else ""

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"
     xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"
     xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>{escape(prog['title'])}</title>
    <link>{escape(site or base)}</link>
    <description>{escape(f['description'])}</description>
    <language>{escape(f['language'])}</language>
    <itunes:author>{escape(f['author'])}</itunes:author>
    <itunes:summary>{escape(f['description'])}</itunes:summary>
    <itunes:category text="{escape(f['category'])}"/>
    <itunes:explicit>{'true' if f.get('explicit') else 'false'}</itunes:explicit>{image_tag}
    <atom:link href="{escape(site)}/feed.xml" rel="self" type="application/rss+xml"/>
{chr(10).join(items)}
  </channel>
</rss>
"""
    DOCS.mkdir(parents=True, exist_ok=True)
    FEED.write_text(xml, encoding="utf-8")
    return FEED


if __name__ == "__main__":
    cfg = te.load_config()
    path = build(cfg)
    print(f"{path} を書き出しました ({len(load_episodes())}エピソード)")
