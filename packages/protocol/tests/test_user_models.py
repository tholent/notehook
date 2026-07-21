from notehook_protocol.models.user import UserCheckDTO, UserCheckVO


def test_user_check_dto_ignores_unknown_version_field() -> None:
    # The device sends a `version` field the spec doesn't document.
    dto = UserCheckDTO.model_validate({"email": "me@example.com", "version": "202407"})
    assert dto.email == "me@example.com"
    assert dto.telephone is None


def test_user_check_vo_round_trip() -> None:
    vo = UserCheckVO(success=True, errorCode="0000", userId=1, dms="ALL", uniqueMachineId="abc")
    data = vo.model_dump()
    assert data["userId"] == 1
    assert data["dms"] == "ALL"
    assert data["uniqueMachineId"] == "abc"
