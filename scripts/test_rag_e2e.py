"""
End-to-end RAG test: create dialog with RAG agent, ask a question,
verify the response references knowledge base content.
"""
import os
import sys
import json
import time
import subprocess

os.environ["NO_PROXY"] = "*"
os.environ.pop("HTTP_PROXY", None)
os.environ.pop("HTTPS_PROXY", None)
os.environ.pop("http_proxy", None)
os.environ.pop("https_proxy", None)

import requests

API = "http://127.0.0.1:7860/api/v1"
AGENT_ID = "kb_rag_test_agent_001"
PROJ = "E:/Code/AgentChat-main"

# 1) Generate fresh JWT token inside container
token = subprocess.check_output([
    "docker", "exec", "-w", "/app", "agentchat-backend", "/app/.venv/bin/python", "-c",
    "import asyncio, json; "
    "from agentchat.api.JWT import Settings as AuthJwtSettings; "
    "from agentchat.auth.auth_jwt import AuthJWT; "
    "from agentchat.utils.JWT import ACCESS_TOKEN_EXPIRE_TIME; "
    "AuthJWT.load_config(lambda: AuthJwtSettings()); "
    "payload={'user_name':'xxy','user_id':'1','role':'admin'}; "
    "print(AuthJWT().create_access_token(subject=json.dumps(payload), expires_time=ACCESS_TOKEN_EXPIRE_TIME))"
]).decode().strip()

s = requests.Session()
s.trust_env = False
s.headers.update({"Authorization": f"Bearer {token}"})

print("[1] Create dialog with RAG agent...")
r = s.post(f"{API}/dialog", json={"name": "RAG测试对话", "agent_id": AGENT_ID, "agent_type": "agent"})
print("    status:", r.status_code)
dialog = r.json().get("data", {})
dialog_id = dialog.get("dialog_id") if isinstance(dialog, dict) else dialog
print("    dialog_id:", dialog_id)

if not dialog_id:
    print("    FAILED to create dialog:", r.text[:300])
    sys.exit(1)

print("[2] Send question: 'AgentChat 支持哪些文件格式上传到知识库？'")
r = s.post(f"{API}/completion", json={
    "dialog_id": dialog_id,
    "user_input": "AgentChat 支持哪些文件格式上传到知识库？"
}, stream=True, timeout=120)

print("    status:", r.status_code)
if r.status_code != 200:
    print("    ERROR:", r.text[:500])
    sys.exit(1)

print("[3] Streaming response:")
answer = []
tool_called = False
for line in r.iter_lines(decode_unicode=True):
    if not line:
        continue
    # SSE format: data: {...}
    if line.startswith("data:"):
        try:
            payload = json.loads(line[5:].strip())
            if isinstance(payload, dict):
                ptype = payload.get("type")
                pdata = payload.get("data", {})
                if ptype == "event" and "检索知识库" in str(pdata.get("title", "")):
                    tool_called = True
                if ptype == "response_chunk":
                    chunk = pdata.get("chunk")
                    if chunk:
                        answer.append(str(chunk))
        except Exception:
            pass

full = "".join(answer)
print("    --- ANSWER START ---")
print("   ", full[:1500])
print("    --- ANSWER END ---")

print(f"[4] RAG tool invoked: {'YES' if tool_called else 'NO'}")
keywords = ["PDF", "Word", "TXT", "Markdown", "markdown", "pdf", "txt", "docx", "格式"]
hit = [k for k in keywords if k.lower() in full.lower()]
print(f"[5] KB content referenced: {'YES' if hit else 'NO'} (matched: {hit})")
