import base64
import difflib
import hashlib
import os
import re
import shutil
import tempfile
from pathlib import Path

import numpy as np
import requests
import soundfile as sf


def normalize_markers(text):
    source = str(text or "")
    source = re.sub(r"([「『（(【\[])[＠@]{2}\s*", r"\1", source)
    return source.replace("@@", "").replace("＠＠", "")


def remove_ignored(text, values):
    source = str(text or "")
    for value in sorted({str(v) for v in (values or []) if str(v)}, key=len, reverse=True):
        source = source.replace(value, "")
    return source


def _key(value):
    return re.sub(r"[\s\u3000_\-・･.。]+", "", str(value or "")).casefold()


def _natural_cut(text, target, backtrack):
    limit = min(len(text), max(1, int(target)))
    start = max(1, limit - max(0, int(backtrack)))
    for reason, chars in (("sentence", "。"), ("exclamation", "！？!?"), ("comma", "、，,")):
        for index in range(limit - 1, start - 1, -1):
            if text[index] in chars:
                end = index + 1
                while end < len(text) and text[end] in "。！？!?、，,」』】〕）)]":
                    end += 1
                return end, reason
    return limit, "hard"


def _find_voice(name, settings, root):
    voice_dir = root / "data" / "voice_refs"
    default = str(settings.get("default_voice") or "")
    default_path = (root / default).resolve() if default and not Path(default).is_absolute() else Path(default)
    default_result = (str(default_path) if default and default_path.is_file() else "", "default", 0.0)
    aliases = settings.get("speaker_aliases") or {}
    explicit = str(aliases.get(name) or "") if isinstance(aliases, dict) else ""
    if explicit:
        explicit_path = Path(explicit)
        if not explicit_path.is_absolute():
            explicit_path = root / explicit_path
        if explicit_path.is_file():
            return str(explicit_path.resolve()), "explicit", 1.0
    files = sorted(voice_dir.glob("*.wav")) if voice_dir.is_dir() else []
    wanted = _key(name)
    if not wanted:
        return default_result
    scored = []
    for path in files:
        stem = _key(path.stem)
        if stem == wanted:
            score, kind = 1.0, "exact"
        elif stem.startswith(wanted) or stem.endswith(wanted) or wanted.startswith(stem) or wanted.endswith(stem):
            score, kind = 0.92, "prefix_suffix"
        elif wanted in stem or stem in wanted:
            score, kind = 0.82, "partial"
        else:
            score, kind = difflib.SequenceMatcher(None, wanted, stem).ratio(), "fuzzy"
        scored.append((score, kind, path))
    scored.sort(key=lambda item: (-item[0], len(item[2].stem)))
    if scored:
        score, kind, path = scored[0]
        second = scored[1][0] if len(scored) > 1 else 0.0
        if kind != "fuzzy" or (score >= float(settings.get("speaker_fuzzy_threshold", 0.62)) and score - second >= 0.08):
            return str(path.resolve()), kind, round(score, 3)
    return default_result


