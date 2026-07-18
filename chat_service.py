import json
import subprocess
import time
from pathlib import Path

import requests


def load_character(path):
    path = Path(path)
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("キャラクターJSONはオブジェクト形式にしてください。")
    name = str(value.get("name") or value.get("speaker_name") or path.stem).strip()
    if not name:
        raise ValueError("キャラクター名がありません。")
    value["name"] = name
    return value


def character_prompt(character):
    direct = character.get("system_prompt") or character.get("system_prompt_injection")
    if direct:
        return str(direct)
    parts = [f"あなたは「{character['name']}」として日本語で自然に会話してください。"]
    for key in ("description", "role", "personality", "speech_style", "relationship_to_user", "conversation_policy", "rules"):
        value = character.get(key)
        if value:
            parts.append(f"{key}: {json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value}")
    return "\n".join(parts)


def llm_health(settings):
    try:
        response = requests.get(settings["llm_health_url"], timeout=0.35)
        return response.ok
    except Exception:
        return False


def run_command(command):
    command = str(command or "").strip()
    if command:
        subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", command], check=True, timeout=180)


def ensure_llm(settings):
    if llm_health(settings):
        return
    command = settings.get("llm_start_command")
    if not command:
        raise RuntimeError("LLMへ接続できません。会話タブの起動例を参考にLLMを起動してください。")
    run_command(command)
    deadline = time.monotonic() + 150
    while time.monotonic() < deadline:
        if llm_health(settings):
            return
        time.sleep(0.5)
    raise RuntimeError("LLMの起動確認がタイムアウトしました。")


def chat(character, user_text, history, settings):
    ensure_llm(settings)
    messages = [{"role": "system", "content": character_prompt(character)}]
    messages.extend(history[-int(settings.get("llm_max_history", 12)):])
    messages.append({"role": "user", "content": user_text})
    headers = {"Content-Type": "application/json"}
    if settings.get("llm_api_key"):
        headers["Authorization"] = f"Bearer {settings['llm_api_key']}"
    response = requests.post(
        settings["llm_endpoint"],
        headers=headers,
        json={
            "model": settings.get("llm_model", "character-chat"),
            "messages": messages,
            "temperature": float(settings.get("llm_temperature", 0.7)),
            "max_tokens": int(settings.get("llm_max_tokens", 512)),
            "stream": False,
        },
        timeout=300,
    )
    if not response.ok:
        raise RuntimeError(f"LLM error {response.status_code}: {response.text[:300]}")
    data = response.json()
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError("LLMから返答がありませんでした。")
    message = choices[0].get("message") or {}
    text = str(message.get("content") or "").strip()
    if not text:
        raise RuntimeError("LLMの返答本文が空です。")
    return text
