from pathlib import Path

# 版本唯一真相源：仓库根 VERSION 文件。app/version.py 只读取、不硬编码；
# 前端、docker 镜像 tag、git release tag 都对齐到同一个 VERSION（见 scripts/check_version_consistency.py 硬门）。
_VERSION_FILE = Path(__file__).resolve().parents[1] / "VERSION"


def _read_version() -> str:
    try:
        return _VERSION_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return "0.0.0+missing-VERSION-file"


APP_VERSION = _read_version()
