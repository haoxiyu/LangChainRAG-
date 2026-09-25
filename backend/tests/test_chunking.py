"""分块质量的单元测试。

分块是 RAG 里最容易被忽视、又最影响效果的一环:切得太碎丢上下文,切得太大
稀释相关度。这里锁住几条不变量 —— 不留空白块、不短于阈值、每块带标题路径、
不把词从中间劈开、表格每块都带表头、长度不超过 chunk_size。

这些不变量都对应真实踩过的坑,见各用例里的说明。
"""

from __future__ import annotations

from app.config import settings
from app.services.ingestion import (
    CHINESE_SEPARATORS,
    MIN_CHUNK_CHARS,
    _split_markdown,
    _split_plain,
    split_text,
)

MARKDOWN = """# 星辰X1商品知识库

## 一、基本信息

星辰X1是星辰科技推出的旗舰智能手机,主打影像与长续航,面向中高端市场。

## 二、屏幕与显示

屏幕尺寸为6.7英寸,分辨率为2712×1220,支持120Hz自适应刷新率。

## 三、电池与充电

电池容量为5000mAh,支持120W有线快充,15分钟可充至60%。
"""


def test_plain_split_produces_non_empty_chunks() -> None:
    chunks = _split_plain(MARKDOWN)
    assert chunks
    assert all(c.strip() for c in chunks)


def test_plain_split_drops_chunks_below_min_length() -> None:
    text = "短。\n\n" + "这是一段足够长的正文内容,用来验证短块会被丢弃。" * 3
    chunks = _split_plain(text)
    assert all(len(c) >= MIN_CHUNK_CHARS for c in chunks)


def test_markdown_chunks_carry_heading_path() -> None:
    """标题路径前缀让分块脱离原文后仍能看出属于哪个章节。"""
    chunks = _split_markdown(MARKDOWN)
    assert chunks
    assert all(c.startswith("【") for c in chunks)
    assert any("二、屏幕与显示" in c for c in chunks)
    assert any("三、电池与充电" in c for c in chunks)


def test_markdown_heading_path_nests_h1_and_h2() -> None:
    chunks = _split_markdown(MARKDOWN)
    # 形如【星辰X1商品知识库 > 三、电池与充电】
    assert any("星辰X1商品知识库 > 三、电池与充电" in c for c in chunks)


def test_markdown_content_is_preserved() -> None:
    """分块不能把正文弄丢,否则检索到了也答不出内容。"""
    joined = "".join(_split_markdown(MARKDOWN))
    for keyword in ("6.7英寸", "5000mAh", "120W"):
        assert keyword in joined


def test_long_section_is_split_into_multiple_chunks() -> None:
    """超过 chunk_size 的章节必须二次切分,否则会顶掉上下文预算。"""
    body = "这一节讲的是商品售后政策,内容需要写得足够长才能触发二次切分。" * 40
    text = f"## 四、售后政策\n\n{body}\n"
    chunks = _split_markdown(text)
    assert len(chunks) > 1
    # 前缀会额外占几十字符,给一点余量即可
    assert all(len(c) <= settings.chunk_size + 80 for c in chunks), [
        len(c) for c in chunks
    ]


def test_split_text_dispatches_by_markdown_flag() -> None:
    assert split_text(MARKDOWN, is_markdown=True) == _split_markdown(MARKDOWN)
    assert split_text(MARKDOWN, is_markdown=False) == _split_plain(MARKDOWN)


def test_empty_and_whitespace_input_yields_no_chunks() -> None:
    assert _split_plain("") == []
    assert _split_plain("   \n\n  \t ") == []
    assert _split_markdown("") == []


# ---------- 分隔符 ----------

def test_separators_are_unique_and_cover_fullwidth_punctuation() -> None:
    """全角标点必须真的在表里。

    只写 ASCII 的 `!` `?` `;` `,` 而漏掉全角版本,中文文本就只剩句号一个切点,
    像商品参数这种整行只用逗号分隔的文本会一路退化到按字符硬切。
    """
    assert len(CHINESE_SEPARATORS) == len(set(CHINESE_SEPARATORS)), "分隔符有重复项"
    for ch in ("。", "！", "？", "；", "，"):
        assert ch in CHINESE_SEPARATORS, f"缺少全角分隔符 {ch}"
    # 英文句点不能单独作分隔符,否则 3.14 / v1.2 会被切开
    assert "." not in CHINESE_SEPARATORS


def test_fullwidth_comma_splits_between_items_not_mid_word() -> None:
    """整行只有全角逗号、没有句号的商品参数,不能在词中间硬切。"""
    spec = "，".join(f"参数项{i}:数值{i}单位" for i in range(60))
    chunks = _split_plain(spec)
    assert len(chunks) > 1
    # 切点只可能落在逗号上,所以每块都必须从一个完整的参数项开始。
    # 退化成按字符硬切的话,会出现以「位，参数项…」开头的块(「单位」被劈成两半)。
    for c in chunks:
        assert c.startswith("参数项"), f"块从词中间开始: {c[:20]!r}"


# ---------- 纯文本的小节切分 ----------

PLAIN = """一、春季服装

1. 纯棉材质

洗涤：可机洗或手洗,水温不超过30℃,使用中性洗涤剂。

2. 薄牛仔材质

洗涤：水温不超过30℃,翻面清洗减少褪色,首次洗盐水浸泡固色。
"""


