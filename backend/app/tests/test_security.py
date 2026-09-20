from app.core.security import hash_password, verify_password


def test_password_hashing() -> None:
    plain_password = "correct horse battery staple"
    encoded = hash_password(plain_password)
    assert encoded != plain_password
    assert encoded.startswith("$argon2id$")
    assert verify_password(plain_password, encoded)
    assert not verify_password("wrong password", encoded)
