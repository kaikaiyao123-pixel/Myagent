# qa — 质量门禁脚本

三个零外部依赖的检查脚本，**不需要 MySQL / Redis / Milvus / LLM** 即可运行，
可单独执行，也可直接挂进 CI。

| 脚本 | 作用 | 耗时 | 失败即代表 |
|---|---|---|---|
| `static_scan.py` | AST 级缺陷扫描：裸 except、静默吞异常、可变默认参数、`eval`/`subprocess`/`pickle.loads`、SQL 拼接、硬编码密钥、关闭 TLS 校验 | < 1s | 代码卫生退化 |
| `contract_check.py` | 后端路由表（真实构建 FastAPI 路由）vs 前端 `/api/v1/*` 调用地址 | ~ 5s | 前端调用了不存在的接口（运行时 404） |
| `test_api_authz.py` | 接口鉴权测试：真实路由 + 真实中间件 + 真实 JWT 依赖，仅对 DAO 打桩 | ~ 8s | 鉴权/越权缺陷回归 |

> `contract_check.py` 默认只报告、退出码为 0（便于先看清存量问题）；
> 加 `--strict` 后只要存在前端调用 404 就以非零码退出，可直接用作 CI 门禁。

## 运行

```bash
# Windows
PY=src/backend/.venv/Scripts/python.exe
# Linux / macOS
PY=src/backend/.venv/bin/python

$PY qa/static_scan.py                  # 输出到 qa/static_findings.json
$PY qa/contract_check.py               # 只报告
$PY qa/contract_check.py --strict      # CI 门禁：存在 404 即非零退出
$PY qa/test_api_authz.py -v            # 17 个用例
```

无 `.venv` 时先安装后端依赖：

```bash
cd src/backend && uv sync        # 或 pip install -r requirements.txt
```

## 设计说明

**为什么接口测试不需要数据库？**
被测对象是"授权判定逻辑"本身——路由、`WhitelistMiddleware`、`get_login_user` 依赖、
JWT 解码、响应封装全部使用仓库真实代码；只有 `*Dao` 层被打桩返回固定数据。
因此断言"匿名请求是否进入业务逻辑"、"用户 A 能否拿到用户 B 的数据"是可信的，
且不依赖任何外部服务。

**契约检查如何匹配动态路由？**
后端 `/api/v1/mcp_user_config/{config_id}` 与前端 `` `/api/v1/mcp_user_config/${id}` ``
会统一归一化为 `/api/v1/mcp_user_config/{}` 后比对。

## 已知边界

- 静态扫描的 `sql-fstring` 规则有 18 处误报：`f"Select Dialog Appear Error: {err}"`
  这类错误文案中的 "Select" 被误判。人工复核后可忽略，或收窄正则后再启用。
- `test_api_authz.py` 依赖 `agentchat/config.yaml` 仅用于填充 `app_settings`，
  不会建立任何连接。

完整结论见 [TEST_REPORT.md](./TEST_REPORT.md)。