def build_plan(text, settings, root):
    root = Path(root).resolve()
    pattern = str(settings.get("speaker_tag_pattern") or "【{name}】")
    markers = settings.get("speaker_markers") or {}
    marker_text = "".join(str(value) for value in markers) if isinstance(markers, dict) else ""
    normalized = str(text or "") if "@@" in pattern or "＠＠" in pattern or "@@" in marker_text or "＠＠" in marker_text else normalize_markers(text)
    source = remove_ignored(normalized, settings.get("ignored_texts", [])).strip()
    if not source:
        return []
    target = max(20, min(250, int(settings.get("target_chars", 120))))
    backtrack = max(0, min(target - 1, int(settings.get("backtrack_chars", 50))))
    if "{name}" not in pattern and "佐藤" in pattern:
        pattern = pattern.replace("佐藤", "{name}", 1)
    segments = [(source, "", "", 0.0)]
    enabled = settings.get("reader_extraction_enabled", True) and settings.get("speaker_switch_enabled", True)
    if enabled:
        occurrences = []
        pattern_spans = []
        if "{name}" in pattern:
            before, after = pattern.split("{name}", 1)
            name_expression = r"(.+?)" if after else r"([^\s　。！？!?、，,]+)"
            for match in re.finditer(re.escape(before) + name_expression + re.escape(after), source):
                name = match.group(1).strip()
                mapped = ""
                if isinstance(markers, dict):
                    mapped = str(markers.get(match.group(0)) or markers.get(name) or "")
                if mapped:
                    voice_path = Path(mapped)
                    if not voice_path.is_absolute():
                        voice_path = root / voice_path
                    voice = str(voice_path.resolve()) if voice_path.is_file() else ""
                    kind, score = "explicit", 1.0
                else:
                    voice, kind, score = _find_voice(name, settings, root)
                occurrences.append((match.start(), match.end(), name, voice, kind, score, True))
                pattern_spans.append((match.start(), match.end()))
        if isinstance(markers, dict):
            for marker, voice_value in markers.items():
                marker = str(marker)
                if not marker:
                    continue
                voice_path = Path(str(voice_value or ""))
                if voice_value and not voice_path.is_absolute():
                    voice_path = root / voice_path
                start = 0
                while True:
                    position = source.find(marker, start)
                    if position < 0:
                        break
                    marker_end = position + len(marker)
                    inside_pattern = any(position >= left and marker_end <= right for left, right in pattern_spans)
                    if not inside_pattern:
                        display_name = voice_path.stem if voice_value else marker
                        voice = str(voice_path.resolve()) if voice_path.is_file() else ""
                        occurrences.append((position, marker_end, display_name, voice, "explicit", 1.0, False))
                    start = marker_end
        occurrences.sort(key=lambda item: (item[0], -item[1], not item[6]))
        if occurrences:
            segments = []
            if occurrences[0][0] > 0:
                segments.append((source[:occurrences[0][0]], "", "default", 0.0))
            for index, (position, marker_end, display_name, voice, kind, score, _) in enumerate(occurrences):
                body_start = marker_end
                body_end = occurrences[index + 1][0] if index + 1 < len(occurrences) else len(source)
                segments.append((source[body_start:body_end], display_name, kind, score, voice))
    plan = []
    for segment in segments:
        body, speaker, kind, score = segment[:4]
        voice = segment[4] if len(segment) > 4 else _find_voice("", settings, root)[0]
        remaining = body.strip()
        while len(remaining) > target:
            cut, reason = _natural_cut(remaining, target, backtrack)
            piece = remaining[:cut].strip()
            if piece:
                plan.append(_plan_item(piece, reason, speaker, voice, kind, score, settings))
            remaining = remaining[cut:].strip()
        if remaining:
            plan.append(_plan_item(remaining, "end", speaker, voice, kind, score, settings))
    fallback_voice = _find_voice("", settings, root)[0]
    if fallback_voice:
        for item in plan:
            if not item.get("voice_ref"):
                item["voice_ref"] = fallback_voice
                item["speaker_match"] = "default"
                item["speaker_score"] = 0.0
    if plan:
        plan[-1]["silence_after_ms"] = 0
    return plan


def _plan_item(text, reason, speaker, voice, kind, score, settings):
    if reason in {"sentence", "exclamation"}:
        silence = int(settings.get("silence_sentence_ms", 150))
    elif reason == "end":
        silence = int(settings.get("silence_dialogue_ms", 220))
    else:
        silence = int(settings.get("silence_hard_ms", 100))
    return {"text": text, "length": len(text), "reason": reason, "speaker": speaker, "voice_ref": voice, "speaker_match": kind, "speaker_score": score, "silence_after_ms": silence}


def _ascii_voice_path(value):
    path = Path(value).resolve()
    if not path.is_file():
        raise RuntimeError(f"参照WAVが見つかりません: {path}")
    try:
        str(path).encode("ascii")
        return path
    except UnicodeEncodeError:
        stat = path.stat()
        key = hashlib.sha1(f"{path}|{stat.st_size}|{stat.st_mtime_ns}".encode()).hexdigest()[:16]
        cache = Path(tempfile.gettempdir()) / "irodori_voice_studio_refs"
        cache.mkdir(exist_ok=True)
        target = cache / f"voice_{key}.wav"
        if not target.exists():
            shutil.copy2(path, target)
        return target


