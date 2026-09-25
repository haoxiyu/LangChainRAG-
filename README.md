# 基于 LangChain 的企业级 RAG 商品知识库问答系统

面向电商商品场景的检索增强生成(RAG)问答系统。管理员在浏览器里维护知识库,所有用户基于知识库提问,**回答会引用知识库原文片段并标注来源**,可点开核对。

- 后端:Python 3.13 + FastAPI + LangChain 1.4
- 前端:Vue 3 + Vite 6 + Element Plus
- 向量库:PostgreSQL + pgvector(HNSW 索引)
- 模型:阿里云百炼(DashScope)云端 API —— 对话 / 向量 / 重排三类模型全部走云端,免本地下载
- **不依赖 Docker**:用 `pgembed` 在项目内拉起自带 pgvector 的 PostgreSQL;用 `diskcache` 替代 Redis

---

## 一、功能一览

| 编号 | 需求 | 实现位置 |
|---|---|---|
| 1 | 浏览器端知识库管理 | 管理页:知识库增删改、文档上传/解析/分块入库、状态轮询、分块预览、重新处理、删除 |
| 2 | 问答时引用知识库并展示引用了哪些片段 | 流式回答正文里的 `[n]` 角标 + 下方引用卡片(来源文件、块序号、相关度、可展开原文),点击角标联动高亮 |
| 3 | 多用户多会话,各自独立 | 会话按 `user_id` 隔离;跨用户访问一律返回 404(不泄露存在性) |
| 4 | 会话历史持久化,不同时段登录可找回 | 会话与消息落 PostgreSQL;引用片段以**快照**存进消息,不随知识库后续变更漂移 |
| 5 | 注册 / 登录 / 修改密码 | JWT(PyJWT)+ bcrypt;改密需校验旧密码 |
| 6 | `admin/123456`,仅管理员可进知识库管理页 | 后端 `require_admin` 拦截(403),前端路由守卫 + 菜单按角色渲染 |
| 7 | 企业级性能优化 | 见 [第五节](#五性能优化需求-7) |
| 8 | 附加功能 | 会话重命名/删除/搜索/清空、停止生成、自动生成会话标题、多轮追问改写、管理仪表盘、语义缓存命中提示、限流 |

---

## 二、系统架构

```
浏览器 (Vue 3 SPA)
   │  HTTP / SSE
   ▼
FastAPI 后端
   ├─ 认证鉴权:JWT + 角色(admin / user)
   ├─ 路由层:auth · knowledge-bases · documents · conversations · chat · admin
   ├─ RAG 服务层(LangChain 编排)
   │    历史感知改写 → 语义缓存 → 混合检索 → RRF 融合 → 重排序 → 流式生成 → 引用回填
   ├─ 数据层:SQLAlchemy(async)+ pgvector + diskcache
   └─ 文档摄取:上传 → 解析 → 分块 → 批量 embedding → 写 chunks
        │
        ▼
   PostgreSQL + pgvector(pgembed 内嵌,免安装免 Docker)
```

### RAG 流程(核心链路)

1. **历史感知改写** —— 取最近 4 轮对话,把"它""这款""还有别的吗"这类指代补全成可独立检索的问题(无历史时跳过,不额外调用模型)。
2. **语义缓存查询** —— 用改写后问题的向量在全局语义缓存里找相似问题(余弦 ≥ 0.95),命中则直接回放答案与引用,耗时降到毫秒级。
3. **混合检索** —— 稠密路走 pgvector HNSW 取 top-20;稀疏路走 jieba 分词 + BM25 取 top-20。
4. **RRF 融合** —— Reciprocal Rank Fusion(k=60)合并两路排名,兼顾语义召回与关键词精确命中。
5. **重排序** —— 百炼 rerank 对融合候选精排,取 top-5 作为最终上下文。
6. **流式生成** —— Qwen 逐 token 输出,系统提示要求每个引用知识的句子必须标注 `[n]`。
7. **引用回填** —— `[n]` 映射回真实 chunk,随回答一起落库并推给前端渲染。

> 检索为空时不会硬答:改用无上下文提示词,明确告知未找到并建议联系人工客服。

### 分块策略

分块决定了「检索到的那一小段能不能脱离原文被读懂」,是 RAG 里最容易被忽视的一环。

1. **先按小节切** —— Markdown 认 `#` 井号标题,纯文本认中文资料里常见的编号标题
   (`一、` / `1.` / `(一)` / `第三章`)。用户上传的商品资料常是 `.txt`,若只按段落机械
   地切,会出现「洗涤:水温≤30℃…」这种没了主语的块,检索命中后连它属于哪种材质都看不出。
2. **挂标题路径** —— 每块前面加
   `【商品知识库 > 一、春季服装 > 1. 纯棉材质（春季衬衫、T恤、休闲裤）】`。
   标题行本身不再留在正文里,避免同一句话被两份文本重复计分。
   **父级标题只取主干、丢掉末尾括号**:`一、春季服装（纯棉、薄牛仔、针织棉、轻薄化纤）`
   这种括号列的是同级小节,整条复制进每一块等于给兄弟块都塞上彼此的名字。实测原文档
   里「雪纺怎么洗」让夏季四块全部命中「雪纺」,BM25 无从分辨,正确答案反被「棉麻」块
   压到第二(4.374 vs 2.764);父级只留主干后变成 3.416 vs 2.432,方向正确。叶子标题的
   括号则保留 —— 它描述的是它自己,「纯棉材质（春季衬衫）」里的品名是正文里唯一
   出现「衬衫」的地方,砍掉就再也检索不到「衬衫怎么洗」。
3. **再按中文标点递归切** —— 段落 → 句末标点(全角 `。！？；，`)→ 英文句末(`. ` 带空格,
   避免切开 `3.14` / `6.7 英寸`)→ 空格 → 字符兜底。
4. **表格每块都补表头** —— 长表格被切开后,后半截只剩 `| 16GB+512GB | 4799 元 | 4499 元 |`
   这种裸数据,列名没了就分不清哪个是指导价。补上表头后每块都能独立解读。
5. **预算按最终长度算** —— 前缀和表头会跟着每块走,先扣掉再定 `chunk_size`,
   否则每块都会超长(曾经出现过 `chunk_size=500` 而实际 524 的情况)。

以上不变量都由 `test_chunking.py` 的用例锁住,改分块器时先看那组测试。

---

## 三、目录结构

```
LangChainRAG/
├─ backend/
│  ├─ requirements.txt
│  ├─ .env.example                # 配置模板(真实 .env 不入库)
│  ├─ run.py                      # 启动脚本(切好事件循环策略再拉起 uvicorn)
│  ├─ app/
│  │  ├─ main.py                  # FastAPI 入口、lifespan、静态资源托管
│  │  ├─ config.py                # pydantic-settings 配置
│  │  ├─ compat.py                # Windows 事件循环兼容(见第八节)
│  │  ├─ db.py                    # 内嵌 PostgreSQL 引导 + 异步引擎
│  │  ├─ models/                  # ORM:用户 / 知识库 / 文档 / 分块 / 会话 / 消息
│  │  ├─ schemas/                 # 请求响应模型
│  │  ├─ api/                     # auth · knowledge_bases · documents · conversations · chat · admin
│  │  ├─ core/                    # JWT、密码哈希、依赖注入(当前用户 / 管理员校验)
│  │  └─ services/
│  │       ├─ ingestion.py        # 解析 → 分块 → 批量 embedding → 入库
│  │       ├─ retriever.py        # 稠密 + BM25 + RRF + 重排
│  │       ├─ rag_chain.py        # LangChain 链路编排
│  │       ├─ llm.py              # 百炼 Qwen(OpenAI 兼容,流式)
│  │       ├─ embedding.py        # text-embedding(带 diskcache)
│  │       ├─ reranker.py         # 原生 rerank 接口 → LangChain BaseDocumentCompressor
│  │       ├─ cache.py            # diskcache:embedding / 语义 / 限流
│  │       └─ parsers/            # pdf · docx · xlsx · csv · txt · md
│  └─ scripts/                    # 建库、联调测试、清理脚本
├─ frontend/
│  └─ src/
│     ├─ views/                   # 登录 / 注册 / 聊天 / 会话 / 个人设置 / 管理页
│     ├─ components/              # ChatMessage、CitationList
│     ├─ stores/                  # Pinia:auth
│     ├─ router/                  # 路由 + 登录/角色守卫
│     ├─ api/                     # axios 封装 + SSE(fetch 流式)
│     └─ utils/markdown.js        # Markdown 渲染 + `[n]` 角标注入
├─ sample_data/                   # 示例商品知识库文档
└─ uploads/                       # 上传的原始文件(随文档/知识库删除而清理)
```

---

## 四、快速开始

### 一键启动(推荐给不熟悉命令行的使用者)

依赖装好之后(见下面「环境要求」与「1. 后端」),以后每次启动只需**双击仓库根目录的 `启动系统.bat`**:

- 它会拉起数据库与后端,等 `/api/health` 真正返回 200 后再**自动打开浏览器**,避免"打开太快看到无法访问此网站";
- 出现「启动完成」字样即可登录使用,**关掉那个黑窗口就等于关闭系统**(或按 Ctrl+C);
- 若提示"系统已经在运行了",说明已有一个实例在跑,脚本只打开浏览器、不会重复启动。

> 为什么 `.bat` 里一个中文都没有?cmd.exe 按 OEM 代码页(中文 Windows 是 cp936)解析批处理文件,
> 里面写 UTF-8 中文会被误码,**甚至吞掉行尾换行导致命令粘连**。所以批处理刻意只含 ASCII,
> 所有中文提示交给 `backend/scripts/launch.py` 用 Python 打印(编码由 Python 自己保证)。

### 环境要求

- Python 3.13
- Node.js 18+(本项目用 24)
- 可访问阿里云百炼的网络
- **无需安装 PostgreSQL,也无需 Docker**

### 1. 后端

```bash
cd backend
python -m venv ../.venv                       # 仓库根目录下的 .venv
../.venv/Scripts/python.exe -m pip install -r requirements.txt

cp .env.example .env                          # 然后填入 DASHSCOPE_API_KEY
```

`.env` 关键项:

```ini
DASHSCOPE_API_KEY=sk-...                      # 必填,百炼控制台获取
CHAT_MODEL=qwen-plus
EMBEDDING_MODEL=qwen3.7-text-embedding        # 注意:不要照抄官方文档的 text-embedding-v3
RERANK_MODEL=qwen3.7-text-rerank              # 该账号下实测可用的模型名与文档不一致
DATABASE_URL=                                 # 留空即自动拉起内嵌 PostgreSQL
SECRET_KEY=<随机字符串>                        # 生产环境务必替换
```

启动:

```bash
../.venv/Scripts/python.exe run.py            # http://127.0.0.1:8000
```

首次启动会自动:拉起内嵌 PostgreSQL → 建库 → 启用 pgvector 扩展 → 建表 → 写入种子账号 `admin/123456`。

### 2. 前端

```bash
cd frontend
npm install
npm run dev                                   # 开发:http://localhost:5173(已配 /api 代理)
npm run build                                 # 生产:产物进 dist/,由后端直接托管
```

构建后直接访问 `http://127.0.0.1:8000` 即可使用。

### 3. 初始化示例知识库(可选)

用 `admin/123456` 登录 → 知识库管理 → 新建知识库 → 上传 `sample_data/星辰X1智能手机_商品知识库.md`,等待状态变为「就绪」后即可问答。

---

## 五、性能优化(需求 7)

| 优化项 | 做法 | 效果 |
|---|---|---|
| 全链路异步 | async FastAPI + async SQLAlchemy + LangChain `astream` | 不阻塞事件循环,单 worker 支撑并发 |
| 流式输出 | SSE 逐 token 下发,`references` 事件先于正文 | 首字延迟大幅降低,来源先渲染 |
| 混合检索 | pgvector 稠密 + jieba/BM25 稀疏 + RRF 融合 | 兼顾语义泛化与型号/数字等精确命中 |
| 重排序 | 百炼 rerank 精排后取 top-5 | 上下文更干净,降低幻觉 |
| 语义缓存 | diskcache 存"问题向量 → 答案+引用",阈值 0.95 | 重复提问从数秒降到**毫秒级** |
| Embedding 缓存 | 同文本不重复调用云端接口 | 省费用、降延迟 |
| 批量 embedding | 摄取时按批编码,不逐条请求 | 文档入库更快 |
| 连接池 | SQLAlchemy 连接池 + `pool_pre_ping` | 连接复用,避免失效连接 |
| 索引优化 | 向量列建 HNSW(cosine),外键与排序字段建 B-tree,组合索引覆盖会话列表 | 检索走索引而非全表扫 |
| 分页 | 会话列表分页(带真实 total)、消息游标翻页 | 避免全量加载 |
| 限流 | 按用户固定窗口分桶计数(diskcache),默认 30 次/分 | 防滥用 |
| 前端拆包 | vue / element / markdown 分包 + 视图懒加载 | 首屏只加载必需 chunk |
| 并发检索 | 稠密与稀疏两路各开独立会话并发执行 | 检索耗时取较慢一路而非相加 |

缓存一致性:文档入库、重新处理、删除文档、删除知识库时都会主动失效该知识库的语义缓存与 BM25 索引。

---

## 六、接口一览(26 个)

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/auth/register` | 注册(角色固定为 user) |
| POST | `/api/auth/login` | 登录获取令牌 |
| GET | `/api/auth/me` | 当前登录用户 |
| POST | `/api/auth/change-password` | 修改密码 |
| GET | `/api/health` | 健康检查(数据库 / pgvector / 模型配置) |
| GET | `/api/knowledge-bases` | 知识库列表(所有登录用户;问答不再选库,仅用于展示已收录范围) |
| POST/PATCH/DELETE | `/api/knowledge-bases[/{id}]` | 知识库增改删(**仅管理员**) |
| GET/POST | `/api/knowledge-bases/{id}/documents` | 文档列表 / 上传(**仅管理员**) |
| DELETE | `/api/knowledge-bases/{id}/documents/{doc_id}` | 删除文档(**仅管理员**) |
| POST | `/api/knowledge-bases/{id}/documents/{doc_id}/reprocess` | 重新解析(**仅管理员**) |
| GET | `/api/knowledge-bases/{id}/documents/{doc_id}/chunks` | 分块预览(**仅管理员**) |
| POST | `/api/chat/stream` | **流式问答(SSE,核心接口)** |
| POST | `/api/chat/stop/{conv_id}` | 停止生成 |
| GET/POST/DELETE | `/api/conversations` | 会话列表 / 新建 / 清空 |
| GET/PATCH/DELETE | `/api/conversations/{id}` | 会话详情 / 重命名 / 删除 |
| GET | `/api/conversations/{id}/messages` | 消息游标分页 |
| GET | `/api/admin/stats` | 仪表盘统计(**仅管理员**) |
| GET | `/api/admin/users` | 用户列表(**仅管理员**) |
| GET | `/api/admin/upload-limits` | 上传支持格式与大小上限(**仅管理员**,前端预检读这份配置) |

### SSE 事件协议

`POST /api/chat/stream` 返回 `data: {json}\n\n` 序列,事件类型:

| 事件 | 含义 |
|---|---|
| `conversation` | 会话 id(新建会话时前端据此更新地址栏),**总是第一个** |
| `status` | 阶段提示(理解问题 / 检索 / 生成) |
| `rewrite` | 历史感知改写后的问题(仅发生改写时) |
| `references` | 引用片段列表,**先于 token 发出**,便于先渲染来源 |
| `token` | 增量正文 |
| `title` | 首轮问答后自动生成的会话标题 |
| `done` | 结束,含耗时、token 用量、是否命中缓存,**总是最后一个** |
| `error` | 出错 |

---

## 七、测试与验证

分两层:**单元测试**离线跑,不连库、不调云端接口,任何环境下都能复现;
**联调脚本**验证真实链路,需要后端已启动。

### 单元测试(72 项,无需前置条件)

```bash
cd backend
../.venv/Scripts/python.exe -m pytest          # 约 3 秒
```

| 用例文件 | 覆盖内容 |
|---|---|
| `test_security.py` | bcrypt 加盐与 72 字节上限、损坏哈希不抛异常、JWT 签发/篡改/过期/换密钥伪造 |
| `test_cache.py` | embedding 缓存命名空间、语义缓存阈值与知识库隔离、零向量不崩、索引条数上限、限流失效与放行、缓存关闭时安全降级 |
| `test_retrieval.py` | RRF 公式与手工计算一致、两路都召回者排前、权重、空输入;jieba 分词;BM25 过滤 0 分;索引失效幂等 |
| `test_chunking.py` | 短块丢弃、Markdown/纯文本分块带标题路径、父级括号枚举不复制进每块、叶子括号保留、全角标点切点不劈词、`6.7 英寸` 不误判成标题、表格每块补表头、长度不超过 `chunk_size`、正文不丢失 |
| `test_parsers.py` | GBK/UTF-8-BOM 编码兜底、CSV 空行清理、docx/xlsx **表格**提取、不支持格式报错 |
| `test_schemas.py` | `exclude_unset` 区分「没传」与「传 null」、旧的 `knowledge_base_id` 被安全忽略、密码/问题长度校验 |

### 联调与端到端

前置:后端已在 `127.0.0.1:8000` 运行;浏览器测试还需先 `npm run build`。

```bash
../.venv/Scripts/python.exe scripts/api_test.py        # 52 项:逐条覆盖毕设需求
../.venv/Scripts/python.exe scripts/e2e_check.py       # 57 项:前端依赖的接口契约
../.venv/Scripts/python.exe scripts/browser_check.py   # 26 项:真实浏览器(CDP)端到端
```

| 脚本 | 覆盖内容 |
|---|---|
| `api_test.py` | 注册/登录/改密、知识库权限隔离、文档上传与解析、问答引用、多会话、历史找回、缓存命中、限流 |
| `e2e_check.py` | SSE 事件顺序(`conversation` 开头、`references` 先于 `token`、`done` 收尾)、引用字段完整性、回答里 `[n]` 与引用列表一致、多轮改写、上传限制下发与下发内容、删除知识库时清理上传文件与全局索引、测试会话自行清理 |
| `browser_check.py` | 用 Chrome DevTools Protocol 驱动无头 Edge/Chrome:页面真实挂载、流式渲染、`[n]` 角标可点击并联动高亮、引用原文可展开、会话条目的三个点排版与点击、内容与落库一致 |

> 两个联调脚本都会自己删掉本次创建的知识库、文档、上传文件与会话;唯一残留是临时账号
> (会话已随账号 CASCADE 删除),所以跑完只要执行一次 `clean_test_data.py` 即可回到干净状态。

联调脚本会产生临时账号与会话,收尾:

```bash
../.venv/Scripts/python.exe scripts/clean_test_data.py              # 只清测试账号
../.venv/Scripts/python.exe scripts/clean_test_data.py --conversations  # 连 admin 的会话一起清
```

---

## 八、注意事项与已知限制

**Windows 必须用 SelectorEventLoop**
asyncio 在 Windows 默认用 `ProactorEventLoop`,psycopg 异步模式不支持它。因此**不要直接用 `uvicorn app.main:app` 启动**,请用 `python run.py`(它会在创建事件循环前切好策略)。自己写脚本调用数据库时,也要在 `asyncio.run()` **之前**调用 `setup_event_loop_policy()`。

**内嵌 PostgreSQL 的端口是随机的**
`pgembed` 每次启动分配的端口不固定,所以连接串必须通过 `server.get_uri()` 获取,不能硬编码。数据目录在 `.pgdata/`,删掉它等于重置数据库。

**非正常关机后第一次启动可能"假失败"**
若上次是强制关机 / 断电 / 蓝屏,PostgreSQL 下次启动要先做**崩溃恢复**。而 pgembed 给 `pg_ctl` 的等待超时写死为 10 秒
(`pgembed/postgres_server.py:231` 的 `timeout=10`),恢复没跑完就会抛出:

```
ERROR   pgembed | Timeout starting server.
subprocess.TimeoutExpired: ... pg_ctl.exe ... timed out after 10 seconds
```

**这是误判 —— 数据库往往已经恢复成功并在正常监听。** 处理办法:等 5~10 秒,**把启动命令原样再跑一次**即可,
第二次会命中"已有实例在运行"分支直接复用端口(日志出现 `a postgres server is already running` 后紧接着
`Now asserting server is running`)。判断数据库是否真的活着:

```bash
netstat -ano | findstr LISTENING | findstr :<端口>   # 端口见 .pgdata/postmaster.pid 第 4 行
```

**不要**因为这条报错就去删 `.pgdata/` 或重装依赖 —— 那会真的丢掉所有数据。

**重启后端要杀掉整棵进程树**
`run.py` 开启了热重载,进程结构是 reloader → 子进程。只杀最外层 PID 会留下孤儿进程继续占着 8000 端口,表现为"改了代码却没生效"。建议按命令行特征(`run.py` / `multiprocessing.spawn`)整批结束。

**模型名以实测为准**
百炼账号下可用的模型名与官方文档不完全一致(如 embedding / rerank 的版本号),本项目以 `.env` 中实测可用的为准。**rerank 没有 OpenAI 兼容端点**(兼容模式返回 404),因此 `reranker.py` 直接调原生接口并封装成 LangChain 的 `BaseDocumentCompressor`。

**密钥安全**
`DASHSCOPE_API_KEY` 只放 `backend/.env`,已被 `.gitignore` 覆盖;仓库里只提交 `.env.example` 占位。答辩演示后建议轮换密钥。

**为什么稀疏检索不用 PostgreSQL 全文索引**
PG 原生全文检索对中文支持很差(需要额外的 zhparser 扩展),分词结果不可用。因此稀疏路改用 jieba 分词 + `rank_bm25` 在应用层构建索引,只对向量列建 HNSW。代价是 BM25 索引在内存中维护:**进程重启后首次检索时按需重建**,文档增删时主动失效该知识库的索引。

**建表方式(有意保留 `create_all`)**
当前用 SQLAlchemy `create_all` 建表并在启动时幂等补建 HNSW 索引,没有引入 Alembic。
这是一个**权衡后的选择**:本项目是单实例、单数据集的毕业设计,`create_all` 足以保证
「克隆仓库 → 装依赖 → 启动」一次成功,而 Alembic 需要额外维护 baseline 迁移与异步
`env.py`,收益主要在多人协作和生产环境的灰度升级上。若后续要上生产或改表结构,
应补上迁移能力 —— 届时先备份 `.pgdata/` 再切换。

**其他限制**
- 单 worker 部署,未做多进程水平扩展
- 引用的相关度分数来自 rerank,分数区间随模型变化,仅作参考

---

## 九、安全管理要点

- 所有会话/消息查询强制带 `user_id` 过滤;访问他人资源返回 **404 而非 403**,避免泄露 id 是否存在
- 知识库管理接口全部经 `require_admin`;前端守卫只是体验优化,真正的权限在后端
- 上传文件仅取文件名并校验扩展名,防止路径穿越;文件按文档 id 命名落盘
- 密码用 bcrypt 加盐哈希存储,不存明文
