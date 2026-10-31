# AgentChat 质量评估报告

> 测试对象：本仓库 `main` 分支（提交 `8d88d0b`）
> 测试日期：2026-10-31
> 测试方式：黑盒接口测试 + 白盒静态分析 + 前后端契约比对 + 前端质量门禁

---

## 一、结论摘要

**整体可运行性良好，但授权模型存在系统性缺口。**

- 仓库自带的 87 项离线单测与 3 道 CI 门禁**全部通过**，说明"稳健型三件套"（语义缓存 / Langfuse 埋点 / 评测 Harness）的工程质量确实到位。
- 但**鉴权是逐接口手工挂载的，没有全局兜底**。86 个端点中 76 个显式声明了 `Depends(get_login_user)`，**10 个未声明**——其中只有登录/注册/头像列表/微信验签 4 个属于合理公开，其余 6 个均可被匿名调用。本次实测确认 **2 个 P0、8 个 P1** 缺陷。
- 前后端存在 **5 处路径/字段契约断裂**，其中"MCP 用户配置保存"因字段名不一致**功能 100% 失败**。
- 前端 `npm run lint`（`vue-tsc --noEmit`）**当前不通过**，24 个类型错误；生产构建可通过，但主 chunk 达 6.46 MB。

### 缺陷统计

| 级别 | 数量 | 含义 |
|---|---|---|
| P0 严重 | 2 | 未鉴权即可读写任意用户数据 |
| P1 高 | 8 | 越权访问、未鉴权调用、核心功能不可用 |
| P2 中 | 5 | 可观测性/安全头/性能缺陷 |
| P3 低 | 5 | 代码卫生、死代码陷阱 |

---

## 二、测试环境与方法

| 项 | 值 |
|---|---|
| OS | Windows 11 (win32) |
| Python | 3.12.2（仓库自带 `.venv`，415 个包已就绪） |
| Node | 22.22.2 |
| 依赖服务 | 未启动 MySQL / Redis / Milvus / ES / MinIO / LLM —— 接口测试对 DAO 层打桩，验证的是**授权判定逻辑本身** |

### 测试手段

| 手段 | 脚本 | 覆盖内容 |
|---|---|---|
| 回归基线 | 复用仓库 `scripts/test_*.py` | 语义缓存、Langfuse、集成接线、评测 Harness |
| 静态分析 | `qa/static_scan.py` | AST 级：语法、裸 except、静默吞异常、可变默认参数、危险调用、SQL 拼接、硬编码密钥、关闭 TLS 校验 |
| 接口鉴权 | `qa/test_api_authz.py` | 真实路由 + 真实中间件 + 真实 `get_login_user` 依赖，17 个用例 |
| 契约比对 | `qa/contract_check.py` | 后端路由表（86 条）vs 前端调用地址（71 条） |
| 前端门禁 | `vue-tsc --noEmit` / `vite build` | 类型检查、生产构建 |

> 说明：接口测试只对**数据访问层（DAO）**打桩，路由、中间件、依赖注入、JWT 校验、响应封装全部是仓库真实代码，因此结论可代表线上行为。

---

## 三、缺陷明细

### P0-1 · 未鉴权即可读取任意用户资料（含密码哈希）

- **位置**：`src/backend/agentchat/api/v1/user.py:95` `GET /api/v1/user/info`
- **成因**：该端点未声明 `Depends(get_login_user)`，且 `user_id` 直接取自查询参数；`UserService.get_user_info_by_id` 调用 `UserTable.to_dict()`，而 `UserTable` 未设置 `hide_fields`，序列化结果包含 `user_password` 字段。
- **复现**：
  ```
  GET /api/v1/user/info?user_id=user-B     # 不带任何凭据
  → 200 {"status_code":200,"data":{...,"user_password":"<sha256>",...}}
  ```
