"""1回ぶんのエピソードを最初から最後まで作る。

  記事収集 -> ダイジェスト -> 台本 -> 音声合成 -> MP3

番組の内容を変えるときは config.toml と sources.json だけを編集すればよい。

例:
    uv run make_episode.py
    uv run make_episode.py --skip-audio     # 台本までで止める(確認用)
    uv run make_episode.py --engine gemini  # TTSエンジンを一時的に変える
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

import build_feed as bf
import collect as co
import generate as ge
import tts_engines as te

ROOT = Path(__file__).resolve().parent
EP_DIR = ROOT / "output" / "episodes"
AUDIO_DIR = ROOT / "output" / "audio"
LOG_DIR = ROOT / "logs"

_log_fh = None


def log(msg: str) -> None:
    line = f"[{dt.datetime.now():%H:%M:%S}] {msg}"
    print(line, flush=True)
    if _log_fh:
        _log_fh.write(line + "\n")
        _log_fh.flush()


def recent_topics(days: int) -> str:
    """過去に扱った見出しを集め、同じ話題の繰り返しを避けるために渡す。

    クラウド実行では毎回まっさらなマシンで動き `output/` が空なので、
    リポジトリにコミットされる episodes.json を主な情報源にする。
    """
    cutoff = dt.date.today() - dt.timedelta(days=days)
    heads: list[str] = []

    for e in bf.load_episodes():
        try:
            if dt.date.fromisoformat(e["date"]) < cutoff:
                continue
        except (ValueError, KeyError):
            continue
        heads += [h.strip() for h in e.get("summary", "").split(" / ") if h.strip()]

    # 手元で連続実行する時は、ダイジェスト本文のほうが見出しを多く拾える
    for md in sorted(EP_DIR.glob("*/*_digest.md")):
        if dt.datetime.fromtimestamp(md.stat().st_mtime).date() < cutoff:
            continue
        heads += [l[4:].strip() for l in md.read_text(encoding="utf-8").splitlines()
                  if l.startswith("### ")]

    heads = [h for h in dict.fromkeys(heads) if h and h != "その他の短信"]
    if not heads:
        return ""
    joined = "\n".join(f"- {h}" for h in heads[-40:])
    return ("\n# 直近で既に取り上げた話題\n"
            "以下は過去回で扱ったものです。原則として再度取り上げないでください。\n"
            "続報がある場合のみ、新しく判明した部分だけを「続報」と明示して扱ってください。\n"
            f"{joined}\n")


def synth_with_fallback(lines: list[te.Line], cfg: dict, engine: str) -> tuple[bytes, str]:
    order = [engine]
    fb = cfg["tts"].get("fallback", "")
    if fb and fb != engine:
        order.append(fb)

    errors = []
    for key in order:
        label, fn = te.ENGINES[key]
        log(f"音声合成: {label}")
        try:
            return te.strip_wav(fn(lines, cfg, lambda m: log(f"  {m}"))), key
        except Exception as e:
            log(f"  {label} 失敗: {e}")
            errors.append(f"{label}: {e}")
    raise RuntimeError("全TTSエンジンが失敗しました\n  " + "\n  ".join(errors))


def main() -> int:
    global _log_fh

    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-audio", action="store_true")
    ap.add_argument("--engine", default=None)
    args = ap.parse_args()

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    _log_fh = open(LOG_DIR / "make_episode.log", "a", encoding="utf-8")

    cfg = te.load_config()
    prog = cfg["program"]
    today = dt.date.today()
    tag = today.isoformat()
    ep_dir = EP_DIR / tag
    ep_dir.mkdir(parents=True, exist_ok=True)

    log("=" * 56)
    log(f"番組: {prog['title']} / 目標 {prog['minutes']}分 / 対象 {prog['days']}日ぶん")

    # 1. 記事収集
    log("記事を収集します")
    items = co.collect(prog["days"], prog["max_items"], log)
    if not items:
        log("記事が1件も取れませんでした。sources.json を確認してください")
        return 1
    log(f"収集: {len(items)}件")
    items_text = co.format_for_prompt(items)
    (ep_dir / f"{tag}_items.txt").write_text(
        "\n".join(f"{i.title} | {i.source} | {i.link}" for i in items), encoding="utf-8")

    # 2. ダイジェスト
    log("ダイジェストを生成します")
    digest = ge.make_digest(items_text, cfg, recent_topics(prog["days"] * 3), log)
    (ep_dir / f"{tag}_digest.md").write_text(digest, encoding="utf-8")

    # 3. 台本
    log("台本を生成します")
    script = ge.make_script(digest, cfg, f"{today:%Y年%m月%d日}", log)
    script_path = ep_dir / f"{tag}_script.txt"
    script_path.write_text(script, encoding="utf-8")

    lines = te.parse_script(script_path)
    chars = sum(len(l.text) for l in lines)
    log(f"台本: {len(lines)}行 / {chars}字 (見込み {chars/470:.1f}分)")

    if args.skip_audio:
        log("--skip-audio のため音声合成は行いません")
        return 0

    # 4. 音声
    engine = args.engine or cfg["tts"]["engine"]
    pcm, used = synth_with_fallback(lines, cfg, engine)
    rate = cfg["audio"]["sample_rate"]
    # ファイル名は半角英数にする(配信URLやGitHubのアセット名で崩れないため)
    mp3 = AUDIO_DIR / f"{tag}_{prog['slug']}.mp3"
    te.pcm_to_mp3(pcm, mp3, rate, cfg["audio"]["mp3_bitrate"],
                  cfg["audio"].get("normalize", False))
    seconds = te.pcm_duration_sec(pcm, rate)

    log(f"完成: {mp3.name}  {seconds/60:.1f}分  "
        f"{mp3.stat().st_size/1024/1024:.1f}MB  (engine={used})")

    # 5. 配信用のRSSを更新
    heads = [l[4:].strip() for l in digest.splitlines() if l.startswith("### ")]
    dropped = bf.record_episode({
        "date": tag,
        "title": f"{prog['title']} {today:%Y年%m月%d日}",
        "file": mp3.name,
        "url": f"{bf.feed_base(cfg)}/{mp3.name}",
        "bytes": mp3.stat().st_size,
        "seconds": round(seconds),
        "summary": " / ".join(heads[:3]) or prog["theme"],
    }, cfg["feed"].get("keep_episodes", 0))
    if dropped:
        # 実際の削除はワークフロー側(gh release delete-asset)が行う
        names = [d["file"] for d in dropped]
        (AUDIO_DIR.parent / "pruned.txt").write_text("\n".join(names), encoding="utf-8")
        log(f"保存上限を超えた{len(names)}回を削除対象にしました: {', '.join(names)}")
    feed = bf.build(cfg)
    log(f"RSSを更新: {feed}")
    log(f"出力先: {mp3.parent}")
    _log_fh.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
