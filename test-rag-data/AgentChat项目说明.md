# AgentChat 项目说明

## 一、项目简介

AgentChat 是一个开源的智能体对话平台，支持多模型接入、知识库检索增强（RAG）、工具调用、记忆系统和技能扩展。用户可以创建自定义 Agent，绑定知识库，配置工具，然后通过对话完成各类任务。

## 二、核心功能

### 2.1 多模型管理
AgentChat 支持接入多种大语言模型，包括但不限于：
- 阿里云通义千问（qwen-plus、qwen-vl-plus 等）
- DeepSeek（deepseek-reasoner）
- 其他兼容 OpenAI 接口的模型

模型配置在 config.yaml 的 multi_models 段中统一管理，分为对话模型（conversation_model）、工具调用模型（tool_call_model）、推理模型（reasoning_model）、文生图模型（text2image）和视觉模型（qwen_vl）。

### 2.2 知识库与 RAG 检索
知识库是 AgentChat 实现 RAG 的核心载体。其工作流程如下：

1. 用户在「知识库」页面创建知识库，并上传文档文件。
2. 系统对上传文件进行解析（支持 PDF、Word、Excel、PPT、Markdown、图片、纯文本等格式）。
3. 解析后的文本被切分为块（chunk_size 默认 500，overlap_size 默认 100）。
4. 使用 embedding 模型（text-embedding-v4）将文本块向量化，存入 ChromaDB 向量数据库。
5. 当用户与绑定了该知识库的 Agent 对话时，LLM 会自主判断是否需要检索知识库。
6. 如果需要检索，系统会对用户问题进行查询重写（query_rewrite），然后在向量库中召回相关文档。
7. 召回结果经过 rerank 模型（gte-rerank-v2）重排序，按 min_score（默认 0.2）阈值过滤。
8. 过滤后的文档内容拼接成上下文，返回给 LLM 作为参考。

**重要提示**：AgentChat 的 RAG 是"工具调用"模式，而非"自动注入"模式。检索被封装成一个名为 retrival_knowledge 的工具，由 LLM 自主决定是否调用。这意味着模型不一定每次回答都会检索知识库。

### 2.3 工具调用
AgentChat 支持为 Agent 配置多种工具：
- 检索知识库（retrival_knowledge）：RAG 检索工具
- 文生图：调用通义万相模型生成图片
- 技能工具：Agent 可挂载自定义技能
- 天气查询、网页搜索等内置工具

工具调用流程：LLM 生成工具调用参数 → 系统执行工具 → 将结果返回给 LLM → LLM 基于结果生成最终回答。

### 2.4 记忆系统
AgentChat 提供对话记忆能力，支持在对话过程中记住用户的偏好和历史上下文，使对话更加连贯和个性化。

## 三、技术架构

### 3.1 后端技术栈
- Web 框架：FastAPI（异步）
- AI 框架：LangChain 1.0 + LangGraph
- ORM：SQLModel（基于 SQLAlchemy）
- 数据库：MySQL 8.0
- 缓存：Redis 7.0
- 向量数据库：ChromaDB（默认）/ Milvus（可选）
- 对象存储：MinIO
- 包管理：uv

### 3.2 前端技术栈
- 框架：Vue 3.4 + TypeScript
- UI 库：Element Plus
- 构建工具：Vite 5
- 状态管理：Pinia
- 路由：Vue Router 4

### 3.3 部署方式
AgentChat 提供 Docker Compose 部署方案，包含开发栈和生产栈两套配置：
- 开发栈：docker-compose.yml，配置文件 COPY 进镜像
- 生产栈：docker-compose.prod.yml，配置文件以只读卷挂载

## 四、RAG 配置说明

### 4.1 知识库绑定
知识库的绑定入口在 Agent 编辑器（agent-editor.vue）中，而非对话页。用户需要：
1. 打开 Agent 编辑页
2. 展开配置区的「知识库」折叠项
3. 在多选下拉框中勾选要绑定的知识库
4. 保存 Agent

绑定后，agent 的 knowledge_ids 字段会存储所选知识库的 ID 列表。

### 4.2 RAG 检索参数
在 config.yaml 的 rag 段中可配置以下参数：
- top_k：召回文档数量，默认 5
- min_score：相似度阈值，默认 0.2，低于此分的文档被过滤
- chunk_size：文本切分块大小，默认 500
- overlap_size：切分重叠量，默认 100
- enable_elasticsearch：是否启用 ES 关键词混合检索，默认 False
- vector_db.mode：向量库类型，默认 chroma

### 4.3 依赖的三个模型
RAG 功能依赖以下三个模型协同工作：
- embedding 模型（text-embedding-v4）：负责将文本向量化，用于入库和检索
- rerank 模型（gte-rerank-v2）：负责对召回结果重排序，提升相关性
- conversation_model（qwen-plus）：负责判断是否调用检索工具，以及最终回答生成

如果 rerank 模型的 API Key 无效，检索链会在重排序环节中断，导致 RAG 功能完全不可用。

## 五、常见问题

### 5.1 为什么对话没有 RAG 效果？
可能的原因：
1. 知识库中没有上传文件，或文件未完成向量化
2. Agent 没有绑定知识库（knowledge_ids 为空）
3. rerank 模型的 API Key 配置错误
4. 容器无法访问模型服务（网络问题）

### 5.2 文件上传后状态一直是「进行中」？
检查以下几点：
- embedding 模型 API Key 是否有效
- 容器是否能访问 DashScope 服务
- 文件格式是否被支持

### 5.3 检索结果不准确？
可调整的参数：
- 降低 min_score 阈值（如从 0.2 降到 0.1）
- 增加 top_k 数量（如从 5 增到 10）
- 调整 chunk_size 和 overlap_size
