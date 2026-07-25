#!/usr/bin/env python3
"""Provider-free regression checks for the pinned PageIndex patch."""

from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast


def identity(value: object, *args: object, **kwargs: object) -> object:
    del args, kwargs
    return value


async def async_identity(value: object, *args: object, **kwargs: object) -> object:
    del args, kwargs
    await asyncio.sleep(0)
    return value


def post_process(value: object, page_count: object) -> object:
    del page_count
    return value


def log_info(*args: object) -> None:
    del args


def load_modules(source: Path) -> tuple[Any, Any]:
    if not (source / "pageindex/page_index.py").is_file():
        raise SystemExit(f"PageIndex source is missing: {source}")
    sys.path.insert(0, str(source))
    return importlib.import_module("pageindex.page_index"), importlib.import_module(
        "pageindex.utils"
    )


def test_page_grouping(page_index: Any) -> None:
    page_index.count_tokens = len
    marker = "<physical_index_1>\n" + ("x" * 80) + "\n<physical_index_1>\n"
    groups = page_index.page_list_to_group_text([marker], [len(marker)], max_tokens=55)
    assert groups
    assert all(group and len(group) <= 55 for group in groups)
    assert all("<physical_index_1>" in group for group in groups)


def test_continuation_boundaries(page_index: Any) -> None:
    first = [{"structure": "1", "title": "Methods", "physical_index": 4}]
    assert page_index.merge_continuation_entries(first, list(first)) == first
    same_page_heading = [{"structure": "1.1", "title": "Setup", "physical_index": 4}]
    assert len(page_index.merge_continuation_entries(first, same_page_heading)) == 2
    try:
        page_index.merge_continuation_entries(
            first,
            [{"structure": "1", "title": "Methods", "physical_index": 5}],
        )
    except ValueError:
        pass
    else:
        raise AssertionError("conflicting continuation mapping was accepted")


async def test_toc_without_numbers(page_index: Any) -> None:
    calls: list[str] = []

    def check_toc(page_list: object, opt: object = None) -> dict[str, object]:
        del page_list, opt
        return {
            "toc_content": "Introduction\nMethods",
            "toc_page_list": ["toc"],
            "page_index_given_in_toc": "no",
        }

    async def meta_processor(*args: object, mode: str, **kwargs: object) -> list[dict[str, object]]:
        del args, kwargs
        calls.append(mode)
        return [{"title": "Introduction", "physical_index": 1}]

    page_index.check_toc = check_toc
    page_index.meta_processor = meta_processor
    page_index.post_processing = post_process
    page_index.generate_toc = identity
    page_index.check_title_appearance_in_start_concurrent = async_identity
    page_index.process_large_node_recursively = async_identity
    options = SimpleNamespace(
        if_add_node_id="no",
        if_add_node_summary="no",
        if_add_doc_description="no",
        if_add_node_text="no",
        model="provider-free",
    )
    await page_index.tree_parser(
        [("page", 1)],
        options,
        logger=SimpleNamespace(info=log_info),
    )
    assert calls == ["process_toc_no_page_numbers"]


async def test_bottom_up_summaries(utilities: Any) -> None:
    visits: list[str] = []

    async def summarize(
        node: dict[str, object],
        child_summaries: list[str] | None = None,
        model: str | None = None,
    ) -> str:
        del model
        visits.append(str(node["title"]))
        return f"{node['title']}:{','.join(child_summaries or [])}"

    utilities.generate_node_summary = summarize
    tree: list[dict[str, object]] = [{"title": "root", "nodes": [{"title": "child"}]}]
    result = cast(list[dict[str, object]], await utilities.generate_summaries_for_structure(tree))
    assert visits[:2] == ["child", "child"]
    assert visits[-2:] == ["root", "root"]
    assert str(result[0]["subtree_summary"]).startswith("root:")


def main() -> None:
    source = Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/pageindex").resolve()
    page_index, utilities = load_modules(source)
    test_page_grouping(page_index)
    test_continuation_boundaries(page_index)
    asyncio.run(test_toc_without_numbers(page_index))
    asyncio.run(test_bottom_up_summaries(utilities))
    print("PageIndex patch regression checks passed")


if __name__ == "__main__":
    main()