- **证据**：`qa/test_api_authz.py::TestUserEndpointsUnauthenticated::test_user_info_readable_anonymously` 通过。
- **影响**：任意互联网用户可枚举 `user_id` 拿到全部用户资料与**无盐 SHA-256 密码哈希**。无盐 SHA-256 对字典/彩虹表几乎无抵抗力，等同密码泄露。
- **修复建议**：
  1. 该端点补 `Depends(get_login_user)`，并只允许查询自己（管理员除外）；
  2. `UserTable.hide_fields = ["user_password"]`，从序列化层根除泄露；
  3. 密码改用 `argon2` / `bcrypt` 等带盐慢哈希。

### P0-2 · 未鉴权即可改写任意用户资料

- **位置**：`src/backend/agentchat/api/v1/user.py:80` `PUT /api/v1/user/update`
- **成因**：同样未挂鉴权依赖，`user_id` 来自请求体，服务层不做归属校验。
- **复现**：
  ```
  PUT /api/v1/user/update
  {"user_id":"user-B","user_avatar":"http://evil/x.png","user_description":"pwned"}
  → 200 且 UserService.update_user_info 被以 user-B 调用
  ```
- **证据**：`test_user_update_writable_anonymously` 通过。
- **影响**：任意人可篡改他人头像（可指向外部追踪图片）与个人简介，属未授权写操作。
- **修复建议**：补鉴权依赖；`user_id` 一律取 `login_user.user_id`，不接受客户端传入。

### P1-1 · 白名单前缀匹配越权（匿名被当作 Admin）

- **位置**：`agentchat/middleware/white_list_middleware.py:30`、`agentchat/api/services/user.py:119`
- **成因**：`WhitelistChecker.is_whitelisted` 用 `path.startswith(prefix)` 做前缀匹配。配置 `/api/v1/mcp/*` 归一化后前缀为 `/api/v1/mcp`，**同时命中** `/api/v1/mcp_server`、`/api/v1/mcp_user_config`。而白名单路径在 `get_login_user` 中直接返回 `UserPayload(user_id="1", user_name="Admin")`——**跳过 JWT 校验并授予管理员身份**。
- **复现**：
  ```
  GET /api/v1/mcp_server            # 无 Authorization
  → 进入业务逻辑，MCPService.get_all_servers(user_id="1")
  ```
- **证据**：`test_whitelisted_sibling_route_accepts_anonymous` 断言收到的 `user_id == "1"`；`test_prefix_bleeds_into_sibling_routes` 直接断言前缀串味。
- **影响范围（重要）**：生产编排挂载的是 `docker_config.prod.generated.yaml`，其 `whitelist_paths: []`，**不受影响**；受影响的是仓库自带的 `docker/docker_config.yaml` 与 `docker_config.example.yaml`（均为 `["/api/v1/mcp/*"]`），即本地与自建部署。
- **修复建议**：前缀匹配改为"路径段边界匹配"（`path == prefix or path.startswith(prefix + "/")`）；白名单语义应只做"免鉴权"，不应直接赋予 Admin 身份。

### P1-2 · 对话历史水平越权

- **位置**：`agentchat/api/v1/history.py:10` `GET /api/v1/history?dialog_id=`
- **成因**：`HistoryService.get_dialog_history(dialog_id)` 只按 `dialog_id` 查库，**从不使用 `login_user.user_id`**。同文件的 `get_short_term_messages` 却有 `db_dialog.user_id != user_id` 校验——说明作者知道该做，只是这里漏了。
- **复现**：用户 A 的 token + 用户 B 的 `dialog_id` → 200 并返回 B 的完整对话。
- **证据**：`test_history_readable_across_users` 通过。
- **影响**：任意登录用户可读取他人全部对话内容（含上传的文档摘要、工具调用结果）。
- **修复建议**：查询前 `DialogDao.select_dialog_by_id(dialog_id)` 校验 `user_id` 归属，不匹配返回 403。

### P1-3 · MCP 用户配置越权读 / 删

