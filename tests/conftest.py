from __future__ import annotations

import os
from dataclasses import dataclass

import pytest


@dataclass(frozen=True)
class ProcessEnvironment:
    """隔离测试对真实进程环境的显式修改，不提供函数或对象替换能力。"""

    def set(self, name: str, value: str) -> None:
        os.environ[name] = value

    def remove(self, name: str) -> None:
        os.environ.pop(name, None)

    def chdir(self, path) -> None:
        os.chdir(path)


@pytest.fixture
def process_environment():
    original = dict(os.environ)
    original_cwd = os.getcwd()
    environment = ProcessEnvironment()
    try:
        yield environment
    finally:
        os.chdir(original_cwd)
        os.environ.clear()
        os.environ.update(original)
