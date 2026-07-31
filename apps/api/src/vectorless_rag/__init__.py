"""Vectorless RAG service."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("vectorless-rag")
except PackageNotFoundError:
    __version__ = "0+unknown"
