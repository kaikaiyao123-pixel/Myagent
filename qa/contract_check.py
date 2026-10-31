"""前后端接口契约一致性检查。

后端：真实构建 FastAPI 路由表（不触发 lifespan，不连任何中间件依赖）。
前端：正则抽取 src/**/*.ts|vue 中出现的 /api/v1/... 调用地址。
比对：前端调用的路径若在后端路由表中找不到匹配，即为契约断裂（运行时 404）。
"""
import os
import re
import sys
from pathlib import Path

REPO = Path(os.environ.get("AGENTCHAT_REPO") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(REPO / "src" / "backend"))
os.environ.setdefault("AUTHJWT_SECRET_KEY", "qa-fixed-secret-for-testing-only")

import asyncio  # noqa: E402

from agentchat.settings import init_app_settings  # noqa: E402

asyncio.run(init_app_settings(str(REPO / "src" / "backend" / "agentchat" / "config.yaml")))

from fastapi import FastAPI  # noqa: E402
from agentchat.api.router import router as api_router  # noqa: E402

app = FastAPI()
app.include_router(api_router)

backend = []
for r in app.routes:
    methods = getattr(r, "methods", None) or set()
    path = getattr(r, "path", "")
    if not path.startswith("/api"):
        continue
    for m in sorted(methods - {"HEAD", "OPTIONS"}):
        backend.append((m, path))


def norm_backend(p: str) -> str:
    return re.sub(r"\{[^}]+\}", "{}", p)


def norm_front(p: str) -> str:
    p = p.split("?")[0]
    p = re.sub(r"\$\{[^}]*\}", "{}", p)
    p = re.sub(r":[A-Za-z_][A-Za-z0-9_]*", "{}", p)
    return p


front = {}
pat = re.compile(r"""['"`](/api/v1/[^'"`\s]+)['"`]""")
for f in list((REPO / "src/frontend/src").rglob("*.ts")) + list((REPO / "src/frontend/src").rglob("*.vue")):
    txt = f.read_text(encoding="utf-8", errors="replace")
    for m in pat.finditer(txt):
        url = m.group(1)
        front.setdefault(norm_front(url), set()).add(str(f.relative_to(REPO)).replace("\\", "/"))

b_norm = {}
for meth, p in backend:
    b_norm.setdefault(norm_backend(p), set()).add(meth)

print(f"后端路由: {len(backend)} 条（唯一路径 {len(b_norm)}）")
print(f"前端调用: {len(front)} 条（唯一路径）")

missing = sorted(k for k in front if k not in b_norm)
print(f"\n前端调用但后端无对应路由: {len(missing)}")
for k in missing:
    print(f"  [404] {k}   ← {', '.join(sorted(front[k])[:2])}")

unused = sorted(k for k in b_norm if k not in front and "/mcp/" not in k)
print(f"\n后端存在但前端未调用（信息项，可能有脚本/第三方调用）: {len(unused)}")
for k in unused[:25]:
    print(f"  [unused] {sorted(b_norm[k])} {k}")

# --strict：存在前端调用但后端无路由时以非零码退出，可用于 CI 门禁
if "--strict" in sys.argv and missing:
    print(f"\n[FAIL] --strict 模式：{len(missing)} 处前端调用无对应后端路由")
    raise SystemExit(1)
