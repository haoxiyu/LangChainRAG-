---
name: unit-test
description: 为后端 Python 代码创建单元测试(pytest + anyio)、执行测试并输出测试报告。当需要给 backend/app 下的模块补测试、或改动代码后要跑单测验证时使用。
metadata:
  category: 测试
---

## 任务

为「电商商品知识库问答系统」后端(`backend/app`)编写单元测试,执行测试,并输出一份结构化的测试报告。

- **测试范围**:`app/services/`(检索、摄取、缓存、向量化、重排序、RAG 编排)、`app/core/`(鉴权与角色)、`app/api/` 里的纯函数、`app/db.py` / `app/config.py` 的纯逻辑
- **测试框架**:pytest(见下方「可用插件」——本机**没有** pytest-asyncio / pytest-mock / respx)
- **不测**:
  - 需要真实 PostgreSQL 的 SQL 查询 → 交给 `backend/scripts/e2e_check.py`
  - 需要真实百炼 API 的调用(对话/向量/重排)→ 交给 `backend/scripts/api_test.py`
  - 浏览器端交互 → 交给 `backend/scripts/browser_check.py`
- **硬性约束**:纯单元测试,**不连数据库、不联网、不调云端接口**,整套应在数秒内跑完。
  同样的约束写在 `backend/tests/conftest.py` 的模块 docstring 里,不要破坏它。

## 前置检查

### 1. 解释器

统一用项目虚拟环境,不要用全局 python(依赖都装在里面):

```bash
cd D:/LangChainRAG/backend
../.venv/Scripts/python.exe -m pytest --version
```

### 2. 可用插件(2026-09 实测)

| 能力 | 本项目的做法 | 说明 |
|---|---|---|
| 异步用例 | **anyio 自带的 pytest 插件** | 装在依赖里的 `anyio 4.15.1` 会注册 `anyio` 这个 pytest11 入口 |
| mock / 打桩 | **内置 `monkeypatch` fixture** | 没有 pytest-mock,不要写 `mocker` |
| 缓存 | **真 `AppCache(tmp_path)`** | diskcache 纯本地,可以直接用真实现 |
| HTTP 打桩 | **手写假 client + monkeypatch** | 没有 respx,不要写 `respx_mock` |
| 覆盖率 | **无 pytest-cov** | 报告用 `--junitxml` + 终端汇总,不要假装有覆盖率数字 |

**异步用例必须显式打标**:没打标的 `async def` 会被 pytest 判为
`async def functions are not natively supported` 直接失败。在模块顶部打一次即可:

```python
pytestmark = pytest.mark.anyio   # 同文件里的同步用例不受影响,可混写
```

### 3. 不要 import `app.main`

`app.main` 的 lifespan 会拉起内嵌 PostgreSQL 并建表 —— 单测一旦触发它就不再是单测。
需要打桩数据库相关函数时,**直接改 `app.db` 模块上的属性**:

```python
from app import db
monkeypatch.setattr(db, "get_sessionmaker", lambda: (lambda: _NullSession()))
```

注意 `app/services/rag_chain.py` 是在函数体内 `from ..db import get_sessionmaker`,
所以运行时取的是模块属性,这样打桩有效。

## 测试编写规范

### 目录与命名

- 测试目录:`backend/tests/`,与 `app/` 下的模块**同名**:`app/services/cache.py` → `tests/test_cache.py`
- 函数命名:`test_<函数名>_<场景>_<期望>`,如 `test_semantic_miss_below_threshold`
- 中文 docstring **必须写清「为什么测这条」**,而不是复述断言。
  现有用例的写法是:先说要守住的不变量,再写断言 —— 比如
  「跨知识库命中等于把 A 库的内容答给 B 库的问题」。
- 每个文件顶部按 `# ---------- 分类 ----------` 分段,便于按主题浏览。

### 断言与替身

- 断言用内置 `assert` + 字面量;浮点比较用 `pytest.approx`;异常用 `pytest.raises`
- 参数化用 `@pytest.mark.parametrize`
- **可变的测试数据一律做成 fixture**,不要写成模块级常量:
  `reranker._apply` 会**就地**改 `Document.metadata`,模块级共享列表会让上一个用例
  写的 `rerank_score` 漏进下一个用例的断言里(这个坑实际踩过)。
