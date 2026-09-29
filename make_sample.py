"""本日分の台本の冒頭を、3つのクラウドTTSで並行合成して聴き比べ用MP3を作る。

例:
    uv run make_sample.py                      # 本日分を自動検出して5分ぶん・3エンジン
    uv run make_sample.py --minutes 3
    uv run make_sample.py --engines edge,gemini
    uv run make_sample.py --script <path> --rename スワベ=ケン
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import tts_engines as te

ROOT = Path(__file__).resolve().parent
OUT_DIR = ROOT / "output" / "samples"
LOG_DIR = ROOT / "logs"

# 既存パイプラインの実測(台本14,704字 -> 31.1分)から
CHARS_PER_MIN = 470

SCRIPT_SEARCH = [
    Path(r"G:\マイドライブ\ClaudeCode\PodCast\ai-weekly-podcast\output\text"),
    Path(r"G:\マイドライブ\ClaudeCode\PodCast\finance-daily-podcast\output\text"),
]

_print_lock = threading.Lock()
_log_fh = None


def log(msg: str, tag: str = "main") -> None:
    line = f"[{dt.datetime.now():%H:%M:%S}] [{tag}] {msg}"
    with _print_lock:
        print(line, flush=True)
        if _log_fh:
            _log_fh.write(line + "\n")
            _log_fh.flush()


def find_latest_script() -> Path:
    cands: list[Path] = []
    for base in SCRIPT_SEARCH:
        if base.is_dir():
            cands += list(base.glob("*/*_podcast_script.txt"))
    if not cands:
        raise SystemExit("台本が見つかりません。--script で明示してください")
    return max(cands, key=lambda p: p.stat().st_mtime)


def run_engine(key: str, lines: list[te.Line], cfg: dict, date_tag: str) -> dict:
    label, fn = te.ENGINES[key]
    started = time.time()
    try:
        pcm = fn(lines, cfg, lambda m: log(m, key))
        pcm = te.strip_wav(pcm)
        rate = cfg["audio"]["sample_rate"]
        out = OUT_DIR / f"{date_tag}_sample_{key}.mp3"
        te.pcm_to_mp3(pcm, out, rate, cfg["audio"]["mp3_bitrate"],
                      cfg["audio"].get("normalize", False))
        dur = te.pcm_duration_sec(pcm, rate)
        log(f"OK {out.name}  {dur/60:.1f}分  {out.stat().st_size/1024:.0f}KB  "
            f"({time.time()-started:.0f}秒)", key)
        return {"engine": label, "ok": True, "file": out.name, "sec": dur,
                "elapsed": time.time() - started}
    except Exception as e:  # エンジン1つの失敗で他を止めない
        log(f"FAILED: {e}", key)
        return {"engine": label, "ok": False, "error": str(e),
                "elapsed": time.time() - started}


def main() -> int:
    global _log_fh

    ap = argparse.ArgumentParser()
    ap.add_argument("--script", type=Path, default=None)
    ap.add_argument("--minutes", type=float, default=5.0)
    ap.add_argument("--engines", default="edge,gemini")
    ap.add_argument("--rename", action="append", default=[],
                    help="台本中の話者名の置換。例 --rename スワベ=ケン")
    args = ap.parse_args()

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    _log_fh = open(LOG_DIR / "make_sample.log", "a", encoding="utf-8")

    cfg = te.load_config()
    script = args.script or find_latest_script()

    # 話者名を固定キャストへ寄せる(既存台本は日替わりキャストで書かれているため)
    rename = dict(cfg.get("cast", {}).get("rename", {}))
    rename.update(dict(p.split("=", 1) for p in args.rename))
    lines_all = te.parse_script(script, rename)
    target = int(args.minutes * CHARS_PER_MIN)
    lines = te.take_head(lines_all, target)
    chars = sum(len(l.text) for l in lines)

    engines = [e.strip() for e in args.engines.split(",") if e.strip()]
    unknown = [e for e in engines if e not in te.ENGINES]
    if unknown:
        raise SystemExit(f"未知のエンジン: {unknown}")

    date_tag = script.parent.name.split("_")[0]
    log(f"台本: {script}")
    log(f"抽出: {len(lines)}/{len(lines_all)}行  {chars}字  "
        f"(目標{args.minutes}分 ≒ {target}字)")
    log(f"キャスト: 進行役={cfg['cast']['host_name']} / 解説役={cfg['cast']['guest_name']}")
    log(f"エンジン: {', '.join(engines)} を並行実行")

    with ThreadPoolExecutor(max_workers=len(engines)) as pool:
        results = list(pool.map(lambda k: run_engine(k, lines, cfg, date_tag), engines))

    log("=" * 56)
    for r in results:
        if r["ok"]:
            log(f"  {r['engine']:<20} {r['sec']/60:>5.1f}分  {r['file']}")
        else:
            log(f"  {r['engine']:<20} 失敗: {r['error'].splitlines()[0]}")
    log(f"出力先: {OUT_DIR}")

    _log_fh.close()
    return 0 if any(r["ok"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
