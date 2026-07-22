import json
import re
import shutil
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

import requests

from studio_core import build_plan, combine_wavs, synthesize_chunk


class LongJobManager:
    def __init__(self, root, generation_lock, history_callback, backend_start_callback):
        self.root = Path(root)
        self.folder = self.root / "data" / "long_jobs"
        self.folder.mkdir(parents=True, exist_ok=True)
        self.generation_lock = generation_lock
        self.history_callback = history_callback
        self.backend_start_callback = backend_start_callback
        self.events = {}
        self.threads = {}
        self.guard = threading.Lock()
        self._mark_interrupted()

    def _job_dir(self, job_id):
        if not re.fullmatch(r"[0-9]{8}-[0-9]{6}-[0-9a-f]{8}", str(job_id)):
            raise ValueError("長文生成ジョブIDが正しくありません。")
        return self.folder / job_id

    def _state_path(self, job_id):
        return self._job_dir(job_id) / "job.json"

    def _read(self, job_id):
        path = self._state_path(job_id)
        if not path.is_file():
            raise ValueError("長文生成ジョブが見つかりません。")
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("長文生成ジョブの情報が壊れています。")
        return value

    def _write(self, state):
        path = self._state_path(state["id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"job-{threading.get_ident()}.tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)

    def _public(self, state):
        result = {key: value for key, value in state.items() if key not in {"config", "plan", "text"}}
        plan = state.get("plan") or []
        current = min(int(state.get("current", 0)), len(plan))
        result["total"] = len(plan)
        result["current_text"] = plan[current].get("text", "") if current < len(plan) else ""
        result["progress"] = round(current * 100 / len(plan), 1) if plan else 0
        result["can_resume"] = state.get("status") in {"failed", "cancelled", "interrupted"}
        result["can_cancel"] = state.get("status") in {"queued", "running", "cancelling"}
        return result

    def _mark_interrupted(self):
        for path in self.folder.glob("*/job.json"):
            try:
                state = json.loads(path.read_text(encoding="utf-8"))
                if state.get("status") in {"queued", "running", "cancelling"}:
                    state["status"] = "interrupted"
                    state["message"] = "前回の生成が中断されました。同じチャンクから再開できます。"
                    state["updated_at"] = datetime.now().isoformat(timespec="seconds")
                    self._write(state)
            except Exception:
                continue

    def create(self, text, config, plan, temporary_voice="", temporary_voice_name=""):
        job_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
        job_dir = self._job_dir(job_id)
        (job_dir / "chunks").mkdir(parents=True)
        if temporary_voice:
            source = Path(temporary_voice)
            target = job_dir / "reference.wav"
            shutil.copy2(source, target)
            config = dict(config)
            config["default_voice"] = str(target)
            plan = build_plan(text, config, self.root)
        now = datetime.now().isoformat(timespec="seconds")
        state = {
            "id": job_id, "status": "queued", "message": "生成を開始します。",
            "created_at": now, "updated_at": now, "started_at": "", "finished_at": "",
            "current": 0, "attempt": 0, "error": "", "filename": "", "url": "",
            "seconds": 0, "voices": [], "temporary_voice_name": temporary_voice_name, "text_preview": text[:160],
            "text": text, "config": config, "plan": plan,
        }
        self._write(state)
        self._launch(job_id)
        return self._public(state)

    def get(self, job_id):
        return self._public(self._read(job_id))

    def active(self):
        candidates = []
        for path in self.folder.glob("*/job.json"):
            try:
                state = json.loads(path.read_text(encoding="utf-8"))
                candidates.append(state)
            except Exception:
                continue
        active_states = {"queued", "running", "cancelling"}
        candidates.sort(key=lambda value: (value.get("status") in active_states, value.get("created_at", "")), reverse=True)
        return self._public(candidates[0]) if candidates else None

    def cancel(self, job_id):
        state = self._read(job_id)
        if state.get("status") not in {"queued", "running", "cancelling"}:
            return self._public(state)
        state["status"] = "cancelling"
        state["message"] = "現在のチャンク終了後に停止します。"
        state["updated_at"] = datetime.now().isoformat(timespec="seconds")
        self._write(state)
        self.events.setdefault(job_id, threading.Event()).set()
        return self._public(state)

    def resume(self, job_id):
        state = self._read(job_id)
        if state.get("status") not in {"failed", "cancelled", "interrupted"}:
            raise ValueError("このジョブは再開できません。")
        state["status"] = "queued"
        state["message"] = "失敗したチャンクから再開します。"
        state["error"] = ""
        state["updated_at"] = datetime.now().isoformat(timespec="seconds")
        self._write(state)
        self._launch(job_id)
        return self._public(state)

    def _launch(self, job_id):
        with self.guard:
            thread = self.threads.get(job_id)
            if thread and thread.is_alive():
                return
            event = threading.Event()
            self.events[job_id] = event
            thread = threading.Thread(target=self._worker, args=(job_id, event), name=f"long-job-{job_id}", daemon=True)
            self.threads[job_id] = thread
            thread.start()

    def _backend_ready(self, config):
        try:
            return requests.get(config["audio_cpp_health_url"], timeout=2).ok
        except requests.RequestException:
            return False

    def _ensure_backend(self, config):
        if self._backend_ready(config):
            return
        self.backend_start_callback(config)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self._backend_ready(config):
                return
            time.sleep(0.5)
        raise RuntimeError("audio.cppへ接続できません。起動状態を確認してから再開してください。")

    def _worker(self, job_id, cancel_event):
        if not self.generation_lock.acquire(blocking=False):
            state = self._read(job_id)
            state.update(status="failed", message="別の音声生成が実行中です。後から再開してください。", error="generation_busy", updated_at=datetime.now().isoformat(timespec="seconds"))
            self._write(state)
            return
        started = time.perf_counter()
        try:
            state = self._read(job_id)
            state.update(status="running", message="長文音声を生成しています。", started_at=state.get("started_at") or datetime.now().isoformat(timespec="seconds"), updated_at=datetime.now().isoformat(timespec="seconds"))
            self._write(state)
            plan, config = state["plan"], state["config"]
            chunks_dir = self._job_dir(job_id) / "chunks"
            for index in range(int(state.get("current", 0)), len(plan)):
                if cancel_event.is_set():
                    state.update(status="cancelled", message="生成を停止しました。再開できます。", updated_at=datetime.now().isoformat(timespec="seconds"))
                    self._write(state)
                    return
                target = chunks_dir / f"{index:05d}.wav"
                if target.is_file() and target.stat().st_size >= 44:
                    with target.open("rb") as handle:
                        valid_existing = handle.read(4) == b"RIFF"
                else:
                    valid_existing = False
                if valid_existing:
                    state["current"] = index + 1
                    self._write(state)
                    continue
                last_error = None
                for attempt in range(1, 3):
                    state.update(attempt=attempt, message=f"チャンク {index + 1}/{len(plan)} を生成しています（{attempt}回目）。", updated_at=datetime.now().isoformat(timespec="seconds"))
                    self._write(state)
                    try:
                        self._ensure_backend(config)
                        wav = synthesize_chunk(plan[index]["text"], plan[index].get("voice_ref", ""), config)
                        temporary = target.with_suffix(".tmp")
                        temporary.write_bytes(wav)
                        temporary.replace(target)
                        last_error = None
                        break
                    except Exception as exc:
                        last_error = exc
                        if attempt < 2:
                            time.sleep(1)
                if last_error:
                    raise RuntimeError(f"チャンク {index + 1}/{len(plan)} の生成に失敗しました: {last_error}")
                state["current"] = index + 1
                state["attempt"] = 0
                self._write(state)
            paths = [chunks_dir / f"{index:05d}.wav" for index in range(len(plan))]
            output = combine_wavs(paths, [item["silence_after_ms"] for item in plan], config.get("fade_ms", 5))
            outputs = self.root / "outputs"
            outputs.mkdir(parents=True, exist_ok=True)
            filename = f"irodori_{datetime.now():%Y%m%d_%H%M%S_%f}.wav"
            target = outputs / filename
            shutil.move(str(output), target)
            elapsed = time.perf_counter() - started
            voices = sorted({Path(item.get("voice_ref") or "").name for item in plan if item.get("voice_ref")})
            if state.get("temporary_voice_name") and "reference.wav" in voices:
                voices = [state["temporary_voice_name"] if name == "reference.wav" else name for name in voices]
            info = {"filename": filename, "url": f"/outputs/{filename}", "created_at": datetime.now().isoformat(timespec="seconds"), "seconds": round(elapsed, 3), "chunks": len(plan), "voices": voices, "text": state["text_preview"]}
            self.history_callback(info)
            state.update(status="completed", message="長文音声の生成が完了しました。", filename=filename, url=info["url"], seconds=info["seconds"], voices=voices, finished_at=datetime.now().isoformat(timespec="seconds"), updated_at=datetime.now().isoformat(timespec="seconds"))
            self._write(state)
            shutil.rmtree(chunks_dir, ignore_errors=True)
            (self._job_dir(job_id) / "reference.wav").unlink(missing_ok=True)
        except Exception as exc:
            try:
                state = self._read(job_id)
                state.update(status="failed", message="長文生成を一時停止しました。原因を確認して再開できます。", error=str(exc), updated_at=datetime.now().isoformat(timespec="seconds"))
                self._write(state)
            except Exception:
                pass
        finally:
            self.generation_lock.release()
