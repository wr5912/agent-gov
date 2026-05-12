# A2UI Vendor Source

- Upstream: https://github.com/google/A2UI
- Version: v0.9
- Commit: 19919ef4c8ad3185867f70386fa4669284d7714c
- Included paths: Python SDK source/build files plus schema/catalog JSON files required by `pack_specs_hook.py`.

This minimal vendor copy is used so the Docker image can build the A2UI Python
SDK without cloning GitHub during image build. Python package dependencies are
still installed during the Docker build from the configured PyPI index.
