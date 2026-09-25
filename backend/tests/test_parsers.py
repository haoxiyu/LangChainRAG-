"""文档解析的单元测试。

重点是两件容易出事的事:一是国内用户上传的 txt/csv 常是 GBK 编码,直接按
UTF-8 读会得到乱码(甚至抛异常);二是 docx/xlsx 里的表格经常才是真正的商品
参数,漏掉表格式数据等于知识库缺了一半内容。
"""

from __future__ import annotations

import pytest

from app.services.parsers import (
    _decode_text,
    parse_csv,
    parse_file,
    parse_plain,
    supported_extensions,
)


# ---------- 编码兜底 ----------
def test_decode_utf8_with_bom() -> None:
    """Windows 记事本存的 UTF-8 带 BOM,BOM 混进正文会破坏第一个分块。"""
    assert _decode_text("电池容量".encode("utf-8-sig")) == "电池容量"


def test_decode_gbk_fallback() -> None:
    raw = "电池容量为5000mAh".encode("gbk")
    assert "电池容量" in _decode_text(raw)


def test_decode_undecodable_does_not_raise() -> None:
    """解不出来也要给出可用的字符串,不能让整篇文档入库失败。"""
    assert isinstance(_decode_text(b"\xff\xfe\x00\x01\x80"), str)


# ---------- 纯文本 ----------
def test_parse_plain_txt(tmp_path) -> None:
    path = tmp_path / "spec.txt"
    path.write_bytes("屏幕尺寸:6.7英寸".encode("gbk"))
    assert "6.7英寸" in parse_plain(path)


def test_parse_plain_markdown(tmp_path) -> None:
    path = tmp_path / "kb.md"
    path.write_text("# 商品知识库\n\n电池容量 5000mAh\n", encoding="utf-8")
    text = parse_plain(path)
    assert "# 商品知识库" in text
    assert "5000mAh" in text


# ---------- CSV ----------
def test_parse_csv_joins_cells(tmp_path) -> None:
    path = tmp_path / "params.csv"
    path.write_text("型号,电池,屏幕\n星辰X1,5000mAh,6.7英寸\n", encoding="utf-8")
    text = parse_csv(path)
    assert "型号 | 电池 | 屏幕" in text
    assert "星辰X1 | 5000mAh | 6.7英寸" in text


def test_parse_csv_skips_empty_rows(tmp_path) -> None:
    path = tmp_path / "params.csv"
    path.write_text("a,b\n\n ,\nc,d\n", encoding="utf-8")
    lines = [ln for ln in parse_csv(path).splitlines() if ln.strip()]
    assert lines == ["a | b", "c | d"]


# ---------- Office 文档 ----------
def test_parse_docx_includes_paragraphs_and_tables(tmp_path) -> None:
    docx = pytest.importorskip("docx")
    doc = docx.Document()
    doc.add_paragraph("星辰X1商品说明")
    table = doc.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text = "电池容量"
    table.rows[0].cells[1].text = "5000mAh"
    path = tmp_path / "doc.docx"
    doc.save(str(path))

    text = parse_file(path)
    assert "星辰X1商品说明" in text
    # 商品参数常只存在于表格里,必须被提取
    assert "电池容量" in text and "5000mAh" in text


def test_parse_xlsx_includes_sheet_name_and_rows(tmp_path) -> None:
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "规格表"
    ws.append(["型号", "电池容量"])
    ws.append(["星辰X1", "5000mAh"])
    path = tmp_path / "spec.xlsx"
    wb.save(str(path))

    text = parse_file(path)
    assert "工作表: 规格表" in text
    assert "星辰X1 | 5000mAh" in text


# ---------- 分发与格式校验 ----------
def test_parse_file_rejects_unsupported_extension(tmp_path) -> None:
    """不支持的类型必须明确报错,否则会入库一篇空文档且状态显示成功。"""
    path = tmp_path / "archive.zip"
    path.write_bytes(b"PK\x03\x04")
    with pytest.raises(ValueError):
        parse_file(path)


def test_supported_extensions_cover_upload_whitelist() -> None:
    exts = supported_extensions()
    assert {".pdf", ".docx", ".xlsx", ".csv", ".txt", ".md"} <= exts