- **位置**：`GET /api/v1/mcp_user_config/{config_id}`、`DELETE /api/v1/mcp_user_config/delete`
- **成因**：`MCPUserConfigDao.get_mcp_user_config_from_id` / `delete_mcp_user_config` 均只按主键操作，端点也不校验归属。
- **复现**：用户 A 读取 / 删除用户 B 的 `config_id` → 均成功。
- **证据**：`test_mcp_user_config_readable_across_users`（读到 `sk-B-secret`）、`test_mcp_user_config_deletable_across_users` 通过。
- **影响**：MCP 用户配置存放的是第三方服务的鉴权头（API Key / Token），泄露即第三方账号失陷。
- **修复建议**：DAO 增加 `user_id` 过滤条件，端点显式校验归属。

### P1-4 · 两个写接口完全无鉴权

- **位置**：`agentchat/api/v1/message.py` `POST /message/like`、`POST /message/down`
- **成因**：两个端点连 `Depends(get_login_user)` 都没有，直接 `await request.json()` 取字段写库。
- **复现**：匿名 POST 任意 `user_input` / `agent_output` → 200 并落库。
- **证据**：`test_message_like_has_no_auth_dependency` 通过（断言服务层被调用）。
- **影响**：匿名可无限写入（无长度限制），造成数据污染与存储放大，可被用作低成本 DoS。
- **修复建议**：补鉴权依赖 + 字段长度上限 + 按用户限流。

### P1-5 · MCP 用户配置保存功能 100% 失败

- **位置**：`src/frontend/src/pages/mcp-server/mcp-server.vue:680`
- **成因**：前端构造
  ```ts
  const requestData: MCPUserConfigUpdateRequest = {
    mcp_server_id: configuringServer.value.mcp_server_id,
    user_config: configContent
  }
  ```
  而后端 `MCPUserConfigUpdateRequest` 要求 `server_id` + `config`。字段名全错。
- **证据**：`vue-tsc` 报错 `TS2561: 'mcp_server_id' does not exist in type 'MCPUserConfigUpdateRequest'. Did you mean to write 'server_id'?`；后端 schema 见 `agentchat/schemas/mcp_user_config.py`。
- **影响**：请求必被 422 拒绝，用户只会看到"保存失败"，功能不可用。
- **修复建议**：改为 `{ server_id, config }`，并在 CI 中接入 `vue-tsc` 阻止同类问题回归。

### P1-6 · 5 处前端调用指向不存在的后端路由

| 前端调用 | 后端实际 | 后果 |
|---|---|---|
| `/api/v1/agent/models` | `/api/v1/llm/agent/models` | Agent 编辑器模型列表 404，页面不可用 |
| `/api/v1/upload/upload` | `/api/v1/upload` | 工具头像上传 404 |
| `/api/v1/user/logout` | 不存在 | 登出仅清本地状态，**服务端会话与 Cookie 不失效** |
| `/api/v1/auth/check` | 不存在 | token 有效性检查恒失败 |
| `/api/v1/config` | 不存在（原端点已注释） | 配置页读写 404 |

- **证据**：`qa/contract_check.py` 输出 5 条 `[404]`。
- **影响**：除直接功能不可用外，`logout` 失效意味着共享设备上退出登录后 JWT 仍在有效期内可用（access token 24 小时 / refresh token 30 天，见 `utils/JWT.py:7` 与 `auth/auth_config.py:26`）。
- **修复建议**：补齐 `/user/logout`（清 Cookie + 删除 Redis 中的 `USER_CURRENT_SESSION`），修正其余路径，并把契约检查接入 CI。

### P1-7 · 知识库检索接口未鉴权

- **位置**：`agentchat/api/v1/knowledge.py:77` `POST /knowledge/retrieval`
- **成因**：端点未声明鉴权依赖，`knowledge_id` 由请求体传入，不做归属校验。
- **复现**：匿名 `{"query":"...","knowledge_id":"<kb-id>"}` → 返回该知识库的命中片段。
- **影响**：知识库内容（企业文档、私有资料）可被匿名读取。`knowledge_id` 为 UUID，需先通过其他渠道获取，故列为 P1 而非 P0。
- **修复建议**：补鉴权依赖 + `KnowledgeService.verify_user_permission`。

### P1-8 · 微信消息接口未校验签名

