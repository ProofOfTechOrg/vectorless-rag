from vectorless_rag.auth import generate_api_key, hash_api_key, validate_scopes


def test_api_key_is_high_entropy_and_only_hash_is_stable() -> None:
    prefix, raw = generate_api_key()
    assert raw.startswith(f"vrag_{prefix}_")
    assert len(raw) >= 50
    digest = hash_api_key(raw, "pepper")
    assert len(digest) == 64
    assert raw not in digest
    assert digest != hash_api_key(raw, "different-pepper")


def test_scope_validation_rejects_unknown_scope() -> None:
    try:
        validate_scopes({"chat", "root"})
    except ValueError as exc:
        assert "root" in str(exc)
    else:
        raise AssertionError("unknown scope was accepted")
