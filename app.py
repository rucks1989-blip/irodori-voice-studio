import json
import mimetypes
import os
import shutil
import subprocess
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import requests

from studio_core import build_plan, save_temporary_wav, save_uploaded_wav, synthesize
from chat_service import chat as call_llm, llm_health, load_character, run_command
from llm_manager import copy_ollama_model, discover, register_gguf_with_ollama, select_ollama_model
from long_jobs import LongJobManager


ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "web"
EXAMPLE = ROOT / "settings.example.json"
LOCAL = ROOT / "settings.local.json"
OUTPUTS = ROOT / "outputs"
HISTORY = ROOT / "data" / "history.json"
LOCK = threading.Lock()
CHAT_LOCK = threading.Lock()
CHAT_HISTORY = {}
JOB_MANAGER = None


def read_json(path, fallback):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        return value if isinstance(value, dict) else fallback
    except Exception:
        return fallback


def settings():
    base = read_json(EXAMPLE, {})
    base.update(read_json(LOCAL, {}))
    base["ui_port"] = 6670
    base["api_port"] = 6666
    return base


def public_settings(value):
    result = dict(value)
    for key in ("backend_start_command", "llm_start_command", "llm_stop_command", "llm_api_key"):
        result.pop(key, None)
    return result


def character_files():
    files = list((ROOT / "characters").glob("*.json"))
    custom = ROOT / "data" / "characters"
    if custom.is_dir():
        files.extend(custom.glob("*.json"))
    return sorted(files, key=lambda item: item.name.casefold())


def character_info(path):
    value = load_character(path)
    return {"file": path.name, "name": value["name"], "description": str(value.get("description") or ""), "sample": path.parent.name == "characters"}


def find_character(filename):
    wanted = Path(str(filename or "")).name
    for path in character_files():
        if path.name == wanted:
            return path
    raise ValueError("選択したキャラクターJSONが見つかりません。")