- **位置**：`agentchat/api/v1/wechat.py:65` `POST /wechat`
- **成因**：`GET /wechat` 调用了 `WeChatService.check_signature`，但 `POST /wechat` 直接解析 XML 并进入 Agent 处理，**完全没有验签**。
- **影响**：任意人可伪造微信 XML 消息（自填 `FromUserName`）触发 LLM 调用，造成**额度消耗与身份伪造**；同时会把回复投递到任意指定用户。
- **修复建议**：POST 同样校验 `signature/timestamp/nonce`；签名比较改用 `hmac.compare_digest`（当前 `==` 非恒定时间比较）。

### P2-1 · 业务错误一律返回 HTTP 200

- **位置**：`agentchat/api/responses/builder.py` `resp_500()`
- **成因**：返回 `UnifiedResponseModel(status_code=500)`，但 HTTP 状态码仍是 200。
- **证据**：`test_business_error_returns_http_200` 通过（HTTP 200 + body `status_code:500`）。
- **影响**：Nginx / 网关 / APM 的 5xx 告警完全失效；前端 axios 拦截器无法统一处理错误。
- **修复建议**：HTTP 状态码与业务码对齐，或至少在网关侧按 body 做告警。

### P2-2 · 前端类型检查不通过（24 个错误 / 7 个文件）

```
src/pages/tool/tool.vue                        12
src/pages/login/login.vue                       3
src/pages/agent/agent-editor.vue                3
src/components/dialog/create_agent/create_agent.vue  3
src/utils/request.ts                            1
src/pages/mcp-server/mcp-server.vue              1   ← 即 P1-5
src/components/dialog/create_agent/AgentFormDialog.vue 1
```
- **影响**：`npm run lint` 形同虚设，P1-5 这类契约错误无法被拦下。
- **修复建议**：先清零存量错误，再在 CI 中把 `vue-tsc --noEmit` 设为必过项。

### P2-3 · `/minio/` 等 location 丢失安全响应头（静态推断）

- **位置**：`docker/nginx/nginx.prod.conf:147-166`
- **成因**：`server` 级声明了 `X-Content-Type-Options` / `X-Frame-Options` / `Referrer-Policy`，但 `location /minio/` 内又写了 `add_header Cache-Control`。按 nginx 规则，**当前层级一旦出现 `add_header`，上一层的 `add_header` 全部不再继承**。`/assets/`、`=/index.html` 同理。
- **影响**：`/minio/*` 与静态资源响应缺少 `nosniff` 等头；结合"上传接口无扩展名白名单"，同源上传 `.html` 可构成存储型 XSS。
- **状态**：本机 Docker 未运行，未能 `docker run nginx -t` 实测，**标注为待验证**。修复方式是在各 location 内显式重复声明，或改用 `include` 片段统一注入。

### P2-4 · 上传接口无类型与大小约束

- **位置**：`agentchat/api/v1/upload.py`
- **成因**：`await file.read()` 一次性读入内存，无大小上限（仅 nginx 侧 200M）、无扩展名/MIME 白名单，对象键由 `filename` 直接拼接。
- **影响**：并发大文件上传可打满内存；`.html` 上传可形成同源 XSS（见 P2-3）。
- **修复建议**：流式落盘 + 大小上限 + 扩展名白名单 + 强制 `Content-Disposition: attachment`。

### P2-5 · SSE 每帧携带全量累计文本

- **位置**：`agentchat/core/agents/general_agent.py:444`、`api/v1/completion.py:59`
- **成因**：每个 `response_chunk` 都带 `accumulated`（截至当前的全文），而前端只消费 `chunk`（`grep accumulated` 在 `src/` 下无命中）。
- **影响**：总传输量约为 `L²/(2·avg_chunk)`，一篇 4000 字回答约产生 2 MB 冗余流量，移动网络下明显拖慢。
- **修复建议**：删除该字段，或改为按需订阅。

### P3 级（代码卫生 / 潜在陷阱）

