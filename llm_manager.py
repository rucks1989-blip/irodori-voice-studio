import json
import os
import re
import shutil
import string
import subprocess
import time
from pathlib import Path

import requests


EXCLUDED_DIRS = {
    "$recycle.bin", "system volume information", "windows", "program files",
    "program files (x86)", ".git", ".venv", "node_modules", "__pycache__",
    "outputs", "logs", "cache", "temp",
}


def _safe_name(value):
    text = re.sub(r"[^0-9A-Za-z._-]+", "-", str(value or "").strip()).strip("-.")
    return text or "local-model"


def _run(command, timeout=300):
    return subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)


def find_ollama_executable():
    found = shutil.which("ollama.exe") or shutil.which("ollama")
    candidates = [found]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(str(Path(local) / "Programs" / "Ollama" / "ollama.exe"))
    for drive in _fixed_drives():
        candidates.extend([
            str(drive / "Ollama" / "ollama.exe"),
            str(drive / "AI" / "Ollama" / "ollama.exe"),
        ])
    for item in candidates:
        if item and Path(item).is_file():
            return str(Path(item).resolve())
    return ""


def find_llama_server(root):
    root = Path(root).resolve()
    candidates = [
        root / "runtime" / "llama_cpp" / "llama-server.exe",
        root / "vendor" / "llama_cpp_pascal" / "llama-server.exe",
        root.parent / "KurisuModel" / "vendor" / "llama_cpp_pascal" / "llama-server.exe",
    ]
    found = shutil.which("llama-server.exe") or shutil.which("llama-server")
    if found:
        candidates.insert(0, Path(found))
    for path in candidates:
        if Path(path).is_file():
            return str(Path(path).resolve())
    return ""


def ollama_ready():
    try:
        return requests.get("http://127.0.0.1:11434/api/tags", timeout=1).ok
    except requests.RequestException:
        return False