def synthesize_chunk(text, voice_ref, settings):
    payload = {
        "model": settings.get("model", "irodori-500m-v3-q8"),
        "input": text,
        "language": "ja",
        "seed": int(settings.get("seed", 1234)),
        "num_inference_steps": int(settings.get("num_steps", 40)),
        "response_format": "wav",
        "options": {"no_ref": not bool(voice_ref), "text_chunk_size": str(settings.get("target_chars", 120)), "text_chunk_mode": "japanese"},
    }
    if voice_ref:
        payload["voice_ref"] = str(_ascii_voice_path(voice_ref))
    response = requests.post(settings["audio_cpp_url"], json=payload, timeout=600)
    if not response.ok:
        raise RuntimeError(f"audio.cpp error {response.status_code}: {response.text[:300]}")
    if len(response.content) < 44 or response.content[:4] != b"RIFF":
        raise RuntimeError("audio.cppから正常なWAVが返りませんでした。")
    return response.content


def combine_wavs(paths, gaps, fade_ms=5):
    arrays, rate, channels = [], None, None
    for index, path in enumerate(paths):
        audio, current_rate = sf.read(str(path), always_2d=True, dtype="float32")
        if rate is None:
            rate, channels = current_rate, audio.shape[1]
        elif current_rate != rate or audio.shape[1] != channels:
            raise RuntimeError("生成WAVの形式が一致しません。")
        frames = min(len(audio) // 2, int(rate * int(fade_ms or 0) / 1000))
        if frames:
            audio[:frames] *= np.linspace(0, 1, frames, dtype=np.float32)[:, None]
            audio[-frames:] *= np.linspace(1, 0, frames, dtype=np.float32)[:, None]
        arrays.append(audio)
        if index < len(paths) - 1:
            silence = int(rate * int(gaps[index] or 0) / 1000)
            if silence:
                arrays.append(np.zeros((silence, channels), dtype=np.float32))
    fd, output = tempfile.mkstemp(prefix="irodori_studio_", suffix=".wav")
    os.close(fd)
    sf.write(output, np.concatenate(arrays), rate, subtype="PCM_16")
    return Path(output)


def synthesize(text, settings, root):
    plan = build_plan(text, settings, root)
    if not plan:
        raise RuntimeError("読み上げ対象がありません。")
    temp_paths = []
    try:
        for item in plan:
            wav = synthesize_chunk(item["text"], item.get("voice_ref", ""), settings)
            fd, path = tempfile.mkstemp(prefix="irodori_chunk_", suffix=".wav")
            os.close(fd)
            Path(path).write_bytes(wav)
            temp_paths.append(Path(path))
        output = combine_wavs(temp_paths, [item["silence_after_ms"] for item in plan], settings.get("fade_ms", 5))
        return output, plan
    finally:
        for path in temp_paths:
            path.unlink(missing_ok=True)


def save_uploaded_wav(name, data_url, root):
    safe, raw = decode_uploaded_wav(name, data_url)
    folder = Path(root) / "data" / "voice_refs"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / safe
    target.write_bytes(raw)
    return target


def save_temporary_wav(name, data_url):
    _, raw = decode_uploaded_wav(name, data_url)
    fd, target = tempfile.mkstemp(prefix="irodori_selected_ref_", suffix=".wav")
    os.close(fd)
    path = Path(target)
    path.write_bytes(raw)
    return path


def decode_uploaded_wav(name, data_url):
    safe = re.sub(r"[^0-9A-Za-zぁ-んァ-ヶ一-龠々ー._-]+", "_", Path(name).name)
    if not safe.lower().endswith(".wav"):
        raise ValueError("WAVファイルを選択してください。")
    encoded = str(data_url).split(",", 1)[-1]
    raw = base64.b64decode(encoded, validate=True)
    if len(raw) < 44 or raw[:4] != b"RIFF" or raw[8:12] != b"WAVE" or len(raw) > 50 * 1024 * 1024:
        raise ValueError("正常な50MB以下のWAVではありません。")
    return safe, raw
