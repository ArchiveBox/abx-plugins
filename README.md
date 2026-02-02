# abx-plugins

ArchiveBox-compatible plugin suite (hooks, config schemas, binaries manifests).

This package contains only plugin assets and a tiny helper to locate them.
It does **not** depend on Django or ArchiveBox.

## Usage

```python
from abx_plugins import get_plugins_dir

plugins_dir = get_plugins_dir()
# scan plugins_dir for plugins/*/config.json, binaries.jsonl, on_* hooks
```

Tools like `abx-dl` and ArchiveBox can discover plugins from this package
without symlinks or environment-variable tricks.
