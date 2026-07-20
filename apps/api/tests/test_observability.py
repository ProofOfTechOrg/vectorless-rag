from vectorless_rag.observability import mask_payload, redacted_locator_payload


def test_masker_removes_credentials_and_reasoning() -> None:
    masked = mask_payload(
        {
            "authorization": "Bearer secret",
            "nested": {"password": "secret", "reasoning_content": "private chain"},
            "safe": "value",
        }
    )
    assert masked["authorization"] == "[REDACTED]"
    assert masked["nested"]["reasoning_content"] == "[REDACTED]"
    assert masked["safe"] == "value"


def test_long_passages_become_hash_size_and_short_preview() -> None:
    value = "x" * 1000
    masked = mask_payload(value)
    assert masked["bytes"] == 1000
    assert len(masked["sha256"]) == 64
    assert len(masked["preview"]) == 120
    locators = redacted_locator_payload([{"document": "a", "page": 1}])
    assert locators["count"] == 1