def ensure_ollama(executable):
    if ollama_ready():
        return
    if not executable:
        raise RuntimeError("Ollamaが見つかりません。")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.Popen([executable, "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if ollama_ready():
            return
        time.sleep(0.5)
    raise RuntimeError("Ollamaの起動確認がタイムアウトしました。")


def list_ollama_models(executable=""):
    if not ollama_ready() and executable:
        try:
            ensure_ollama(executable)
        except Exception:
            return []
    try:
        response = requests.get("http://127.0.0.1:11434/api/tags", timeout=3)
        response.raise_for_status()
        result = []
        for item in response.json().get("models", []):
            result.append({
                "name": str(item.get("name") or item.get("model") or ""),
                "size": int(item.get("size") or 0),
                "modified_at": str(item.get("modified_at") or ""),
                "digest": str(item.get("digest") or ""),
            })
        return result
    except (requests.RequestException, ValueError):
        return []


def _fixed_drives():
    return [Path(f"{letter}:\\") for letter in string.ascii_uppercase if Path(f"{letter}:\\").exists()]


def _gguf_info(path, source="gguf"):
    path = Path(path)
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            magic = handle.read(4)
        valid = magic == b"GGUF" and size > 1024 * 1024
        reason = "使用候補です" if valid else "GGUF形式として認識できません"
    except OSError as exc:
        size, valid, reason = 0, False, str(exc)
    return {"source": source, "name": path.name, "path": str(path.resolve()), "size": size, "valid": valid, "reason": reason}


def _scan_folder(folder, depth, results, seen, limit=200):
    folder = Path(folder)
    if not folder.is_dir() or len(results) >= limit:
        return
    try:
        for child in folder.iterdir():
            if len(results) >= limit:
                return
            if child.is_file() and child.suffix.casefold() == ".gguf":
                key = str(child.resolve()).casefold()
                if key not in seen:
                    seen.add(key)
                    results.append(_gguf_info(child))
            elif depth > 0 and child.is_dir() and child.name.casefold() not in EXCLUDED_DIRS:
                _scan_folder(child, depth - 1, results, seen, limit)
    except (OSError, PermissionError):
        return


def discover_gguf(root, deep=False):
    root = Path(root)
    results, seen = [], set()
    local = root / "llm"
    local.mkdir(parents=True, exist_ok=True)
    _scan_folder(local, 6, results, seen)
    home = Path.home()
    common = [home / "Downloads", home / "Documents", home / "Desktop"]
    for drive in _fixed_drives():
        common.extend([drive / "LLM", drive / "Models", drive / "AI", drive / "Ollama", drive / "LM Studio"])
    for folder in common:
        _scan_folder(folder, 3 if deep else 1, results, seen)
    if deep:
        for drive in _fixed_drives():
            _scan_folder(drive, 5, results, seen)
    return results


def discover(root, deep=False):
    executable = find_ollama_executable()
    llama_server = find_llama_server(root)
    return {
        "llm_folder": str((Path(root) / "llm").resolve()),
        "ollama": {"installed": bool(executable), "running": ollama_ready(), "executable": executable, "models": list_ollama_models(executable)},
        "llama_cpp": {"available": bool(llama_server), "executable": llama_server, "gpu_note": "GPU対応は検出したllama.cppビルドに依存します。"},
        "gguf": discover_gguf(root, deep=deep),
        "deep": bool(deep),
    }


def _ps_quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def configure_llama_cpp(root, gguf_path):
    root = Path(root).resolve()
    executable = find_llama_server(root)
    if not executable:
        raise RuntimeError("GPU対応llama.cppが見つかりません。")
    path = Path(gguf_path).resolve()
    local = (root / "llm").resolve()
    if not _gguf_info(path)["valid"]:
        raise ValueError("正常なGGUFを選択してください。")
    if local not in path.parents:
        local.mkdir(parents=True, exist_ok=True)
        target = local / path.name
        if not target.exists():
            shutil.copy2(path, target)
        path = target.resolve()
    data = root / "data"
    data.mkdir(parents=True, exist_ok=True)
    start_script = data / "start_llama_cpp_gpu.ps1"
    stop_script = data / "stop_llama_cpp_gpu.ps1"
    pid_path = root / "logs" / "llama-gpu.pid"
    stdout = root / "logs" / "llama-gpu.stdout.log"
    stderr = root / "logs" / "llama-gpu.stderr.log"
    start_script.write_text(f'''$ErrorActionPreference="Stop"
try{{if((Invoke-RestMethod "http://127.0.0.1:11438/health" -TimeoutSec 2).status -eq "ok"){{exit 0}}}}catch{{}}
if(Get-NetTCPConnection -LocalPort 11438 -State Listen -ErrorAction SilentlyContinue){{throw "11438番ポートが別の処理で使用中です。"}}
New-Item -ItemType Directory -Force -Path {_ps_quote(pid_path.parent)}|Out-Null
$p=Start-Process -FilePath {_ps_quote(executable)} -ArgumentList @("--model",{_ps_quote(path)},"--host","127.0.0.1","--port","11438","--no-webui","--offline","-c","4096","-np","1","-ngl","99","--no-mmap","--flash-attn","off","--cache-ram","0","--reasoning","off","--reasoning-format","none") -WorkingDirectory {_ps_quote(Path(executable).parent)} -RedirectStandardOutput {_ps_quote(stdout)} -RedirectStandardError {_ps_quote(stderr)} -WindowStyle Hidden -PassThru
$p.Id|Set-Content {_ps_quote(pid_path)} -Encoding ascii
$limit=[DateTime]::UtcNow.AddSeconds(150)
while([DateTime]::UtcNow -lt $limit){{if($p.HasExited){{throw "llama.cppが起動途中で終了しました。"}};try{{if((Invoke-RestMethod "http://127.0.0.1:11438/health" -TimeoutSec 2).status -eq "ok"){{exit 0}}}}catch{{}};Start-Sleep -Milliseconds 500}}
throw "llama.cppの起動待機がタイムアウトしました。"
''', encoding="utf-8-sig")
    stop_script.write_text(f'''$pidPath={_ps_quote(pid_path)}
if(Test-Path $pidPath){{$idValue=0;if([int]::TryParse((Get-Content $pidPath -Raw).Trim(),[ref]$idValue)){{$p=Get-Process -Id $idValue -ErrorAction SilentlyContinue;if($p -and $p.Path -eq {_ps_quote(executable)}){{Stop-Process -Id $idValue -Force}}}};Remove-Item $pidPath -Force -ErrorAction SilentlyContinue}}
''', encoding="utf-8-sig")
    return {
        "llm_provider": "llama_cpp",
        "llm_endpoint": "http://127.0.0.1:11438/v1/chat/completions",
        "llm_health_url": "http://127.0.0.1:11438/health",
        "llm_model": path.stem,
        "llm_model_path": str(path),
        "llm_unload_after_reply": True,
        "llm_start_command": str(start_script),
        "llm_stop_command": str(stop_script),
    }


def ollama_model_blob(executable, model):
    ensure_ollama(executable)
    result = _run([executable, "show", model, "--modelfile"], timeout=60)
    if result.returncode:
        raise RuntimeError(f"Ollamaモデル情報を取得できません: {result.stderr.strip()}")
    match = re.search(r"(?m)^FROM\s+(.+?)\s*$", result.stdout)
    if not match:
        raise RuntimeError("OllamaモデルのGGUF実体を特定できません。")
    raw = match.group(1).strip().strip('"')
    path = Path(raw)
    info = _gguf_info(path, source="ollama")
    if not info["valid"]:
        raise RuntimeError("Ollama内部モデルは直接利用できるGGUFではありません。")
    return path


def copy_ollama_model(root, model):
    executable = find_ollama_executable()
    if not executable:
        raise RuntimeError("Ollamaが見つかりません。")
    source = ollama_model_blob(executable, model)
    folder = Path(root) / "llm"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{_safe_name(model.replace(':', '-'))}.gguf"
    if not target.exists() or target.stat().st_size != source.stat().st_size:
        shutil.copy2(source, target)
    info = _gguf_info(target)
    if not info["valid"]:
        raise RuntimeError("コピー後のGGUF検査に失敗しました。")
    return info


def register_gguf_with_ollama(root, gguf_path):
    executable = find_ollama_executable()
    if not executable:
        raise RuntimeError("GGUFは見つかりましたが、実行に使うOllamaがありません。")
    ensure_ollama(executable)
    path = Path(gguf_path).resolve()
    local = (Path(root) / "llm").resolve()
    if not _gguf_info(path)["valid"]:
        raise ValueError("正常なGGUFを選択してください。")
    if local not in path.parents:
        local.mkdir(parents=True, exist_ok=True)
        target = local / path.name
        if target.exists() and target.stat().st_size != path.stat().st_size:
            target = local / f"{_safe_name(path.stem)}-{path.stat().st_size}.gguf"
        if not target.exists():
            shutil.copy2(path, target)
        path = target.resolve()
    alias = f"irodori-{_safe_name(path.stem).lower()}:latest"
    data = Path(root) / "data"
    data.mkdir(parents=True, exist_ok=True)
    modelfile = data / "ollama-import.Modelfile"
    modelfile.write_text(f'FROM "{path}"\nPARAMETER num_ctx 4096\n', encoding="utf-8")
    result = _run([executable, "create", alias, "-f", str(modelfile)], timeout=900)
    if result.returncode:
        raise RuntimeError(f"GGUFをOllamaへ登録できません: {(result.stderr or result.stdout).strip()[-500:]}")
    return {
        "llm_provider": "ollama",
        "llm_endpoint": "http://127.0.0.1:11434/v1/chat/completions",
        "llm_health_url": "http://127.0.0.1:11434/api/tags",
        "llm_model": alias,
        "llm_model_path": str(path),
        "llm_unload_after_reply": True,
    }


def select_ollama_model(model):
    executable = find_ollama_executable()
    ensure_ollama(executable)
    names = {item["name"] for item in list_ollama_models(executable)}
    if model not in names:
        raise ValueError("選択したOllamaモデルが見つかりません。")
    return {
        "llm_provider": "ollama",
        "llm_endpoint": "http://127.0.0.1:11434/v1/chat/completions",
        "llm_health_url": "http://127.0.0.1:11434/api/tags",
        "llm_model": model,
        "llm_model_path": "",
        "llm_unload_after_reply": True,
    }


def unload_ollama(model):
    try:
        requests.post("http://127.0.0.1:11434/api/generate", json={"model": model, "keep_alive": 0}, timeout=30).raise_for_status()
        return True
    except requests.RequestException:
        return False
