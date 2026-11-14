Browser Harness (use the same connection prefix and exact tab selection above):

```python
from pathlib import Path

directory = Path("opencode/screenshots").resolve()
directory.mkdir(parents=True, exist_ok=True)
activate_tab(current_tab())
print(capture_screenshot(path=str(directory / "browser-check.png")))
```
