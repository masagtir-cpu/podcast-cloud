"""3つのクラウドTTSエンジンを同じ入出力で扱うための薄いラッパ。

どのエンジンも「(speaker, text) の列 -> PCM s16le/mono/24kHz のbytes」を返す。
出力形式を揃えてあるので、MP3化は共通処理で行い、A/B比較が音量・サンプルレートに
左右されないようにしている。
"""

from __future__ import annotations

import array
import asyncio
import base64
import io
import math
import os
import re
import shutil
import subprocess
import tomllib
import wave
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# Gemini TTS はプロンプトで話し方を指示できる。既存 run_weekly.ps1 の $TtsStyle を踏襲。
GEMINI_STYLE = (
    "TTS the following Japanese podcast conversation between HOST and GUEST. "
    "HOST is a woman and GUEST is a man. Both speak clearly and naturally at a normal, "
    "comfortable pace. Both speakers stay clearly audible throughout. Natural intonation "
    "and pitch variation that follows the flow of the conversation is welcome, but the "
    "speaking TEMPO must stay steady from the first line to the last: never slow down the "
    "pace, regardless of the topic. Also avoid sudden bursts of an excited register: do not "
    "jump to a noticeably higher pitch with faster speech; keep the energy level even."
)

_HARM_CATEGORIES = (
    "HARM_CATEGORY_HARASSMENT",
    "HARM_CATEGORY_HATE_SPEECH",
    "HARM_CATEGORY_SEXUALLY_EXPLICIT",
    "HARM_CATEGORY_DANGEROUS_CONTENT",
)

LINE_RE = re.compile(r"^(HOST|GUEST)\s*[:：]\s*(.+)$")

_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_MD_MARK = re.compile(r"[*`#]+")


def _clean(text: str) -> str:
    """モデルがまれに混ぜる装飾記号を落とす。

    `**重要**` のような記法が残っていると、TTSが記号をそのまま読み上げたり
    不自然な間を空けたりする。
    """
    text = _MD_LINK.sub(r"\1", text)
    text = _MD_MARK.sub("", text)
    return re.sub(r"\s{2,}", " ", text).strip()


@dataclass
class Line:
    speaker: str  # "HOST" | "GUEST"
    text: str


class EngineError(RuntimeError):
    pass


# ---------- 設定・台本 ----------


def load_config(path: Path | None = None) -> dict:
    p = path or (ROOT / "config.toml")
    # Windowsのエディタで保存するとBOMが付くことがあるので utf-8-sig で読む
    try:
        return tomllib.loads(p.read_text(encoding="utf-8-sig"))
    except tomllib.TOMLDecodeError as e:
        raise SystemExit(
            f"config.toml の書き方に誤りがあります: {e}\n"
            '  よくある原因: 引用符 " の閉じ忘れ、= の書き忘れ、全角スペースの混入'
        ) from e


def parse_script(path: Path, rename: dict[str, str] | None = None) -> list[Line]:
    lines: list[Line] = []
    for raw in Path(path).read_text(encoding="utf-8-sig").splitlines():
        m = LINE_RE.match(raw.strip())
        if not m:
            continue
        text = m.group(2).strip()
        for old, new in (rename or {}).items():
            text = text.replace(old, new)
        text = _clean(text)
        if not text:
            continue
        lines.append(Line(m.group(1), text))
    if not lines:
        raise EngineError(f"HOST:/GUEST: 行が見つかりません: {path}")
    return lines


def take_head(lines: list[Line], target_chars: int) -> list[Line]:
    """先頭から target_chars 相当まで取り出す(発話の途中では切らない)。"""
    out, total = [], 0
    for ln in lines:
        out.append(ln)
        total += len(ln.text)
        if total >= target_chars:
            break
    return out


# ---------- 音声ユーティリティ ----------


def find_ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    base = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/WinGet/Packages"
    if base.is_dir():
        for cand in base.glob("Gyan.FFmpeg*/**/bin/ffmpeg.exe"):
            return str(cand)
    raise EngineError("ffmpeg が見つかりません")


def silence_pcm(ms: int, rate: int) -> bytes:
    return b"\x00\x00" * int(rate * ms / 1000)


def strip_wav(data: bytes) -> bytes:
    """WAVコンテナからPCM本体を取り出す(既に生PCMならそのまま返す)。"""
    if data[:4] != b"RIFF":
        return data
    with wave.open(io.BytesIO(data), "rb") as w:
        return w.readframes(w.getnframes())


