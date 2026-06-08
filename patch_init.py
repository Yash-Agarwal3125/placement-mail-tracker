import sys
from pathlib import Path

file_path = Path("src/placement_mail_tracker/ai/gemini_extractor.py")
content = file_path.read_text(encoding="utf-8")

old_init = """    def __init__(
        self,
        settings: Settings,
        *,
        model: GeminiModel | None = None,
        max_retries: int = 6,  # 1 initial attempt + 5 retries
        retry_delay_seconds: float = 2.0,
    ) -> None:
        self.settings = settings
        self.max_retries = max_retries
        self.retry_delay_seconds = retry_delay_seconds
        self._model = model
        self._client: genai.Client | None = None"""

new_init = """    def __init__(
        self,
        settings: Settings,
        *,
        model: GeminiModel | None = None,
        max_retries: int | None = None,
        retry_delay_seconds: float | None = None,
    ) -> None:
        self.settings = settings
        self.max_retries = max_retries if max_retries is not None else settings.gemini_max_retries
        self.retry_delay_seconds = retry_delay_seconds if retry_delay_seconds is not None else settings.gemini_retry_delay_seconds
        self._model = model
        self._client: genai.Client | None = None"""

if old_init not in content:
    print("old_init not found!")
    sys.exit(1)

content = content.replace(old_init, new_init)
file_path.write_text(content, encoding="utf-8")
print("Patch applied successfully.")