def test_plain_text_numbered_headings_carry_path_prefix() -> None:
    """纯文本也要按小节切,并挂上标题路径。

    用户上传的商品资料常是 .txt。只按段落机械切的话会出现「洗涤:水温不超过
    30℃…」这种没有主语的块 —— 检索命中后连它属于哪种材质都看不出来。
    """
    chunks = _split_plain(PLAIN)
    assert chunks
    assert all(c.startswith("【") for c in chunks)
    assert any("一、春季服装 > 1. 纯棉材质" in c for c in chunks)
    # 两种材质的洗涤说明各自带着自己的标题
    assert any("1. 纯棉材质" in c and "可机洗或手洗" in c for c in chunks)
    assert any("2. 薄牛仔材质" in c and "盐水浸泡固色" in c for c in chunks)


ENUMERATED = """一、春季服装（纯棉、薄牛仔、针织棉）

1. 纯棉材质（春季衬衫）

洗涤：可机洗或手洗,水温不超过30℃,使用中性洗涤剂。

2. 薄牛仔材质（春季牛仔裤）

洗涤：水温不超过30℃,翻面清洗减少褪色,首次洗盐水浸泡固色。

3. 针织棉材质（春季针织衫）

洗涤：手洗优先,水温不超过25℃,轻轻按压,禁止用力搓揉。
"""


def test_ancestor_parenthetical_is_not_copied_into_every_chunk() -> None:
    """父级标题的括号枚举不能复制进每一块。

    「一、春季服装（纯棉、薄牛仔、针织棉）」这种括号列的是同级小节。整条挂进
    前缀,等于给三个兄弟块都塞上彼此的名字:实测原文档里「雪纺怎么洗」让夏季
    四块全部命中「雪纺」,BM25 无从分辨,正确答案被「棉麻」块抢走第一名。
    """
    chunks = _split_plain(ENUMERATED)
    assert len(chunks) == 3

    # 同级枚举整串不该出现在任何一块里
    assert all("（纯棉、薄牛仔、针织棉）" not in c for c in chunks)

    # 每种材质只该命中「自己那一块」——这是本用例真正要守住的东西
    for name in ("纯棉", "薄牛仔", "针织棉"):
        hits = [c for c in chunks if name in c]
        assert len(hits) == 1, f"{name} 扩散到了 {len(hits)} 块"


def test_leaf_heading_keeps_its_own_parenthetical() -> None:
    """叶子标题的括号描述的是它自己,是有用信息,不能一起砍掉。

    「纯棉材质（春季衬衫）」里的「春季衬衫」是检索「T恤/衬衫怎么洗」的唯一线索 ——
    正文只写「洗涤:可机洗…」,不含任何品名。
    """
    chunks = _split_plain(ENUMERATED)
    assert any("1. 纯棉材质（春季衬衫）" in c for c in chunks)
    # 父级只留主干,但主干本身要还在,否则块就不知道自己属于哪一季
    assert all("一、春季服装" in c for c in chunks)
    assert all("（纯棉、薄牛仔、针织棉）" not in c for c in chunks)


def test_decimal_number_line_is_not_mistaken_for_heading() -> None:
    """「6.7 英寸 AMOLED 屏幕…」是正文,不是编号标题。"""
    text = (
        "## 一、屏幕\n\n"
        "6.7 英寸 AMOLED 柔性直屏,支持 120Hz 自适应刷新率。\n"
        "机身重量 210 克,厚度 8.1 毫米。\n"
    )
    chunks = _split_markdown(text)
    assert len(chunks) == 1, [c[:30] for c in chunks]
    assert "6.7 英寸" in chunks[0]
    assert chunks[0].count("【") == 1


def test_heading_line_is_not_repeated_inside_body() -> None:
    """标题已经进了路径前缀,正文里不该再抄一份。"""
    chunks = _split_markdown(MARKDOWN)
    assert all("## 二、屏幕与显示" not in c for c in chunks)
    # 但标题本身仍要能被检索到,所以它必须出现在前缀里
    assert all("【" in c for c in chunks)


# ---------- 长度与表格 ----------

def test_chunks_stay_within_chunk_size_even_with_deep_heading_path() -> None:
    """前缀会跟着每一块走,预算必须按最终长度扣掉,否则每块都会超长。"""
    body = "这一节讲的是商品售后政策的具体执行口径与时效。" * 60
    text = f"# 商品知识库\n\n## 一、售后\n\n### 第三章 售后与保修政策\n\n{body}\n"
    chunks = _split_markdown(text)
    assert len(chunks) > 1
    assert all(
        len(c) <= settings.chunk_size for c in chunks
    ), [len(c) for c in chunks]
    assert all("【商品知识库 > 一、售后 > 第三章 售后与保修政策】" in c for c in chunks)


def test_long_table_repeats_header_in_every_chunk() -> None:
    """表格被切开后,后半截只剩裸数据,必须把表头补回去。

    否则模型看到「| 16GB+512GB | 4799 元 | 4499 元 |」无从判断哪个是指导价、
    哪个是优惠价 —— 商品参数表最常栽在这里。
    """
    rows = "\n".join(f"| 12GB+{i}GB | {4000 + i} 元 | {3900 + i} 元 |" for i in range(30))
    text = (
        "## 三、价格与优惠\n\n"
        "| 版本 | 官方指导价 | 首发优惠价 |\n| --- | --- | --- |\n"
        f"{rows}\n"
    )
    chunks = _split_markdown(text)
    assert len(chunks) > 1
    assert all("| 版本 | 官方指导价 | 首发优惠价 |" in c for c in chunks)
    # 表头只能出现一次,首块自带表头时不能重复叠加
    assert all(c.count("| 版本 |") == 1 for c in chunks)
