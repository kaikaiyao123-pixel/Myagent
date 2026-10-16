"""
Upload test-rag-data files to AgentChat knowledge base via API.
This script:
1. Uploads each file to MinIO via /api/v1/upload
2. Creates knowledge_file record via /api/v1/knowledge_file/create
3. Checks status until all files are processed
"""
import requests
import time
import os
import sys

# 宿主机开了系统代理（Clash 7897），必须禁用，否则 localhost 请求会被劫持
os.environ["NO_PROXY"] = "*"
os.environ.pop("HTTP_PROXY", None)
os.environ.pop("HTTPS_PROXY", None)
os.environ.pop("http_proxy", None)
os.environ.pop("https_proxy", None)

SESSION = requests.Session()
SESSION.trust_env = False  # 完全忽略环境变量代理

# 读取 JWT token（由容器内生成，scripts/kb_token.txt）
_TOKEN_FILE = os.path.join(os.path.dirname(__file__), "kb_token.txt")
with open(_TOKEN_FILE, "r") as f:
    JWT_TOKEN = f.read().strip()
SESSION.headers.update({"Authorization": f"Bearer {JWT_TOKEN}"})

API_BASE = "http://127.0.0.1:7860/api/v1"
KNOWLEDGE_ID = "t_bfe98e8358d24a97"
DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "test-rag-data")

files_to_upload = [
    "AgentChat项目说明.md",
    "AgentChat技术问答手册.md",
    "部署运维手册.md",
    "AgentChat产品功能列表.md",
]

def upload_file(filename):
    filepath = os.path.join(DATA_DIR, filename)
    url = f"{API_BASE}/upload"

    with open(filepath, "rb") as f:
        files = {"file": (filename, f, "text/markdown")}
        resp = SESSION.post(url, files=files, timeout=30)

    print(f"  Upload {filename}: status={resp.status_code}")
    if resp.status_code != 200:
        print(f"  ERROR: {resp.text[:200]}")
        return None

    result = resp.json()
    sign_url = result.get("data")
    if isinstance(sign_url, dict):
        sign_url = sign_url.get("sign_url") or sign_url.get("url")
    print(f"  Got sign_url: {sign_url}")
    return sign_url


def create_knowledge_file(filename, sign_url):
    url = f"{API_BASE}/knowledge_file/create"
    payload = {
        "knowledge_id": KNOWLEDGE_ID,
        "file_name": filename,
        "file_url": sign_url,
    }
    resp = SESSION.post(url, json=payload, timeout=120)
    print(f"  Create knowledge_file: status={resp.status_code}")
    if resp.status_code != 200:
        print(f"  ERROR: {resp.text[:200]}")
        return None
    result = resp.json()
    file_id = result.get("data", {}).get("id") if isinstance(result.get("data"), dict) else result.get("data")
    print(f"  File ID: {file_id}")
    return file_id


def check_status():
    url = f"{API_BASE}/knowledge_file/select?knowledge_id={KNOWLEDGE_ID}"
    resp = SESSION.get(url, timeout=10)
    if resp.status_code != 200:
        print(f"  Status check failed: {resp.status_code}")
        return []
    result = resp.json()
    files = result.get("data", [])
    return files


def main():
    print(f"=== Uploading {len(files_to_upload)} files to knowledge base '{KNOWLEDGE_ID}' ===\n")

    results = []
    for filename in files_to_upload:
        print(f"\n--- Processing: {filename} ---")
        sign_url = upload_file(filename)
        if not sign_url:
            results.append((filename, "UPLOAD_FAILED"))
            continue

        file_id = create_knowledge_file(filename, sign_url)
        if not file_id:
            results.append((filename, "CREATE_FAILED"))
            continue

        results.append((filename, file_id))

    print("\n=== All uploads submitted. Waiting for processing... ===\n")

    # Poll status
    for attempt in range(12):
        time.sleep(5)
        files = check_status()
        print(f"\n[Attempt {attempt+1}] File statuses:")
        for f in files:
            name = f.get("file_name", "?")
            status = f.get("status", "?")
            print(f"  {name}: {status}")

        all_done = all(f.get("status") in ("success", "fail") for f in files) if files else False
        if all_done and len(files) == len(files_to_upload):
            print("\n=== All files processed! ===")
            break
    else:
        print("\n=== Timeout waiting for processing ===")

    # Final summary
    print("\n=== Final Status ===")
    files = check_status()
    for f in files:
        name = f.get("file_name", "?")
        status = f.get("status", "?")
        print(f"  {name}: {status}")


if __name__ == "__main__":
    main()
