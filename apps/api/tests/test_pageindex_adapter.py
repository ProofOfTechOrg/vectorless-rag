from vectorless_rag.pageindex_adapter import artifact_version_key, normalize_artifact


def test_artifact_normalizes_nested_tree_and_pages() -> None:
    raw = {
        "doc_description": "A paper",
        "structure": [
            {
                "node_id": "0001",
                "title": "Methods",
                "start_index": 2,
                "end_index": 4,
                "summary": "Method details",
                "nodes": [{"node_id": "0002", "title": "Data", "start_index": 3}],
            }
        ],
    }
    artifact = normalize_artifact(raw, ["one", "two", "three", "four"], metadata={"title": "T"})
    assert artifact["schema_version"] == 1
    assert [node["node_id"] for node in artifact["tree"]] == ["0001", "0002"]
    assert artifact["tree"][1]["page_start"] == 3
    assert artifact["pages"][0] == {"page": 1, "text": "one"}


def test_index_version_changes_for_every_material_input() -> None:
    base = artifact_version_key("sha", "pageindex", "prompt", "model")
    assert base != artifact_version_key("other", "pageindex", "prompt", "model")
    assert base != artifact_version_key("sha", "pageindex-2", "prompt", "model")
    assert base != artifact_version_key("sha", "pageindex", "prompt-2", "model")
    assert base != artifact_version_key("sha", "pageindex", "prompt", "model-2")