def registered_voice(value):
    """Resolve a UI/character voice reference without allowing arbitrary file access."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    voice_dir = (ROOT / "data" / "voice_refs").resolve()
    candidate = Path(raw)
    if candidate.is_absolute():
        resolved = candidate.resolve()
    else:
        resolved = (ROOT / candidate).resolve()
        if not resolved.is_file():
            resolved = (voice_dir / candidate.name).resolve()
    if resolved.parent != voice_dir or not resolved.is_file() or resolved.suffix.lower() != ".wav":
        raise ValueError("選択した会話用WAVが登録済み音声に見つかりません。")
    return str(resolved)


def write_local(value):
    current = read_json(LOCAL, {})
    allowed = set(read_json(EXAMPLE, {})) - {"ui_port", "api_port", "backend_start_command"}
    for key in allowed:
        if key in value:
            current[key] = value[key]
    LOCAL.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")


def backend_status(config):
    try:
        response = requests.get(config["audio_cpp_health_url"], timeout=2)
        return response.ok, "接続済み" if response.ok else f"HTTP {response.status_code}"
    except Exception as exc:
        return False, str(exc)


def history():
    value = read_json(HISTORY, {"items": []})
    return value.get("items", [])[:30]


def add_history(item):
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    items = [item] + history()
    HISTORY.write_text(json.dumps({"items": items[:30]}, ensure_ascii=False, indent=2), encoding="utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "IrodoriVoiceStudio/0.1"

    def log_message(self, fmt, *args):
        print(f"[studio] {self.address_string()} {fmt % args}", flush=True)

    def send_json(self, status, value):
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def request_json(self, maximum=70 * 1024 * 1024):
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0 or length > maximum:
            raise ValueError("送信内容が空、または大きすぎます。")
        value = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("JSONオブジェクトが必要です。")
        return value

    def static_file(self, path):
        relative = "index.html" if path == "/" else unquote(path.lstrip("/"))
        target = (STATIC / relative).resolve()
        if STATIC.resolve() not in target.parents and target != STATIC.resolve():
            self.send_error(403)
            return
        if not target.is_file():
            self.send_error(404)
            return
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(target.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed_url = urlparse(self.path)
        path = parsed_url.path
        try:
            if path == "/api/health":
                config = settings()
                ready, detail = backend_status(config)
                self.send_json(200, {"studio": "ok", "backend_ready": ready, "backend_detail": detail, "model": config.get("model"), "llm_ready": llm_health(config), "llm_model": config.get("llm_model")})
            elif path == "/api/settings":
                refs = sorted(p.name for p in (ROOT / "data" / "voice_refs").glob("*.wav")) if (ROOT / "data" / "voice_refs").is_dir() else []
                self.send_json(200, {"settings": public_settings(settings()), "voice_refs": refs})
            elif path == "/api/history":
                self.send_json(200, {"items": history()})
            elif path == "/api/characters":
                self.send_json(200, {"characters": [character_info(item) for item in character_files()]})
            elif path == "/api/llm/discover":
                deep = (parse_qs(parsed_url.query).get("deep") or ["0"])[0] == "1"
                self.send_json(200, discover(ROOT, deep=deep))
            elif path == "/api/jobs/active":
                self.send_json(200, {"job": JOB_MANAGER.active() if JOB_MANAGER else None})
            elif path.startswith("/api/jobs/"):
                job_id = Path(unquote(path)).name
                self.send_json(200, {"job": JOB_MANAGER.get(job_id)})
            elif path.startswith("/outputs/"):
                target = (OUTPUTS / Path(unquote(path)).name).resolve()
                if not target.is_file() or OUTPUTS.resolve() not in target.parents:
                    self.send_error(404)
                else:
                    body = target.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "audio/wav")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Content-Disposition", f'inline; filename="{target.name}"')
                    self.end_headers()
                    self.wfile.write(body)
            else:
                self.static_file(path)
        except Exception as exc:
            self.send_json(500, {"error": str(exc)})

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            payload = self.request_json()
            if path == "/api/settings":
                write_local(payload)
                self.send_json(200, {"status": "saved"})
                return
            if path == "/api/upload-voice":
                target = save_uploaded_wav(payload.get("name", ""), payload.get("data", ""), ROOT)
                self.send_json(200, {"status": "saved", "name": target.name, "relative_path": str(target.relative_to(ROOT)).replace("\\", "/")})
                return
            if path == "/api/upload-character":
                name = Path(str(payload.get("name") or "")).name
                if not name.lower().endswith(".json"):
                    raise ValueError("JSONファイルを選択してください。")
                raw = str(payload.get("text") or "")
                if not raw or len(raw.encode("utf-8")) > 1024 * 1024:
                    raise ValueError("キャラクターJSONは1MB以下にしてください。")
                value = json.loads(raw)
                if not isinstance(value, dict):
                    raise ValueError("キャラクターJSONはオブジェクト形式にしてください。")
                folder = ROOT / "data" / "characters"
                folder.mkdir(parents=True, exist_ok=True)
                target = folder / name
                target.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
                info = character_info(target)
                self.send_json(200, {"status": "saved", "character": info})
                return
            if path == "/api/llm/copy-ollama":
                info = copy_ollama_model(ROOT, str(payload.get("model") or ""))
                self.send_json(200, {"status": "copied", "gguf": info})
                return
            if path == "/api/llm/open-folder":
                folder = ROOT / "llm"
                folder.mkdir(parents=True, exist_ok=True)
                os.startfile(folder)
                self.send_json(200, {"status": "opened", "folder": str(folder)})
                return
            if path == "/api/llm/select":
                source = str(payload.get("source") or "")
                if source == "ollama":
                    config = select_ollama_model(str(payload.get("model") or ""))
                elif source == "gguf":
                    config = register_gguf_with_ollama(ROOT, str(payload.get("path") or ""))
                else:
                    raise ValueError("LLMの種類を選択してください。")
                write_local(config)
                self.send_json(200, {"status": "selected", "settings": public_settings(settings())})
                return
            if path == "/api/llm/test":
                config = settings()
                reply = call_llm(
                    {"name": "リリー", "system_prompt": "あなたはツンデレの女の子リリーです。日本語で一文だけ返答してください。"},
                    str(payload.get("message") or "こんにちは。自己紹介して。"), [], config,
                )
                self.send_json(200, {"status": "ok", "reply": reply, "model": config.get("llm_model")})
                return
            if path.startswith("/api/jobs/"):
                parts = [part for part in path.split("/") if part]
                if len(parts) != 4 or parts[1] != "jobs":
                    raise ValueError("長文生成ジョブの操作が正しくありません。")
                job_id, action = parts[2], parts[3]
                if action == "cancel":
                    self.send_json(200, {"job": JOB_MANAGER.cancel(job_id)})
                elif action == "resume":
                    self.send_json(200, {"job": JOB_MANAGER.resume(job_id)})
                else:
                    raise ValueError("長文生成ジョブの操作が正しくありません。")
                return
            if path == "/api/chat/reset":
                character_file = Path(str(payload.get("character") or "")).name
                if not CHAT_LOCK.acquire(blocking=False):
                    self.send_json(409, {"error": "会話生成中は履歴を消去できません。"})
                    return
                try:
                    CHAT_HISTORY.pop(character_file, None)
                finally:
                    CHAT_LOCK.release()
                self.send_json(200, {"status": "reset"})
                return
            if path == "/api/chat":
                message = str(payload.get("message") or "").strip()
                if not message or len(message) > 4000:
                    raise ValueError("メッセージは1～4000文字で入力してください。")
                character_path = find_character(payload.get("character"))
                character = load_character(character_path)
                if not CHAT_LOCK.acquire(blocking=False):
                    self.send_json(409, {"error": "別の会話を処理中です。"})
                    return
                if not LOCK.acquire(blocking=False):
                    CHAT_LOCK.release()
                    self.send_json(409, {"error": "別の音声を生成中です。"})
                    return
                config = settings()
                try:
                    conversation = CHAT_HISTORY.setdefault(character_path.name, [])
                    voice = registered_voice(payload.get("voice") or character.get("voice_ref") or "")
                    try:
                        reply = call_llm(character, message, conversation, config)
                    except Exception:
                        try:
                            run_command(config.get("llm_stop_command"))
                        finally:
                            raise
                    run_command(config.get("llm_stop_command"))
                    if voice:
                        config["default_voice"] = voice
                    # Character chat is single-speaker output.  Do not let words in an
                    # LLM reply accidentally trigger story-mode speaker mappings.
                    config["reader_extraction_enabled"] = False
                    config["speaker_switch_enabled"] = False
                    output, plan = synthesize(reply, config, ROOT)
                    OUTPUTS.mkdir(parents=True, exist_ok=True)
                    filename = f"chat_{datetime.now():%Y%m%d_%H%M%S_%f}.wav"
                    target = OUTPUTS / filename
                    shutil.move(str(output), target)
                    conversation.extend([{"role": "user", "content": message}, {"role": "assistant", "content": reply}])
                    maximum = max(2, int(config.get("llm_max_history", 12)))
                    del conversation[:-maximum]
                finally:
                    LOCK.release()
                    CHAT_LOCK.release()
                used_voice = Path(plan[0].get("voice_ref") or "").name if plan else ""
                self.send_json(200, {"reply": reply, "character": character["name"], "voice": used_voice, "filename": filename, "url": f"/outputs/{filename}", "chunks": len(plan)})
                return
            text = str(payload.get("text") or "")
            if not text.strip() or len(text) > 20000:
                raise ValueError("文章は1～20000文字で入力してください。")
            config = settings()
            if path == "/api/plan":
                self.send_json(200, {"chunks": build_plan(text, config, ROOT)})
                return
            if path == "/api/generate":
                started = time.perf_counter()
                temporary_voice = None
                lock_acquired = False
                try:
                    voice_name = str(payload.get("voice_name") or "")
                    voice_data = str(payload.get("voice_data") or "")
                    if voice_name or voice_data:
                        if not voice_name or not voice_data:
                            raise ValueError("参照WAVをもう一度選択してください。")
                        if bool(payload.get("register_voice")):
                            selected_voice = save_uploaded_wav(voice_name, voice_data, ROOT)
                        else:
                            temporary_voice = save_temporary_wav(voice_name, voice_data)
                            selected_voice = temporary_voice
                        config["default_voice"] = str(selected_voice)
                    plan = build_plan(text, config, ROOT)
                    if not plan:
                        raise RuntimeError("読み上げ対象がありません。")
                    if len(text) >= 2000 or len(plan) >= 20:
                        if LOCK.locked():
                            self.send_json(409, {"error": "別の音声を生成中です。"})
                            return
                        job = JOB_MANAGER.create(text, config, plan, str(temporary_voice or ""), voice_name if temporary_voice else "")
                        self.send_json(202, {"long_job": True, "job": job})
                        return
                    if not LOCK.acquire(blocking=False):
                        self.send_json(409, {"error": "別の音声を生成中です。"})
                        return
                    lock_acquired = True
                    output, plan = synthesize(text, config, ROOT)
                    OUTPUTS.mkdir(parents=True, exist_ok=True)
                    filename = f"irodori_{datetime.now():%Y%m%d_%H%M%S_%f}.wav"
                    target = OUTPUTS / filename
                    shutil.move(str(output), target)
                finally:
                    if temporary_voice:
                        temporary_voice.unlink(missing_ok=True)
                    if lock_acquired:
                        LOCK.release()
                elapsed = time.perf_counter() - started
                used_voices = sorted({Path(item.get("voice_ref") or "").name for item in plan if item.get("voice_ref")})
                if temporary_voice and temporary_voice.name in used_voices:
                    used_voices = [voice_name if name == temporary_voice.name else name for name in used_voices]
                info = {"filename": filename, "url": f"/outputs/{filename}", "created_at": datetime.now().isoformat(timespec="seconds"), "seconds": round(elapsed, 3), "chunks": len(plan), "voices": used_voices, "text": text[:160]}
                add_history(info)
                self.send_json(200, info)
                return
            self.send_json(404, {"error": "not_found"})
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_json(400, {"error": str(exc)})
        except Exception as exc:
            self.send_json(500, {"error": str(exc)})


def maybe_start_backend(config):
    ready, _ = backend_status(config)
    command = str(config.get("backend_start_command") or "").strip()
    if not ready and command:
        subprocess.Popen(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", command], cwd=ROOT)


def main():
    global JOB_MANAGER
    config = settings()
    maybe_start_backend(config)
    JOB_MANAGER = LongJobManager(ROOT, LOCK, add_history, maybe_start_backend)
    api_server = ThreadingHTTPServer(("127.0.0.1", 6666), Handler)
    ui_server = ThreadingHTTPServer(("127.0.0.1", 6670), Handler)
    threading.Thread(target=api_server.serve_forever, name="local-api-6666", daemon=True).start()
    print("[irodori voice studio] UI http://127.0.0.1:6670 / API http://127.0.0.1:6666", flush=True)
    try:
        ui_server.serve_forever()
    finally:
        ui_server.server_close()
        api_server.shutdown()
        api_server.server_close()


if __name__ == "__main__":
    main()
