"""Gemini API でダイジェストと台本を生成する。

モデルは config.toml の text_models を上から順に試す。無料枠は混雑で 503 を返したり、
空応答を返すことがあるため、どちらもリトライ対象にしている。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent
API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

_HARM = ("HARM_CATEGORY_HARASSMENT", "HARM_CATEGORY_HATE_SPEECH",
         "HARM_CATEGORY_SEXUALLY_EXPLICIT", "HARM_CATEGORY_DANGEROUS_CONTENT")

# 台本の目標字数 = 分数 × この係数。
# 実測(2026-09-17): 480 を指定したら目標10分に対し12分ぶん書いたので 410 に下げた。
# モデルの癖が変わったら、ログの「見込み」と実際の尺を見てここを調整する。
CHARS_PER_MIN = 410


class GenerationError(RuntimeError):
    pass


def _load_prompt(name: str, values: dict[str, str]) -> str:
    text = (ROOT / "prompts" / name).read_text(encoding="utf-8-sig")
    for k, v in values.items():
        text = text.replace("{{" + k + "}}", str(v))
    return text


def generate_text(prompt: str, cfg: dict, log, tries_per_model: int = 3) -> str:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise GenerationError("環境変数 GEMINI_API_KEY が未設定です")

    payload_base = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "safetySettings": [{"category": c, "threshold": "BLOCK_NONE"} for c in _HARM],
    }

    last = ""
    for model in cfg["gemini"]["text_models"]:
        for attempt in range(1, tries_per_model + 1):
            # 503(混雑)は待つほど収まりやすいので、試行ごとに待ち時間を延ばす
            backoff = 5 * attempt
            try:
                resp = httpx.post(API.format(model=model), params={"key": api_key},
                                  json=payload_base, timeout=300.0)
            except Exception as e:
                last = f"{model}: {type(e).__name__}"
                log(f"  {model} 通信失敗 ({attempt}/{tries_per_model})")
                time.sleep(backoff)
                continue

            if resp.status_code != 200:
                last = f"{model}: HTTP {resp.status_code} {resp.text[:160]}"
                log(f"  {model} HTTP {resp.status_code} ({attempt}/{tries_per_model})")
                time.sleep(backoff)
                continue

            text = ""
            for part in (resp.json().get("candidates") or [{}])[0] \
                    .get("content", {}).get("parts", []):
                text += part.get("text", "")

            # 空応答は無料枠で実際に起きる。リトライ対象にする
            if not text.strip():
                last = f"{model}: 空応答"
                log(f"  {model} 空応答 ({attempt}/{tries_per_model})")
                time.sleep(backoff)
                continue

            log(f"  {model} で生成 ({len(text)}字)")
            return text.strip()

    raise GenerationError(f"全モデルで生成に失敗しました (最後のエラー: {last})")


def make_digest(items_text: str, cfg: dict, recent_topics: str, log) -> str:
    p = cfg["program"]
    prompt = _load_prompt("digest.txt", {
        "PROGRAM_TITLE": p["title"],
        "THEME": p["theme"],
        "LISTENER": p["listener"],
        "DAYS": p["days"],
        "ITEMS": items_text,
        "RECENT_TOPICS": recent_topics,
    })
    return generate_text(prompt, cfg, log)


def make_script(digest: str, cfg: dict, date_str: str, log) -> str:
    p = cfg["program"]
    prompt = _load_prompt("script.txt", {
        "PROGRAM_TITLE": p["title"],
        "THEME": p["theme"],
        "LISTENER": p["listener"],
        "DATE": date_str,
        "DIGEST": digest,
        "HOST_NAME": cfg["cast"]["host_name"],
        "GUEST_NAME": cfg["cast"]["guest_name"],
        "TARGET_CHARS": int(p["minutes"] * CHARS_PER_MIN),
    })
    return generate_text(prompt, cfg, log)
