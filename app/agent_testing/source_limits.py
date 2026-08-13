"""Agent workspace 源码树的共享有界契约。"""

from typing import Final

MAX_SOURCE_FILES: Final = 10_000
MAX_SOURCE_BYTES: Final = 256 * 1024 * 1024
MAX_SOURCE_FILE_BYTES: Final = 64 * 1024 * 1024
MAX_SOURCE_PATH_BYTES: Final = 4 * 1024
MAX_SOURCE_PATH_DEPTH: Final = 32
MAX_SOURCE_COMPONENT_BYTES: Final = 255
