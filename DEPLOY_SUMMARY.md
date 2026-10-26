# AgentChat 部署问题排查总结

> 场景：将 AgentChat 部署到阿里云轻量应用服务器（2 核 2G，公网 IP `101.132.146.85`），作为简历作品展示。
> 访问方式：明文 HTTP（`http://101.132.146.85`），未绑域名、未上 HTTPS。
> 排查时间：2026-10-17 ~ 2026-10-18

---

## 一、现象

| 功能 | 状态 |
|------|------|
| 工作台「会话」（主对话） | ✅ 正常能聊 |
| 工作台「日常模式」发送消息 | ❌ 没反应 |
| 「新建会话」 | ❌ 失败 |
| 「任务图」 | ❌ 进不去 |

浏览器 Console 固定报错：

```
TypeError: crypto.randomUUID is not a function
```

---

## 二、根因

`crypto.randomUUID()` **只在 secure context（HTTPS 或 localhost）可用**；明文 `http://` 下该 API 为 `undefined`。

前端在生成会话 ID 时调用了它 → 抛异常 → **请求根本没发到后端**。
（关键证据：backend 日志里全程没有 `POST /workspace/simple/chat`，说明请求在浏览器端就崩了。）

### 为什么不对称性：会话能聊、日常模式不能？

两条取值路径不同，且只有非会话页调用了 `crypto.randomUUID`：

- **会话页**：代码路径**不调用** `crypto.randomUUID` → 正常
- **日常模式 / 新建会话 / 任务图**：三处都用 `crypto.randomUUID()` 生成 `sessionId` → 崩

前端出错时的表现：`workspaceSimpleChatStreamAPI` 的 `onerror` 回调会 `messages.value.splice(...)` 删掉刚 push 的两条气泡 + 弹「对话失败，请稍后重试」→ 用户感知为「没反应」。

---

## 三、三处源码（已打永久补丁，本地已确认落盘）

路径前缀：`src/frontend/src/pages/workspace/`

- `defaultPage.vue` → `generateSessionId`：`if (crypto && typeof crypto.randomUUID === 'function') { ... } else { ... }`
- `taskGraphPage.vue`：`const genId = () => (crypto && typeof crypto.randomUUID === 'function') ? crypto.randomUUID().replace(/-/g,'') : (Date.now().toString(16)+Math.random().toString(16).slice(2)).replace(/-/g,'')`
- `workspacePage.vue`：三元兜底同上

> 这三个改动是**永久修复**，会在后续重建镜像时固化进产品。

---

## 四、线上应急 polyfill 注入（排查踩坑合集，可复用）

### 坑 1：docker compose 必须在 compose 文件目录执行
`-f docker-compose.prod.yml` 是相对路径，在 `~`（/home/admin）下跑会报：
```
compose file "/home/admin/docker-compose.prod.yml" is invalid: open ...: no such file or directory
```
✅ 正确目录：`cd /home/admin/Myagent/docker`

### 坑 2：bundle 真实路径
nginx `root` 是 `/usr/share/nginx/html`，但 bundle 在**子目录** `/usr/share/nginx/html/assets/` 下（共 17 个 `index-*.js` 分块文件）。
✅ 正确 glob：`/usr/share/nginx/html/assets/index-*.js`

### 坑 3：`!` 触发 bash history expansion
交互式 bash 双引号里的 `!` 会触发 `bash: !crypto: event not found` 并中止命令。
✅ 用 `||` 代替 `if(!x)`：

```js
crypto.randomUUID||(crypto.randomUUID=function(){return Date.now().toString(16)+Math.random().toString(16).slice(2)})
```

### 坑 4：坏 polyfill 陷阱（最阴险）
曾经误注入：
```js
if(typeof crypto.randomUUID===undefined)crypto.randomUUID=function(){...}
```
`typeof` 返回**字符串** `'undefined'`，而 `'undefined' === undefined` **永远为 false**（字符串 vs 真正的 undefined 值）→ polyfill 函数体从不执行 → 报错持续。

