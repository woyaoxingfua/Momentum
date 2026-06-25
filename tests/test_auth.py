"""Test auth module — password hashing, verification, token generation."""

from momentum_agent.auth import hash_password, verify_password, generate_token, utcnow


class TestHashPassword:
    def test_hash_password_returns_salted_hash(self) -> None:
        password = "my_secret_password"
        hashed = hash_password(password)

        assert hashed.startswith("pbkdf2:")
        parts = hashed.split(":")
        assert len(parts) == 3
        assert len(parts[1]) == 64  # 32 bytes = 64 hex chars
        assert len(parts[2]) == 64  # 32 bytes = 64 hex chars

    def test_hash_password_different_salts(self) -> None:
        password = "same_password"
        hash1 = hash_password(password)
        hash2 = hash_password(password)

        assert hash1 != hash2  # Different salts

    def test_hash_password_type(self) -> None:
        hashed = hash_password("test")
        assert isinstance(hashed, str)


class TestVerifyPassword:
    def test_verify_correct_password(self) -> None:
        password = "correct_horse_battery"
        hashed = hash_password(password)

        assert verify_password(password, hashed) is True

    def test_verify_wrong_password(self) -> None:
        password = "correct_horse_battery"
        hashed = hash_password(password)

        assert verify_password("wrong_password", hashed) is False

    def test_verify_empty_password(self) -> None:
        password = "some_password"
        hashed = hash_password(password)

        assert verify_password("", hashed) is False

    def test_verify_malformed_stored_hash(self) -> None:
        assert verify_password("pass", "not_valid_format") is False
        assert verify_password("pass", "pbkdf2:short:also_short") is False

    def test_verify_tampered_hash(self) -> None:
        password = "test123"
        hashed = hash_password(password)
        # Tamper with the key part
        tampered = hashed.rsplit(":", 1)[0] + ":ab" + "0" * 62
        assert verify_password(password, tampered) is False


class TestGenerateToken:
    def test_generate_token_length(self) -> None:
        token = generate_token()
        assert len(token) == 64  # 32 bytes = 64 hex chars

    def test_generate_token_type(self) -> None:
        token = generate_token()
        assert isinstance(token, str)

    def test_generate_token_random(self) -> None:
        tokens = [generate_token() for _ in range(100)]
        assert len(set(tokens)) == 100  # All unique

    def test_generate_token_hex_only(self) -> None:
        token = generate_token()
        assert all(c in "0123456789abcdef" for c in token)


class TestUtcnow:
    def test_utcnow_returns_aware_datetime(self) -> None:
        now = utcnow()
        assert now.tzinfo is not None

    def test_utcnow_reasonable_value(self) -> None:
        from datetime import datetime, timezone
        now = utcnow()
        expected = datetime.now(timezone.utc)
        # Within 5 seconds
        assert abs((now - expected).total_seconds()) < 5
