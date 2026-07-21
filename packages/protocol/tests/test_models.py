from notehook_protocol.models.common import BaseVO, fail, ok
from notehook_protocol.models.file import (
    EntriesVO,
    FileUploadApplyLocalDTO,
    FileUploadFinishLocalDTO,
    ListFolderLocalDTO,
    SynchronousStartLocalVO,
)


def test_ok_envelope() -> None:
    vo = ok()
    assert vo.success is True
    assert vo.errorCode == "0000"


def test_fail_envelope() -> None:
    vo = fail("E0019", "Password error")
    assert vo.success is False
    assert vo.errorCode == "E0019"
    assert vo.errorMsg == "Password error"


def test_extra_fields_ignored() -> None:
    # Real firmware may send fields the reverse-engineered spec doesn't document.
    vo = BaseVO.model_validate({"success": True, "someUnknownField": 42})
    assert vo.success is True


def test_upload_apply_size_accepts_string() -> None:
    dto = FileUploadApplyLocalDTO.model_validate({"size": "1024"})
    assert dto.size == 1024


def test_upload_apply_size_accepts_int() -> None:
    dto = FileUploadApplyLocalDTO.model_validate({"size": 2048})
    assert dto.size == 2048


def test_upload_apply_size_empty_string_is_none() -> None:
    dto = FileUploadApplyLocalDTO.model_validate({"size": ""})
    assert dto.size is None


def test_upload_finish_size_accepts_string() -> None:
    dto = FileUploadFinishLocalDTO.model_validate(
        {"fileName": "a.note", "content_hash": "x", "innerName": "y", "size": "77"}
    )
    assert dto.size == 77


def test_list_folder_defaults_to_root() -> None:
    dto = ListFolderLocalDTO.model_validate({})
    assert dto.id == 0
    assert dto.recursive is False


def test_syn_type_defaults_false() -> None:
    vo = SynchronousStartLocalVO()
    assert vo.synType is False


def test_entries_vo_tag_literal() -> None:
    e = EntriesVO.model_validate({"tag": "folder", "id": "3"})
    assert e.tag == "folder"