- 需要冻结/改写全局配置时:`monkeypatch.setattr(settings, "history_rewrite_turns", 0)`
- **缓存必须指向 tmp_path**,绝不能碰全局 `get_cache()`(那是项目真实的 `.cache` 目录):

```python
@pytest.fixture()
def cache(tmp_path) -> AppCache:
    return AppCache(tmp_path / "cache", enabled=True)
```

- 打桩模块级单例(它们的导入方式决定了要打在哪个模块上):

| 要替换的东西 | monkeypatch 目标 | 备注 |
|---|---|---|
| 对话模型 | `rag_chain.get_chat_model` | `llm.get_chat_model` 有 `lru_cache`,不要直接改 settings |
| 向量化 | `rag_chain.get_embeddings` / `embedding.get_embeddings` | 同理 |
| 检索 | `rag_chain.hybrid_retrieve` | 在 `rag_chain` 里是模块属性 |
| 缓存 | `rag_chain.get_cache` / `embedding.get_cache` | 换成 `AppCache(tmp_path)` |
| 两路召回 | `retriever._dense_task` / `retriever._sparse_task` | 直接返回造好的命中列表 |
| 重排器 | `retriever.get_reranker` | 返回一个带 `acompress_documents` 的假对象 |
| HTTP 客户端 | `httpx.Client` / `httpx.AsyncClient` | `reranker` 里是 `import httpx`,打在模块属性上 |
| OpenAI 客户端 | `embedding.OpenAIEmbeddings` | 换掉可以免去 api_key 校验,不必依赖 `.env` |

## 本项目测试要点清单

改到哪个模块,就补对应文件里的用例。括号内是当前用例数,可作为「有没有漏」的参照。

### `services/cache.py` → `tests/test_cache.py`(15)

- embedding 缓存:命中/未命中,**换模型必须换命名空间**
- 语义缓存:同向量命中、低于阈值不命中、阈值可调、**按知识库隔离**、空缓存返回 None
- **全零向量不能抛 ZeroDivisionError**,也不能匹配成功
- `invalidate_semantic` 只清目标库
- 语义索引有上限(`SEMANTIC_INDEX_LIMIT`)
- 限流:达到上限后拒绝、剩余额度不出现负数、按 key 分别计数
- **关闭缓存时全部退化为安全 no-op,且限流一律放行**(缓存故障不能变成服务不可用)

### `services/retriever.py` → `tests/test_retrieval.py`(19)+ `tests/test_hybrid_retrieve.py`(16)

`test_retrieval.py` —— 内部算子:

- RRF 公式与论文一致:`sum(1/(k+rank+1))`;两路都召回的要排前面;**只看排名不看原始分数**;支持权重
- 分词:中文切分、大小写归一、丢弃空白 token
- 稀疏打分:降序、**过滤 0 分**、遵守 top_k、无 BM25 对象时安静返回空
- 索引生命周期:失效后从 `bm25_index_stats()` 消失、重复失效不抛 KeyError
- `_format_vector`:方括号字面量、**元素个数保持**、确定性、不退化科学计数法

`test_hybrid_retrieve.py` —— 编排:

- 空白问题短路(不该算向量、不该查库);两路都没召回返回空
- **稠密命中里不在索引快照中的分块必须丢弃**(否则会按错误坐标取到不相干的正文)
- Document 带出 `chunk_id`/`filename`/`chunk_index`/`rrf_score`/`dense_score`
- 只在稀疏路出现的分块 `dense_score` 为 None
- 候选池 = `max(top_k*4, top_k)` 且不超过融合结果数;最终截断到 `top_k`
- 重排:生效、**只有 1 条候选时跳过**、`use_rerank=False` 时跳过、降级不丢候选不乱序

> ⚠️ 写排序断言时注意 **RRF 会出现并列分**(两路各自排第一的分块得分相同),
> 并列时顺序取决于字典插入序。断言前先构造一个两路都排第一的分块来打破并列。

