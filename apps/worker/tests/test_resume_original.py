import io
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from copilot_worker import documents
from copilot_worker.resume_text import extract, original_text


def pdf_block():
    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=300)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
    )
    content = DecodedStreamObject()
    content.set_data(b"BT /F1 12 Tf 20 200 Td (Original fictional resume) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(content)
    output = io.BytesIO()
    writer.write(output)
    return documents.document_block(output.getvalue(), "application/pdf", "Resume")


async def test_original_pdf_text_is_extracted_in_isolated_process():
    assert "Original fictional resume" in extract(pdf_block())
    assert "Original fictional resume" in await original_text(pdf_block())


async def test_malformed_pdf_and_oversized_input_are_unavailable_not_model_inferences():
    assert (
        await original_text(documents.document_block(b"not a PDF", "application/pdf", "Resume"))
        == ""
    )
    assert await original_text({"source": {"type": "text", "data": "x" * (17 * 1024 * 1024)}}) == ""


async def test_persisted_text_uses_profile_encryption_and_current_content_hash(monkeypatch):
    uid, version = uuid4(), uuid4()
    conn = SimpleNamespace(
        fetchrow=AsyncMock(return_value={"content_sha256": "current", "text_enc": None}),
        execute=AsyncMock(),
    )
    used = []

    @asynccontextmanager
    async def user(profile):
        used.append(profile)
        yield conn

    crypto = SimpleNamespace(encrypt=AsyncMock(return_value=(b"sealed", 7)))
    ctx: Any = SimpleNamespace(user=user, crypto=crypto)
    monkeypatch.setattr(documents, "original_text", AsyncMock(return_value="Original text"))
    await documents.persist_original(ctx, uid, version, "current", {})
    assert used == [uid]
    assert crypto.encrypt.await_args.args[1:4] == (uid, "resume_versions:original", str(version))
    assert conn.execute.await_args.args[1:] == (version, b"sealed", 7, "current")
    for current in (
        None,
        {"content_sha256": "replaced", "text_enc": None},
        {"content_sha256": "current", "text_enc": b"new authored text"},
    ):
        conn.fetchrow.return_value = current
        crypto.encrypt.reset_mock()
        conn.execute.reset_mock()
        await documents.persist_original(ctx, uid, version, "current", {})
        crypto.encrypt.assert_not_awaited()
        conn.execute.assert_not_awaited()
