"""文档摄取:解析 → 分块 → 批量向量化 → 写入 chunks 表。

流程设计要点:
- 先落 Document 行拿到 id,再用 id 命名落盘文件,避免额外的路径字段
- 分块先按小节标题切、再按中文标点递归切,并给每块挂上「标题路径」前缀,
  让块脱离原文后仍自带上下文(详见 _iter_sections)
- 向量化走批量 + 缓存,重复入库不重复花钱
- 入库成功后失效语义缓存,防止答出旧内容
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from sqlalchemy import delete, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..models import Chunk, Document, DocumentStatus
from .cache import get_cache
from .embedding import get_embeddings
from .parsers import parse_file

logger = logging.getLogger(__name__)

# 中文友好的切分优先级:先按段落,再按句末标点,再到分句标点,最后才按字符兜底。
# 全角标点必须写成真正的全角字符:漏掉「，」「！」「？」「；」的话,像商品参数
# 这种整行只用逗号分隔、又不含句号的文本会一路退化到按字符硬切,把「5000mAh」
# 这类词从中间劈开,检索命中了也读不通。英文标点带上后面的空格,否则会把
# 3.14 / v1.2 / 6.7 英寸 从数字中间切开。
CHINESE_SEPARATORS = [
    "\n\n",
    "\n",
    "。", "！", "？", "；", "…", "，",
    ". ", "! ", "? ", "; ", ", ",
    " ",
    "",
]

# 过短的分块检索价值低且浪费向量额度,直接丢弃
MIN_CHUNK_CHARS = 10

# 标题路径前缀与表头会跟着每一块走,会从 chunk_size 里分掉一部分。
# 但不能让正文预算被长标题挤到不可用,留一个下限。
MIN_CHUNK_BUDGET = 120

# 一行的长度超过这个值基本是正文而不是小节标题,避免把整段话误判成标题
MAX_HEADING_CHARS = 40

# ---------- 小节标题识别 ----------
# Markdown 走井号标题;纯文本走中文资料里最常见的编号标题(一、/ 1. / (一) / 第三章)。
# 用户上传的商品资料很多是 .txt,如果只按段落机械地切,就会出现「洗涤:优先干洗,
# 水温≤30℃…」这种没了主语的块 —— 检索命中后连它属于哪种材质都看不出来。
# 所以两种格式统一按小节切分,并把小节标题作为路径前缀挂到每一块上。
_MD_HEADING = re.compile(r"^(#{1,6})\s+(?P<text>\S.*?)\s*#*\s*$")
_CN_HEADING = re.compile(
    r"^(?P<kind>"
    r"第[一二三四五六七八九十百零\d]+[章节篇部]"       # 第三章 / 第 2 节
    r"|[(（][一二三四五六七八九十\d]+[)）]"             # (一) / (1)
    r"|[一二三四五六七八九十]+[、.．]"                  # 一、
    # 编号后面不能紧跟数字,否则「6.7 英寸」这种正文会被误判成标题
    r"|\d+[、.．](?!\d)"                                # 1. / 1、
    r"|\d+[.．]\d+[、.．](?!\d)"                        # 1.2.
    r")\s*(?P<text>\S.*)$"
)
# 标题行末尾如果是句读,几乎可以肯定是正文
_SENTENCE_TAIL = "。，；！？,;!?"

# 标题末尾的括号举例,例如「一、春季服装（纯棉、薄牛仔、针织棉、轻薄化纤）」。
# 这类括号在目录式资料里是同级枚举,详见 _iter_sections 里 path() 的处理。
_TAIL_PARENTHETICAL = re.compile(r"\s*[（(][^）)]*[）)]\s*$")
# 切点上的标点会被 keep_separator 留在下一块开头,这些字符开头的块没有意义
_LEADING_PUNCT = "，、；：！？…,;:!?"


def document_file_path(document_id: int, filename: str) -> Path:
    """上传文件在磁盘上的落盘路径:用文档 id 命名,天然去重。"""
    suffix = Path(filename).suffix.lower()
    return settings.upload_dir / f"{document_id}{suffix}"


def count_tokens(text: str) -> int:
    """估算 token 数,用于统计展示;失败时退化为字符数。"""
    try:
        import tiktoken

        return len(tiktoken.get_encoding("cl100k_base").encode(text))
    except Exception:
        return len(text)


def _recursive_splitter(chunk_size: int | None = None) -> "RecursiveCharacterTextSplitter":
    from langchain_text_splitters import RecursiveCharacterTextSplitter

    size = chunk_size or settings.chunk_size
    return RecursiveCharacterTextSplitter(
        chunk_size=size,
        # 预算被前缀压小时按比例缩小重叠,否则重叠会逼近块长,块与块几乎重复
        chunk_overlap=min(settings.chunk_overlap, size // 4),
        separators=CHINESE_SEPARATORS,
        keep_separator=True,
        length_function=len,
    )


def _backbone(title: str) -> str:
    """去掉标题末尾的括号举例,只留主干。括号不在末尾(标题中间)时原样返回。"""
    return _TAIL_PARENTHETICAL.sub("", title).strip()


def _cn_heading_level(kind: str) -> int:
    """编号标题的层级,用来拼出可读的嵌套路径(一、> 1.)。"""
    if kind.startswith("第"):
        return 2
    if kind.startswith(("(", "（")):
        return 3
    if kind[0].isdigit():
        return 2 + kind.count(".") + kind.count("．")
    return 2


def _heading_of(line: str, allow_markdown: bool) -> tuple[int, str] | None:
    """识别一行是不是小节标题,是则返回 (层级, 标题文本)。"""
    if allow_markdown:
        m = _MD_HEADING.match(line)
        if m:
            return len(m.group(1)), m.group("text")

    s = line.strip()
    if (
        not s
        or len(s) > MAX_HEADING_CHARS
        or s[-1] in _SENTENCE_TAIL
    ):
        return None
    m = _CN_HEADING.match(s)
    if m:
        return _cn_heading_level(m.group("kind")), s
    return None


def _iter_sections(text: str, allow_markdown: bool) -> list[tuple[str, str]]:
    """把文本切成 (标题路径, 正文) 序列。

    标题行本身不留在正文里 —— 它已经进了路径前缀,再留一份只是白占 token,
    还会让同一句话在检索时被两份文本重复计分。
    """
    # level -> (完整标题, 去掉末尾括号举例后的主干)
    titles: dict[int, tuple[str, str]] = {}
    body: list[str] = []
    out: list[tuple[str, str]] = []

    def path() -> str:
        if not titles:
            return ""
        leaf = max(titles)
        return " > ".join(
            # 叶子用完整标题,父级只用主干。父级标题的括号往往是同级枚举,
            # 整条复制进每一块等于给兄弟块都塞上彼此的名字:实测「雪纺怎么洗」
            # 时夏季四块全都含「雪纺」,BM25 分辨不出,正确答案被棉麻块挤到第二。
            full if level == leaf else (backbone or full)
            for level, (full, backbone) in sorted(titles.items())
        )

    def flush() -> None:
        content = "\n".join(body).strip()
        body.clear()
        if content:
            out.append((path(), content))

    for line in text.splitlines():
        head = _heading_of(line, allow_markdown)
        if head is None:
            body.append(line)
            continue
        flush()
        level, title = head
        # 同级及更深的旧标题要被挤掉,否则路径会串到上一个分支上去
        for stale in [k for k in titles if k >= level]:
            titles.pop(stale)
        titles[level] = (title, _backbone(title))
    flush()
    return out


def _is_table_separator(line: str) -> bool:
    """Markdown 表格的分隔行,形如 `| --- | :--: |`。"""
    s = line.strip().strip("|").strip()
    return bool(s) and "-" in s and set(s) <= set("-: ")


def _table_header(text: str) -> str:
    """整段都是「每行带 |」的表格时,返回表头行,供每块重复。

    表格被切开后,后半截只剩「| 16GB+512GB | 4799 元 | 4499 元 |」这种裸数据,
    列名没了,模型无从判断 4799 是指导价还是优惠价 —— 商品参数表最常栽在这里。
    """
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    if len(lines) < 3 or not all("|" in ln for ln in lines):
        return ""
    head = [lines[0]]
    if _is_table_separator(lines[1]):
        head.append(lines[1])
    return "\n".join(head)


def _sections_to_chunks(sections: list[tuple[str, str]]) -> list[str]:
    out: list[str] = []
    for path, content in sections:
        prefix = f"【{path}】\n" if path else ""
        header = _table_header(content)
        # 前缀和表头会跟着每一块走,预算得按「最终长度」扣掉,否则每块都会超长
        budget = max(
            MIN_CHUNK_BUDGET,
            settings.chunk_size - len(prefix) - (len(header) + 1 if header else 0),
        )
        pieces = (
            _recursive_splitter(budget).split_text(content)
            if len(content) > budget
            else [content]
        )
        head_line = header.split("\n")[0] if header else ""
        for piece in pieces:
            # keep_separator 会把切点上的标点留在下一块开头,显示出来就是一个孤零零
            # 的「，」;它不承载任何信息,去掉再判长度
            piece = piece.strip().lstrip(_LEADING_PUNCT)
            if len(piece) < MIN_CHUNK_CHARS:
                continue
            # 首块本身就带着表头,别重复加
            if header and head_line not in piece:
                piece = f"{header}\n{piece}"
            out.append(f"{prefix}{piece}" if prefix else piece)
    return out


def _split_plain(text: str) -> list[str]:
    """纯文本:按小节标题切分;没有标题时退化为按段落/句子切分。"""
    return _sections_to_chunks(_iter_sections(text, allow_markdown=False))


def _split_markdown(text: str) -> list[str]:
    """Markdown:按标题层级切分,并把「标题路径」挂到每块前面。

    这样分块脱离原文后仍自带上下文,避免检索到的片段看不出属于哪个主题,
    也让模型更容易判断信息属于哪个章节。
    """
    return _sections_to_chunks(_iter_sections(text, allow_markdown=True))


def split_text(text: str, is_markdown: bool = False) -> list[str]:
    """把长文本切成检索友好的分块。Markdown 认井号标题,纯文本认编号标题。"""
    return _split_markdown(text) if is_markdown else _split_plain(text)


async def ingest_document(session: AsyncSession, document_id: int) -> int:
    """处理一个已落库的文档,返回写入的分块数。

    失败时把文档标记为 failed 并记录原因,不向上抛异常 ——
    上传接口是异步触发的,异常无法反馈给前端,状态字段才是反馈渠道。
    """
    doc = await session.get(Document, document_id)
    if doc is None:
        raise ValueError(f"文档 {document_id} 不存在")

    path = document_file_path(doc.id, doc.filename)
    try:
        if not path.exists():
            raise FileNotFoundError(f"上传文件缺失: {path.name}")

        doc.status = DocumentStatus.PROCESSING
        doc.error_message = ""
        await session.commit()

        # 1. 解析
        text = parse_file(path)
        if not text.strip():
            raise ValueError("解析结果为空,可能是扫描件或加密文件")

        # 2. 分块(markdown 走结构感知切分)
        pieces = split_text(text, is_markdown=path.suffix.lower() in {".md", ".markdown"})
        if not pieces:
            raise ValueError("分块结果为空,文本可能过短")

        # 3. 批量向量化(带缓存)
        embeddings = get_embeddings()
        vectors = await embeddings.aembed_documents(pieces)

        # 4. 先清掉旧分块,保证重复处理不会产生重复数据
        await session.execute(delete(Chunk).where(Chunk.document_id == doc.id))

        rows = [
            {
                "knowledge_base_id": doc.knowledge_base_id,
                "document_id": doc.id,
                "content": piece,
                "embedding": vector,
                "chunk_index": i,
                "token_count": count_tokens(piece),
            }
            for i, (piece, vector) in enumerate(zip(pieces, vectors))
        ]
        await session.execute(insert(Chunk), rows)

        doc.status = DocumentStatus.READY
        doc.chunk_count = len(rows)
        doc.error_message = ""
        await session.commit()

        # 5. 内容变了,旧答案不能再命中
        get_cache().invalidate_semantic()

        logger.info("文档 %s 摄取完成,共 %d 个分块", doc.id, len(rows))
        return len(rows)

    except Exception as e:
        await session.rollback()
        logger.exception("文档 %s 摄取失败", document_id)
        # 重新取一次,rollback 后原对象可能已过期
        doc = await session.get(Document, document_id)
        if doc is not None:
            doc.status = DocumentStatus.FAILED
            doc.error_message = str(e)[:500]
            doc.chunk_count = 0
            await session.commit()
        return 0


async def delete_document_chunks(session: AsyncSession, document_id: int) -> None:
    await session.execute(delete(Chunk).where(Chunk.document_id == document_id))