### `services/rag_chain.py` → `tests/test_rag_chain.py`(36)

- `_format_context`:编号从 1 起、带来源文件名、缺文件名时用「未知来源」、空列表返回空串
- `build_references`:index 连续、快照 `chunk_id`/`document_id`/文件名/正文/三个分数、缺字段不炸
- `_history_messages`:**参数单位是「轮」不是「条」**(一轮 = 2 条消息)、丢弃空内容、忽略未知角色、`turns<=0` 返回空
- `rewrite_query`:无历史时不调模型;可被配置关闭;能消解指代;去掉模型加的引号;
  空回复/超长回复**沿用原问题**;模型报错也要安静降级
- `generate_title`:用模型回复、去引号并**硬截到 20 字**、失败回退到问题前 20 字、空问题用「新对话」
- `answer_stream` 事件流:
  - 无知识库 → 用 `NO_CONTEXT_PROMPT`,不发 references,不算查询向量
  - **references 必须先于首个 token**;提示词里的编号与引用卡片同源
  - 带指代的追问要用**改写后的问题**去检索;未改写时不发 rewrite 事件
  - 命中语义缓存 → 不检索、**不调模型**、done 带 `cache_hit=True`
  - **检索为空时绝不写语义缓存**(否则一次降级会在整个 TTL 内反复复现)
  - 模型失败/向量失败 → 收敛成 `error` 事件收尾,不抛异常、不漏堆栈
  - 支持结构化内容块(只取其中的文本)、丢弃空增量

### `services/reranker.py` → `tests/test_reranker.py`(17)

- `_endpoint` 走**原生接口**路径,不能含 `compatible-mode`
- `_payload`:`return_documents=False`、`top_n` 取 `min(top_n, 候选数)`
- `_apply`:下标映射回原文档、**保留原 metadata**、越界/缺 index 时跳过、空结果返回空
- 空候选短路(不发请求)
- **失败降级**:连接错误 / 超时 / 4xx 都要返回原顺序,且不带 `rerank_score`
- 同步与异步两条路径行为一致
- `get_reranker(top_n)` 按 top_n 缓存

### `services/embedding.py` → `tests/test_embedding.py`(16)

- `_split_missing`:全未命中 / 全命中 / 部分命中;**按模型名分命名空间**
- 空输入短路
- **分批**:严格按 `EMBED_BATCH_SIZE`(10)切
- **顺序与入参一一对应**(混合命中时最容易错,一错就是答非所问)
- 同文本第二次调用**不再请求云端**(省钱的核心断言)
- 已知取舍:同一批内的重复文本不去重(缓存是请求回来才写的),跨调用才会省
- 查询缓存与文档缓存共命名空间,可用文档向量回答查询
- 同步与异步共享同一份缓存
- 关闭缓存时仍返回正确向量(只是不省钱)

### `services/ingestion.py` → `tests/test_chunking.py`(8)+ `tests/test_ingestion.py`(8)

- 分块:不留空白块、不短于 `MIN_CHUNK_CHARS`、Markdown 块带标题路径、超长章节二次切分、正文不丢
- `count_tokens`:正整数、单调;**tiktoken 失败时退化为字符数**、空串为 0
- `document_file_path`:用文档 id 命名、后缀小写、只取最后一个后缀、无后缀不抛异常

### `services/parsers/` → `tests/test_parsers.py`(11)

编码探测(UTF-8/BOM/GBK 回退/不可解码不抛)、txt/md/csv/docx/xlsx 各自能取出正文、
不支持的后缀要报错、上传白名单与解析器实现保持一致

### `core/deps.py` → `tests/test_deps.py`(13)

- 令牌:合法解析出用户;**无 token / 空 token / 乱码 / 别的密钥签的 / sub 非数字 / 用户已删**一律 401
- `require_admin`:admin 放行、普通用户 **403**
- `client_key`:登录用户优先按 `user:<id>`;否则取 `X-Forwarded-For` 第一段;
  都没有时不能抛异常

> 这是需求 6「仅管理员可进知识库管理」在服务端的唯一落点,改动鉴权务必跑它。

