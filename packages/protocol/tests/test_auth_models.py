from notehook_protocol.models.auth import (
    LoginDTO,
    LoginVO,
    QueryTokenVO,
    RandomCodeDTO,
    RandomCodeVO,
)


def test_login_dto_minimal() -> None:
    dto = LoginDTO.model_validate(
        {"password": "x", "account": "me@example.com", "equipment": 3, "loginMethod": "2"}
    )
    assert dto.equipment == 3
    assert dto.equipmentNo is None


def test_login_vo_round_trip() -> None:
    vo = LoginVO(success=True, token="tok", userName="chris", isBindEquipment="Y")
    data = vo.model_dump()
    assert data["token"] == "tok"
    assert data["isBindEquipment"] == "Y"


def test_random_code_models() -> None:
    dto = RandomCodeDTO.model_validate({"account": "me@example.com"})
    assert dto.countryCode is None
    vo = RandomCodeVO(success=True, randomCode="abcd", timestamp=123)
    assert vo.randomCode == "abcd"


def test_query_token_vo() -> None:
    vo = QueryTokenVO(success=True, token="tok")
    assert vo.token == "tok"