✅ 正确写法只做真值判断，无 `!`、无 `typeof`、无 `===undefined`：
```js
crypto.randomUUID||(crypto.randomUUID=function(){return Date.now().toString(16)+Math.random().toString(16).slice(2)})
```

### 坑 5：注入生产 bundle 的正确姿势
- 别用 `sed "1s|^.*$|polyfill|"`（**整行替换第 1 行**，会冲掉 bundle 第 1 行真实代码 → 可能白屏）。
- 用 `printf + cat` 拼接最稳，且第 1 行若已存在 polyfill（无论好坏），要用 `tail -n +2` **替换**而非 `prepend`（否则会叠加或仍是坏的）。

✅ 最终可用命令（替换坏 polyfill 为正确版）：

```bash
cd /home/admin/Myagent/docker && docker compose -f docker-compose.prod.yml exec frontend sh -c '
PF="crypto.randomUUID||(crypto.randomUUID=function(){return Date.now().toString(16)+Math.random().toString(16).slice(2)})"
for F in $(grep -rl "crypto.randomUUID" /usr/share/nginx/html/assets/index-*.js 2>/dev/null); do
  if head -n 1 "$F" | grep -q "crypto.randomUUID||"; then
    echo "OK_SKIP $F"
  elif head -n 1 "$F" | grep -q "crypto.randomUUID="; then
    { printf "%s\n" "$PF"; tail -n +2 "$F"; } > /tmp/p.js && mv /tmp/p.js "$F" && echo "REPLACED $F"
  else
    { printf "%s\n" "$PF"; cat "$F"; } > /tmp/p.js && mv /tmp/p.js "$F" && echo "PREPENDED $F"
  fi
done
echo DONE
'
```

✅ 验证第 1 行：
```bash
docker compose -f docker-compose.prod.yml exec frontend sh -c 'head -n 1 /usr/share/nginx/html/assets/index-Bd-0OD48.js'
```
应输出：
```
crypto.randomUUID||(crypto.randomUUID=function(){return Date.now().toString(16)+Math.random().toString(16).slice(2)})
```

### 坑 6：浏览器缓存
bundle 文件名 hash 不变（`index-Bd-0OD48.js`）→ 浏览器按 URL 死缓存旧文件。
✅ 验证时务必用**无痕窗口**或 `Ctrl+Shift+R` 硬刷新。
✅ 诊断技巧：报错行号从 `:1036` 变 `:1037`，即证明插入成功（原代码整体下移 1 行）。

---

## 五、当前修复状态

- 线上 `index-Bd-0OD48.js` 第 1 行曾有误注入的**坏 polyfill**（`===undefined` 版），导致一直无效。
- 已给出**修正版替换命令**（用 `tail -n +2` 丢掉坏行、写入正确 `||` 版），待执行 + 无痕窗口验证。

---

## 六、待办（永久方案 + 安全）

1. **永久方案（推荐）**：用已打补丁的 3 个 `.vue` 源文件 `npm run build` → 重建 `agentchat-prod-frontend` 镜像（约 86MB）→ `docker save` → OSS → 服务器 `wget` + `docker load` → `restart frontend`。把补丁真正固化进产品，不再靠运行时打补丁。
2. **安全**：百炼控制台删除已泄露旧 Key（`sk-ws-H.PHEPRYE...` 等 3 条）；OSS 公共读 Bucket 里的 `agentchat-images.tar` 改「私有」或删除。
3. **悬置**：「探索」页按钮点击 URL 不变（疑似线上镜像基于旧代码，等重建镜像后验证）。
4. **可选优化**：2G 内存加 swap / 关 minio；**绑域名 + HTTPS**（HTTPS 下 `crypto.randomUUID` 原生可用，连 polyfill 都不需要）。

---

## 七、一句话结论

> 明文 HTTP 下 `crypto.randomUUID` 不可用，日常模式/新建会话/任务图三处生成会话 ID 时崩溃；会话页不调用故正常。临时救法是在前端 bundle 顶部注入真值判定的 polyfill，根治是重建镜像固化源码补丁（或上 HTTPS）。