| 编号 | 位置 | 问题 |
|---|---|---|
| P3-1 | `core/agents/text2sql_agent.py:22` | `pymysql.connect(**db_config)` 应为 `**self.db_config`；不传参构造时直接 TypeError。当前仅 `__main__` 触达，属潜伏缺陷 |
| P3-2 | `api/services/user.py:46` | `create_user` 用 MD5，而登录校验用 SHA-256，两者不一致；该方法当前无调用方，一旦启用则新建用户永远无法登录 |
| P3-3 | 5 处 | 裸 `except:`（`services/deepsearch/graph.py:115,262`、`mars/ai_news/crawl_news.py:34,80`、`mars/deepsearch.py:44`）会吞掉 `KeyboardInterrupt`/`SystemExit` |
| P3-4 | 9 处 | `except ...: pass` 静默吞异常，故障不可观测（`semantic_cache.py:122,163`、`langfuse_trace.py:141` 等） |
| P3-5 | `services/redis.py:98` | `pickle.loads` 反序列化缓存内容；Redis 一旦被写入恶意载荷即可 RCE |

---

## 四、已验证正常的部分

避免只报问题，以下项经实测确认无缺陷：

| 项 | 结果 |
|---|---|
| 后端语法 | 350 个 `.py` 文件 `ast.parse` 全部通过，0 语法错误 |
| 既有单测 | 87 项全部通过（语义缓存 22 / 服务 15 / Langfuse 11 / 接线 7 / Harness 28 / fakeredis E2E 4） |
| JWT 伪造签名 | 拒绝（401） |
| JWT 过期令牌 | 拒绝（401） |
| `alg=none` 攻击 | 拒绝（401）——`jwt.decode` 显式限定 `algorithms=["HS256"]`，未落入算法混淆 |
| refresh token 冒充 access | 拒绝（401） |
| 非白名单路由匿名访问 | 拒绝（401）——基线对照成立，说明问题出在个别接口遗漏而非全局失效 |
| 前端生产构建 | 通过（24.79s），产物可生成 |
| 语义缓存正确性 | 阈值边界、TTL 过期与续期、零向量不产生 NaN、kb_version/prompt_hash 门控均有用例覆盖 |
| 兜底文案不写缓存 | 有专门用例守护（避免错误答案被长期复用） |

---

## 五、建议的 CI 门禁

当前 CI 只做"不需要真实服务的静态校验"，建议追加三项（均可在无数据库环境运行，总计约 20 秒）：

```yaml
- name: 接口鉴权回归
  run: python3 qa/test_api_authz.py          # 17 用例，无需 MySQL/Redis

- name: 前后端契约
  run: python3 qa/contract_check.py           # 发现前端调用 404 即失败

- name: 前端类型门禁
  run: cd src/frontend && npm ci && npx vue-tsc --noEmit
```

---

## 六、局限与未覆盖

1. **未启动真实依赖服务**：MySQL / Redis / Milvus / ES / MinIO / DashScope 均未连通，因此未覆盖真实 SQL 执行、RAG 检索质量、LLM 输出质量、并发与连接池表现。
2. **未做 LLM 效果评测**：`scripts/eval_harness.py` 的黄金集当前是 24 条 seed 占位样本，指标不具参考价值。
3. **未做性能压测**：SSE 长连接并发、大文件上传内存占用、语义缓存命中率均需真实环境。
4. **P2-3 未经实测**：本机 Docker 未运行，nginx 响应头继承问题为依据配置规则的静态推断。
5. **未覆盖 MCP / Skill 子代理的提示注入面**：这部分需要真实 LLM 才能构造有效攻击载荷。

---

## 七、复跑方式

```bash
# 依赖：仓库自带 src/backend/.venv（或 pip install -r src/backend/requirements.txt）
PY=src/backend/.venv/Scripts/python.exe        # Linux/macOS 用 src/backend/.venv/bin/python

$PY qa/static_scan.py        # 静态缺陷扫描 → qa/static_findings.json
$PY qa/contract_check.py     # 前后端契约比对
$PY qa/test_api_authz.py -v  # 接口鉴权测试（17 用例）
```
