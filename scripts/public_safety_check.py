import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP = {".git", ".venv", "data", "outputs", "logs", "models", "runtime", "__pycache__"}
SKIP_FILES = {"settings.local.json"}
DENIED_SUFFIXES = {".wav", ".mp3", ".flac", ".safetensors", ".ckpt", ".pth", ".pt", ".gguf", ".onnx", ".bin"}
problems = []
for path in ROOT.rglob("*"):
    if not path.is_file() or path.name in SKIP_FILES or any(part in SKIP for part in path.relative_to(ROOT).parts):
        continue
    if path.suffix.lower() in DENIED_SUFFIXES:
        problems.append(f"公開禁止形式: {path.relative_to(ROOT)}")
        continue
    if path.stat().st_size > 5 * 1024 * 1024:
        problems.append(f"5MB超過: {path.relative_to(ROOT)}")
        continue
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        continue
    if re.search(r"(?<![A-Za-z])[A-Za-z]:[\\/]", text):
        problems.append(f"Windows絶対パス: {path.relative_to(ROOT)}")

if problems:
    print("公開安全チェック: NG")
    print("\n".join(f"- {item}" for item in problems))
    sys.exit(1)
print("公開安全チェック: OK")
