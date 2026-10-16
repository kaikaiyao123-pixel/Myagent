# AgentChat 技术问答手册

## Q1: AgentChat 支持哪些文件格式上传到知识库？

AgentChat 的知识库支持以下文件格式的解析与向量化：

| 格式 | 扩展名 | 解析模块 |
|------|--------|----------|
| PDF | .pdf | doc_parser/pdf.py |
| Word | .docx | doc_parser/docx.py |
| Excel | .xlsx, .xls | doc_parser/excel.py |
| PPT | .pptx | doc_parser/pptx.py |
| Markdown | .md | doc_parser/markdown.py |
| 纯文本 | .txt | doc_parser/text.py |
| 图片 | .jpg, .png | doc_parser/image.py（OCR） |
| 其他 | .csv, .json 等 | doc_parser/other_file.py |

每个格式有独立的解析器，位于 services/rag/doc_parser/ 目录下。解析器负责将文件内容提取为纯文本，再交给切分和向量化流程。

## Q2: 如何创建一个带 RAG 能力的 Agent？

完整步骤如下：

1. 进入「知识库」页面，点击新建，输入知识库名称（如"产品文档库"）。
2. 进入知识库详情页，点击上传文件，选择本地文档。
3. 等待文件状态从"🚀 进行中"变为"✅ 完成"——此时文件已完成解析、切分和向量化。
4. 进入「Agent 管理」页面，点击新建 Agent 或编辑已有 Agent。
5. 在 Agent 编辑页的配置区，找到「知识库」折叠项并展开。
6. 在多选下拉框中搜索并勾选刚才创建的知识库。
7. 配置 Agent 的其他参数（名称、头像、模型等），然后保存。
8. 进入对话页，选择这个 Agent，新建会话开始对话。
9. 当你提问涉及知识库内容时，模型会自动调用「检索知识库」工具。

## Q3: RAG 检索的完整链路是什么？

当 LLM 决定调用检索工具时，后端执行以下流程：

1. **查询重写（query_rewrite）**：使用 conversation_model 对用户原始问题进行改写，生成多个语义等价的查询，提高召回率。
2. **文档检索（mix_retrival_documents）**：用重写后的查询在向量库中做语义检索，召回 top_k 个文档。如果启用了 Elasticsearch，还会同时做关键词检索并合并结果。
3. **重排序（rerank_documents）**：用 rerank 模型对召回的文档按与原始问题的相关性重新打分排序。
4. **阈值过滤**：按 min_score 过滤掉得分过低的文档。
5. **结果拼接**：将过滤后的文档内容拼接成一段文本，返回给 LLM。

## Q4: 配置文件 config.yaml 中哪些参数影响 RAG？

```yaml
rag:
  retrival:
    top_k: 5          # 召回文档数量
    min_score: 0.2    # 相似度阈值
  split:
    chunk_size: 500     # 文本块大小
    overlap_size: 100   # 块间重叠
  enable_elasticsearch: False  # 是否启用 ES 混合检索
  vector_db:
    mode: "chroma"     # 向量库类型：chroma / milvus / milvus_lite

multi_models:
  embedding:
    api_key: "你的DashScope密钥"
    model_name: "text-embedding-v4"
  rerank:
    api_key: "你的DashScope密钥"
    model_name: "gte-rerank-v2"
```

## Q5: 什么是"工具调用模式"的 RAG？

AgentChat 采用"工具调用"（Tool Calling）而非"自动注入"（Auto Injection）的 RAG 架构：

- **自动注入**：每次用户提问前，系统自动检索知识库，把结果塞进 system prompt 的 context 中。LLM 始终能看到检索内容。
- **工具调用**：检索被封装成一个工具函数，LLM 根据问题自行判断是否需要调用。如果问题与知识库无关，LLM 不会检索，直接回答。

工具调用的优势：
- 节省 token 消耗（不需要每次都塞检索结果）
- 减少无关信息干扰
- LLM 可以更灵活地决定何时需要外部知识

工具调用的劣势：
- 依赖 LLM 的判断能力，模型可能漏检
- 不是每次对话都会体现 RAG 效果

## Q6: 如何排查 RAG 不工作的问题？

按以下顺序排查：

1. **检查向量库是否有数据**：查看 ChromaDB 的 embeddings 数量是否大于 0。
2. **检查 Agent 是否绑定了知识库**：查看 agent 表的 knowledge_ids 字段是否非空。
3. **检查 rerank 模型 Key**：rerank 的 API Key 必须有效，否则检索链会在重排序环节抛异常中断。
4. **检查容器网络**：容器必须能访问 DashScope 服务，直连不通时需要配置 HTTPS_PROXY 代理。
5. **检查文件状态**：知识库中的文件状态必须为"✅ 完成"，"❌ 失败"的文件没有向量数据。
