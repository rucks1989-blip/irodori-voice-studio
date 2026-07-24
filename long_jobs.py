import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path

import requests
import soundfile as sf

from studio_core import _log_tts_event, build_plan, combine_wavs, synthesize_chunk, validate_voice_reference


class LongJobManager:
    def __init__(self, root, generation_lock, history_callback, backend_start_callback):
        self.root = Path(root)
        self.folder = self.root / "outputs" / "jobs"
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

    def _append_log(self, job_id, event, **values):
        record = {
            "timestamp": datetime.now().astimezone().isoformat(timespec="milliseconds"),
            "job_id": job_id,
            "event": event,
            **values,
        }
        try:
            with (self._job_dir(job_id) / "app.log").open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        except OSError:
            pass

    def _chunk_paths(self, job_id, index):
        stem = f"chunk_{index + 1:04d}"
        folder = self._job_dir(job_id) / "chunks"
        return folder / f"{stem}.wav", folder / f"{stem}.txt", folder / f"{stem}.json"

    @staticmethod
    def _write_json(path, value):
        temporary = path.with_suffix(path.suffix + f".{threading.get_ident()}.tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
        temporary.replace(path)

    @staticmethod
    def _wav_info(path):
        info = sf.info(str(path))
        return {
            "output_wav_size": Path(path).stat().st_size,
            "output_duration_sec": round(info.duration, 6),
            "sample_rate": info.samplerate,
            "channels": info.channels,
            "frames": info.frames,
            "subtype": info.subtype,
        }

    def _backend_identity(self):
        exe = self.root / "runtime" / "audio_cpp" / "audiocpp_server.exe"
        result = {"path": str(exe.resolve()), "sha256": None, "size": None}
        try:
            result["sha256"] = hashlib.sha256(exe.read_bytes()).hexdigest()
            result["size"] = exe.stat().st_size
        except OSError:
            pass
        return result

    def _app_revision(self):
        checkout = self.root / "investigation" / "github-latest"
        try:
            return subprocess.run(
                ["git", "-c", f"safe.directory={checkout}", "-C", str(checkout), "rev-parse", "HEAD"],
                check=True, capture_output=True, text=True, encoding="utf-8",
            ).stdout.strip()
        except Exception:
            return None

    def _capture_backend_logs(self, job_id):
        destination = self._job_dir(job_id)
        pointer = self.root / "logs" / "audio-cpp-current.json"
        candidates = []
        try:
            current = json.loads(pointer.read_text(encoding="utf-8-sig"))
            candidates.extend(Path(value) for key, value in current.items() if key in {"stdout", "stderr", "trace"} and value)
        except Exception:
            candidates.extend([
                self.root / "logs" / "audio-cpp.stdout.log",
                self.root / "logs" / "audio-cpp.stderr.log",
                self.root / "logs" / "audio-cpp.trace.log",
            ])
        for source in candidates:
            try:
                resolved = source if source.is_absolute() else self.root / source
                if resolved.is_file():
                    shutil.copy2(resolved, destination / f"server-{resolved.name}")
            except OSError:
                continue

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
        temporary = path.with_name(
            f"job-{threading.get_ident()}-{uuid.uuid4().hex}.tmp"
        )
        try:
            temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            for attempt in range(5):
                try:
                    temporary.replace(path)
                    break
                except PermissionError:
                    if attempt == 4:
                        raise
                    time.sleep(0.02 * (attempt + 1))
        finally:
            temporary.unlink(missing_ok=True)

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
        reference_dir = job_dir / "reference"
        reference_dir.mkdir()
        config = dict(config)
        plan = [dict(item) for item in plan]
        if temporary_voice:
            config["default_voice"] = str(Path(temporary_voice).resolve())
            plan = build_plan(text, config, self.root)
        references = {}
        for item in plan:
            source_value = str(item.get("voice_ref") or "")
            if not source_value:
                continue
            try:
                info = validate_voice_reference(source_value, required=True)
                digest = info["voice_ref_sha256"]
                if digest not in references:
                    source = Path(info["voice_ref_path"])
                    target = reference_dir / f"{len(references) + 1:02d}_{digest[:12]}_{source.name}"
                    shutil.copy2(source, target)
                    copied = validate_voice_reference(target, required=True)
                    if copied["voice_ref_sha256"] != digest:
                        raise RuntimeError("ジョブへ保存した参照WAVのハッシュが一致しません。")
                    references[digest] = {
                        **copied,
                        "original_path": info["voice_ref_path"],
                        "snapshot_path": str(target.resolve()),
                        "display_name": temporary_voice_name if temporary_voice and Path(source_value).resolve() == Path(temporary_voice).resolve() else source.name,
                    }
                item["voice_ref"] = references[digest]["snapshot_path"]
                item["voice_ref_sha256"] = digest
            except (OSError, ValueError) as exc:
                item["voice_ref_required"] = True
                item["voice_ref_original_path"] = source_value
                item["voice_ref_validation_error"] = f"{type(exc).__name__}: {exc}"
        now = datetime.now().isoformat(timespec="seconds")
        for index, item in enumerate(plan):
            _, text_path, metadata_path = self._chunk_paths(job_id, index)
            text_path.write_text(str(item.get("text") or ""), encoding="utf-8", newline="\n")
            reference = references.get(item.get("voice_ref_sha256"))
            reference_required = bool(item.get("voice_ref_required") or item.get("voice_ref"))
            self._write_json(metadata_path, {
                "chunk_index": index + 1,
                "text": item.get("text", ""),
                "status": "pending",
                "attempt": 0,
                "request_mode": "clone" if reference_required else "tts",
                "voice_ref_requested": reference_required,
                "voice_ref_path": item.get("voice_ref_original_path") or item.get("voice_ref") or "",
                "voice_ref_exists": bool(reference),
                "voice_ref_sha256": reference.get("voice_ref_sha256") if reference else None,
                "voice_ref_size": reference.get("voice_ref_size") if reference else None,
                "voice_ref_mtime": reference.get("voice_ref_mtime") if reference else None,
                "reference_cache_status": "unknown",
                "request_started_at": None,
                "request_finished_at": None,
                "output_wav": None,
                "output_wav_size": None,
                "output_duration_sec": None,
                "sample_rate": None,
                "channels": None,
                "retry_reason": None,
                "error": None,
            })
        self._write_json(reference_dir / "voice_ref.json", {"references": list(references.values())})
        state = {
            "id": job_id, "status": "queued", "message": "生成を開始します。",
            "created_at": now, "updated_at": now, "started_at": "", "finished_at": "",
            "current": 0, "attempt": 0, "error": "", "filename": "", "url": "",
            "seconds": 0, "voices": [], "temporary_voice_name": temporary_voice_name, "text_preview": text[:160],
            "text": text, "config": config, "plan": plan,
            "job_id": job_id, "input_text": text,
            "split_rule": {"target_chars": config.get("target_chars"), "backtrack_chars": config.get("backtrack_chars")},
            "total_chunks": len(plan), "success_chunks": 0, "failed_chunks": 0,
            "model": config.get("model"), "audio_cpp": self._backend_identity(),
            "gpu": None, "references": list(references.values()),
            "final_wav": None, "final_wav_size": None, "final_wav_duration_sec": None,
            "result": "queued", "error_summary": None, "retry_count": 0,
            "app_revision": self._app_revision(),
        }
        self._write(state)
        self._append_log(job_id, "job_created", total_chunks=len(plan), references=len(references))
        self._capture_backend_logs(job_id)
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
                target, text_path, metadata_path = self._chunk_paths(job_id, index)
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
                    chunk_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                    chunk_metadata.update({
                        "status": "running",
                        "attempt": attempt,
                        "request_started_at": datetime.now().astimezone().isoformat(timespec="milliseconds"),
                        "request_finished_at": None,
                        "retry_reason": str(last_error) if last_error else None,
                        "error": None,
                    })
                    self._write_json(metadata_path, chunk_metadata)
                    state.update(attempt=attempt, message=f"チャンク {index + 1}/{len(plan)} を生成しています（{attempt}回目）。", updated_at=datetime.now().isoformat(timespec="seconds"))
                    self._write(state)
                    self._append_log(job_id, "chunk_start", chunk_index=index + 1, attempt=attempt, text_path=text_path.name)
                    try:
                        self._ensure_backend(config)
                        if plan[index].get("voice_ref_validation_error"):
                            raise ValueError(plan[index]["voice_ref_validation_error"])
                        _log_tts_event(
                            "long_job_chunk",
                            job_id=job_id,
                            chunk_index=index,
                            chunk_number=index + 1,
                            chunk_total=len(plan),
                            attempt=attempt,
                            chunk_length=len(plan[index].get("text", "")),
                            chunk_reason=plan[index].get("reason", ""),
                            speaker=plan[index].get("speaker", ""),
                            speaker_match=plan[index].get("speaker_match", ""),
                            voice_ref=plan[index].get("voice_ref", ""),
                        )
                        diagnostics = {
                            "job_id": job_id,
                            "chunk_index": index,
                            "chunk_number": index + 1,
                            "chunk_total": len(plan),
                            "attempt": attempt,
                            "request_mode": "clone" if plan[index].get("voice_ref_required") or plan[index].get("voice_ref") else "tts",
                        }
                        wav = synthesize_chunk(
                            plan[index]["text"], plan[index].get("voice_ref", ""), config,
                            diagnostics,
                        )
                        temporary = target.with_suffix(".tmp")
                        temporary.write_bytes(wav)
                        temporary.replace(target)
                        chunk_metadata.update({
                            "status": "success",
                            "request_id": diagnostics.get("request_id"),
                            "request_mode": diagnostics.get("request_mode"),
                            "request": diagnostics.get("request"),
                            "voice_reference": diagnostics.get("voice_reference"),
                            "voice_ref_requested": bool(plan[index].get("voice_ref_required") or plan[index].get("voice_ref")),
                            "voice_ref_path": diagnostics.get("request", {}).get("voice_ref_path") or plan[index].get("voice_ref") or "",
                            "voice_ref_exists": diagnostics.get("voice_reference", {}).get("voice_ref_exists", False),
                            "voice_ref_sha256": diagnostics.get("voice_reference", {}).get("voice_ref_sha256"),
                            "voice_ref_size": diagnostics.get("voice_reference", {}).get("voice_ref_size"),
                            "voice_ref_mtime": diagnostics.get("voice_reference", {}).get("voice_ref_mtime"),
                            "request_started_at": diagnostics.get("request_started_at"),
                            "request_finished_at": diagnostics.get("request_finished_at"),
                            "http_status": diagnostics.get("http_status"),
                            "output_wav": target.name,
                            **self._wav_info(target),
                            "error": None,
                        })
                        self._write_json(metadata_path, chunk_metadata)
                        self._append_log(job_id, "chunk_success", chunk_index=index + 1, attempt=attempt, request_id=diagnostics.get("request_id"), output=target.name)
                        last_error = None
                        break
                    except Exception as exc:
                        last_error = exc
                        retryable = not isinstance(exc, ValueError) and attempt < 2
                        state["retry_count"] = int(state.get("retry_count", 0)) + (1 if attempt > 1 else 0)
                        chunk_metadata.update({
                            "status": "retrying" if retryable else "failed",
                            "request_finished_at": datetime.now().astimezone().isoformat(timespec="milliseconds"),
                            "error": f"{type(exc).__name__}: {exc}",
                        })
                        self._write_json(metadata_path, chunk_metadata)
                        self._append_log(job_id, "chunk_error", chunk_index=index + 1, attempt=attempt, error_type=type(exc).__name__, error=str(exc))
                        if not retryable:
                            break
                        if attempt < 2:
                            time.sleep(1)
                if last_error:
                    state["failed_chunks"] = int(state.get("failed_chunks", 0)) + 1
                    raise RuntimeError(f"チャンク {index + 1}/{len(plan)} の生成に失敗しました: {last_error}")
                state["current"] = index + 1
                state["success_chunks"] = index + 1
                state["attempt"] = 0
                self._write(state)
            paths = [self._chunk_paths(job_id, index)[0] for index in range(len(plan))]
            output = combine_wavs(paths, [item["silence_after_ms"] for item in plan], config.get("fade_ms", 5))
            outputs = self.root / "outputs"
            outputs.mkdir(parents=True, exist_ok=True)
            filename = f"irodori_{datetime.now():%Y%m%d_%H%M%S_%f}.wav"
            target = outputs / filename
            shutil.move(str(output), target)
            final_target = self._job_dir(job_id) / "final.wav"
            shutil.copy2(target, final_target)
            final_info = self._wav_info(final_target)
            elapsed = time.perf_counter() - started
            voices = sorted({Path(item.get("voice_ref") or "").name for item in plan if item.get("voice_ref")})
            if state.get("temporary_voice_name") and "reference.wav" in voices:
                voices = [state["temporary_voice_name"] if name == "reference.wav" else name for name in voices]
            info = {"filename": filename, "url": f"/outputs/{filename}", "created_at": datetime.now().isoformat(timespec="seconds"), "seconds": round(elapsed, 3), "chunks": len(plan), "voices": voices, "text": state["text_preview"]}
            self.history_callback(info)
            state.update(
                status="completed", message="長文音声の生成が完了しました。", filename=filename,
                url=info["url"], seconds=info["seconds"], voices=voices,
                finished_at=datetime.now().isoformat(timespec="seconds"), updated_at=datetime.now().isoformat(timespec="seconds"),
                final_wav="final.wav", final_wav_size=final_info["output_wav_size"],
                final_wav_duration_sec=final_info["output_duration_sec"], result="completed",
                error_summary=None, success_chunks=len(plan), failed_chunks=0,
            )
            self._write(state)
            self._append_log(job_id, "job_completed", final_wav="final.wav", elapsed_sec=round(elapsed, 3))
            self._capture_backend_logs(job_id)
            retention = str(config.get("chunk_wav_retention") or "keep")
            if retention in {"delete_on_success", "delete_always"}:
                for chunk_wav in chunks_dir.glob("chunk_*.wav"):
                    chunk_wav.unlink(missing_ok=True)
        except Exception as exc:
            try:
                state = self._read(job_id)
                state.update(
                    status="failed", message="長文生成を一時停止しました。原因を確認して再開できます。",
                    error=str(exc), error_summary=str(exc), result="failed",
                    finished_at=datetime.now().isoformat(timespec="seconds"),
                    updated_at=datetime.now().isoformat(timespec="seconds"),
                )
                self._write(state)
                self._append_log(job_id, "job_failed", error_type=type(exc).__name__, error=str(exc), traceback=traceback.format_exc())
                self._capture_backend_logs(job_id)
            except Exception:
                pass
        finally:
            self.generation_lock.release()