### `core/security.py` → `tests/test_security.py`(10)

密码加盐不可逆、错误密码拒绝、**超 72 字节拒绝**(bcrypt 静默截断的隐患)、
损坏哈希返回 False 而不是抛异常;JWT 往返、换密钥的伪造令牌拒绝、过期拒绝、垃圾串拒绝

> bcrypt 故意慢,这个文件耗时 ~1.4s,是整套里最慢的,属正常现象。

### 纯函数 → `tests/test_db.py`(5)、`tests/test_config.py`(8)、`tests/test_schemas.py`(14)

- `to_async_url`:三种同步前缀都能改写、已是异步则幂等、不认识的写法原样返回
- 配置:`cors_origin_list` 切分/去空、上传白名单与解析器一致、
  检索参数关系(`rerank_top_k < retrieve_top_k`、`chunk_overlap < chunk_size`)、`embedding_dim == 1024`
- schemas:会话 PATCH 的「显式 null = 解绑」与「字段缺失 = 不动」必须能区分开

## 执行测试

```bash
cd D:/LangChainRAG/backend
../.venv/Scripts/python.exe -m pytest -q                 # 全部
../.venv/Scripts/python.exe -m pytest tests/test_rag_chain.py -q          # 单文件
../.venv/Scripts/python.exe -m pytest -q -k "cache_hit"   # 按名字筛选
../.venv/Scripts/python.exe -m pytest -q -x               # 首个失败即停
```

`pytest.ini` 已配置 `testpaths = tests`、`addopts = -q --tb=short`,
并把 `app.*` 的 DeprecationWarning 升级为错误 —— 撞到这类报错说明代码用了过时写法,要改代码而不是关掉这条配置。

## 生成测试报告

用内置的 junit 报告拿到**精确的分文件统计**,再据此汇总(不安装额外依赖):

```bash
cd D:/LangChainRAG/backend
../.venv/Scripts/python.exe -m pytest --junitxml=junit-tmp.xml -q
```

解析 `junit-tmp.xml` 里每个 `<testcase>` 的 `classname` / `time`,以及
`<failure>` / `<error>` / `<skipped>` 子节点,按文件聚合。**解析完删掉临时 xml**。

> Windows 上注意:`/tmp` 是 Git Bash 的虚拟路径,原生 Python 打不开,
> 临时文件写到 `backend/` 下的相对路径(用完 `rm`)。

### 报告格式

```
## 单元测试报告
- 总计:N 个用例,通过 P,失败 F,错误 E,跳过 S
- 通过率:xx%
- 执行时长:x.xx s

### 测试文件明细
| 测试文件 | 用例数 | 失败 | 耗时 |
|---------|-------|------|------|
| test_rag_chain | 36 | 0 | 0.31s |
| ... |

### 失败/错误用例(如有)
- tests/test_xxx.py::test_yyy — 原因:...(附关键断言差异或异常类型)

### 结论
✅ 全部通过 / ❌ 存在失败,需修复
```

报告必须如实反映 pytest 结果,**不得虚构通过**;失败时列出失败用例、断言差异与异常类型,
便于定位。若失败来自「测试期望写错了」而非代码缺陷,要明确说明是哪一种。

## 注意事项

- **纯单元测试**:不启动服务、不连数据库、不调云端接口,秒级完成;
  真实链路验证交给 `backend/scripts/` 下的三个联调脚本,两者不要互相替代
- **不修改生产代码**;若测试暴露了真实缺陷,**先报告用户**再由用户决定是否修复,
  不要在补测试的过程里顺手改业务逻辑
- 注释与 docstring 用中文,**标识符用英文**
- 新增依赖前先确认真的需要:现有 pytest + anyio + monkeypatch 已能覆盖
  异步、打桩、参数化、报告,不要为了一点便利引入 pytest-asyncio / pytest-mock
- 断言写「不变量」而不是「当前实现的细节」。例如断言「候选池为 `max(top_k*4, top_k)`」
  时,docstring 要说明这是「给重排留挑选空间」的意图;若将来改为可配置,
  应当同步改用例而不是删掉它
