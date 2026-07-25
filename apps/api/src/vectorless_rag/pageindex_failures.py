from __future__ import annotations

PAGEINDEX_FAILURE_DIAGNOSTICS = {
    "pageindex_child_failed": "PageIndex failed inside its isolated runtime.",
    "pageindex_content_filter": (
        "The provider rejected PageIndex content under its content policy."
    ),
    "pageindex_empty_response": "The provider returned an empty PageIndex completion.",
    "pageindex_insufficient_system_resource": (
        "The provider reported insufficient system resources."
    ),
    "pageindex_invalid_json": "PageIndex received malformed JSON.",
    "pageindex_invalid_response": "PageIndex received an invalid provider response.",
    "pageindex_llm_retry_exhausted": (
        "The PageIndex provider request failed after all retry attempts."
    ),
    "pageindex_provider_rejected": "The PageIndex provider rejected the request.",
    "pageindex_provider_unavailable": "The PageIndex provider is unavailable.",
    "pageindex_authentication_failed": "PageIndex provider authentication failed.",
    "pageindex_permission_denied": "PageIndex provider permission was denied.",
    "pageindex_rate_limited": "The PageIndex provider rate-limited the request.",
    "pageindex_provider_server_error": "The PageIndex provider returned a server error.",
    "pageindex_network_error": "The PageIndex provider network request failed.",
    "pageindex_truncated_response": "The provider truncated an atomic PageIndex completion.",
    "pageindex_transient_provider_error": ("The PageIndex provider request failed transiently."),
    "pageindex_unexpected_tool_call": "PageIndex received an unexpected tool call.",
    "usage_persistence_failed": "Model usage accounting could not be persisted.",
    "experiment_cost_cap": "The frozen experiment cost cap was reached before the next call.",
}