def decode_to_pcm(data: bytes, rate: int) -> bytes:
    """任意の音声バイト列(MP3等)を PCM s16le/mono/指定レート に変換する。"""
    proc = subprocess.run(
        [find_ffmpeg(), "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
         "-f", "s16le", "-acodec", "pcm_s16le", "-ar", str(rate), "-ac", "1", "pipe:1"],
        input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if proc.returncode != 0:
        raise EngineError(f"ffmpeg デコード失敗: {proc.stderr.decode('utf-8', 'replace')[:300]}")
    return proc.stdout


def pcm_to_mp3(pcm: bytes, out_path: Path, rate: int, bitrate: str,
               normalize: bool = False) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [find_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
           "-f", "s16le", "-ar", str(rate), "-ac", "1", "-i", "pipe:0"]
    if normalize:
        # チャンク間の音量差をならす。根本対策はチャンク分割のほうで、これは保険
        cmd += ["-af", "dynaudnorm=f=300:g=15"]
    cmd += ["-codec:a", "libmp3lame", "-b:a", bitrate, str(out_path)]
    proc = subprocess.run(cmd, input=pcm, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise EngineError(f"ffmpeg エンコード失敗: {proc.stderr.decode('utf-8', 'replace')[:300]}")


def pcm_duration_sec(pcm: bytes, rate: int) -> float:
    return len(pcm) / 2 / rate


def pcm_rms(pcm: bytes, stride: int = 7) -> float:
    """平均振幅。チャンクが小声で返ってきていないかの判定に使う。"""
    a = array.array("h")
    a.frombytes(pcm[: len(pcm) // 2 * 2])
    s = a[::stride]
    if not s:
        return 0.0
    return math.sqrt(sum(x * x for x in s) / len(s))


# ---------- エンジン1: Gemini TTS ----------


def _chunk_lines(lines: list[Line], max_chars: int) -> list[list[Line]]:
    """話者の切れ目を保ったままチャンクに分ける。"""
    chunks: list[list[Line]] = []
    cur: list[Line] = []
    n = 0
    for ln in lines:
        size = len(ln.text) + len(ln.speaker) + 2
        if cur and n + size > max_chars:
            chunks.append(cur)
            cur, n = [], 0
        cur.append(ln)
        n += size
    if cur:
        chunks.append(cur)
    return chunks


def _gemini_once(block: str, cfg: dict, api_key: str) -> bytes:
    import httpx

    model = cfg["gemini"]["model"]
    voices = cfg["voices"]["gemini"]
    payload = {
        "contents": [{"role": "user", "parts": [{"text": GEMINI_STYLE + "\n\n" + block}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {
                "multiSpeakerVoiceConfig": {
                    "speakerVoiceConfigs": [
                        {"speaker": "HOST",
                         "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voices["host"]}}},
                        {"speaker": "GUEST",
                         "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voices["guest"]}}},
                    ]
                }
            },
        },
        # ニュース台本は攻撃・インシデントを正当に扱うため、誤ブロックを避ける
        "safetySettings": [{"category": c, "threshold": "BLOCK_NONE"} for c in _HARM_CATEGORIES],
    }
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    resp = httpx.post(url, params={"key": api_key}, json=payload, timeout=420.0)
    if resp.status_code != 200:
        raise EngineError(f"HTTP {resp.status_code}: {resp.text[:300]}")
    try:
        part = resp.json()["candidates"][0]["content"]["parts"][0]
        return base64.b64decode(part["inlineData"]["data"])  # PCM s16le 24kHz mono
    except (KeyError, IndexError) as e:
        raise EngineError(f"音声データが応答に含まれません ({e}): {resp.text[:200]}") from e


def synth_gemini(lines: list[Line], cfg: dict, log) -> bytes:
    """Gemini TTS で合成する。

    1リクエストが長いほど声が単調に小さくなっていく。実測(2026-09-17)では
    4,954字を1回で投げたところ、30秒ごとの平均振幅が 3826 -> 84 まで落ち、
    後半がほとんど聞こえなくなった。そのため必ずチャンクに刻んで投げる。
    それでも小声で返ることがあるので、直前までの水準と比べて明らかに低ければ
    無料枠の範囲内で作り直す。
    """
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise EngineError("環境変数 GEMINI_API_KEY が未設定です")

    gcfg = cfg["gemini"]
    max_chars = gcfg.get("chunk_chars", 1500)
    budget = gcfg.get("regen_budget", 2)
    floor = gcfg.get("rms_floor_ratio", 0.55)

    chunks = _chunk_lines(lines, max_chars)
    log(f"{len(chunks)}チャンクに分割(最大{max_chars}字)")

    out: list[bytes] = []
    ref: float | None = None
    for i, ch in enumerate(chunks, 1):
        block = "\n".join(f"{ln.speaker}: {ln.text}" for ln in ch)
        best, best_rms = b"", -1.0
        while True:
            pcm = _gemini_once(block, cfg, api_key)
            rms = pcm_rms(pcm)
            if rms > best_rms:
                best, best_rms = pcm, rms
            if ref is None or rms >= ref * floor or budget <= 0:
                break
            budget -= 1
            log(f"  チャンク{i} 音量低下 (rms={rms:.0f} < {ref * floor:.0f}) 再生成 "
                f"(残り{budget}回)")
        if ref is None:
            ref = best_rms
        log(f"  チャンク{i}/{len(chunks)} rms={best_rms:.0f} "
            f"({pcm_duration_sec(best, cfg['audio']['sample_rate']) / 60:.1f}分)")
        out.append(best)

    return b"".join(out)


# ---------- エンジン2: Google Cloud TTS ----------


def synth_gcloud(lines: list[Line], cfg: dict, log) -> bytes:
    from google.cloud import texttospeech as tts
    from google.oauth2 import service_account

    cred_path = ROOT / cfg["gcloud"]["credentials"]
    if not cred_path.is_file():
        raise EngineError(
            f"サービスアカウントJSONがありません: {cred_path}\n"
            "        Cloud TTS は APIキー非対応のため、GCPでサービスアカウントの作成が必要です"
        )

    rate = cfg["audio"]["sample_rate"]
    gap = silence_pcm(cfg["audio"]["gap_ms"], rate)
    voices = cfg["voices"]["gcloud"]

    creds = service_account.Credentials.from_service_account_file(str(cred_path))
    client = tts.TextToSpeechClient(credentials=creds)

    parts: list[bytes] = []
    for i, ln in enumerate(lines, 1):
        resp = client.synthesize_speech(
            input=tts.SynthesisInput(text=ln.text),
            voice=tts.VoiceSelectionParams(
                language_code="ja-JP", name=voices[ln.speaker.lower()]
            ),
            audio_config=tts.AudioConfig(
                audio_encoding=tts.AudioEncoding.LINEAR16,
                sample_rate_hertz=rate,
                speaking_rate=voices.get("speaking_rate", 1.0),
            ),
        )
        parts.append(strip_wav(resp.audio_content))
        parts.append(gap)
        if i % 10 == 0:
            log(f"{i}/{len(lines)} 行")
    return b"".join(parts)


# ---------- エンジン3: edge-tts ----------


async def _edge_line(text: str, voice: str, speed: str, log, tries: int = 4) -> bytes:
    """1行を合成する。

    edge-tts は非公式エンドポイントのため、正常な入力でも突発的に
    "No audio was received" を返すことがある(2026-09-17 に実際に発生し、
    同じ台本の再実行では成功した)。無人実行で1行の失敗が全体を落とさないよう、
    行単位で待ち時間を延ばしながら再試行する。
    """
    import edge_tts

    for attempt in range(1, tries + 1):
        try:
            buf = bytearray()
            async for chunk in edge_tts.Communicate(text, voice, rate=speed).stream():
                if chunk["type"] == "audio":
                    buf += chunk["data"]
            if buf:
                return bytes(buf)  # MP3
            reason = "空の音声"
        except Exception as e:
            reason = f"{type(e).__name__}: {e}"

        if attempt == tries:
            raise EngineError(f"{tries}回試して合成できませんでした ({reason}): {text[:40]}")
        await asyncio.sleep(2 * attempt)
        log(f"再試行 {attempt}/{tries - 1} ({reason})")


async def _edge_all(lines: list[Line], voices: dict, log) -> list[bytes]:
    speed = voices.get("rate", "+0%")
    out = []
    for i, ln in enumerate(lines, 1):
        out.append(await _edge_line(ln.text, voices[ln.speaker.lower()], speed, log))
        if i % 10 == 0:
            log(f"{i}/{len(lines)} 行")
    return out


def synth_edge(lines: list[Line], cfg: dict, log) -> bytes:
    rate = cfg["audio"]["sample_rate"]
    gap = silence_pcm(cfg["audio"]["gap_ms"], rate)
    mp3_parts = asyncio.run(_edge_all(lines, cfg["voices"]["edge"], log))

    parts: list[bytes] = []
    for mp3 in mp3_parts:
        parts.append(decode_to_pcm(mp3, rate))
        parts.append(gap)
    return b"".join(parts)


ENGINES = {
    "gemini": ("Gemini TTS", synth_gemini),
    "gcloud": ("Google Cloud TTS", synth_gcloud),
    "edge": ("edge-tts", synth_edge),
}
