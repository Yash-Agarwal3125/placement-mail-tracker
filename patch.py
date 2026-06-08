import sys
from pathlib import Path

file_path = Path("src/placement_mail_tracker/ai/gemini_extractor.py")
content = file_path.read_text(encoding="utf-8")

old_extract = """    def extract_from_text(self, email_content: str) -> dict[str, Any]:
        \"\"\"Send cleaned email content to Gemini and return validated dictionaries.\"\"\"
        if not self.settings.gemini_api_key and self._model is None:
            logger.warning("Gemini API key is missing; returning empty extraction result")
            return empty_extraction_result()

        prompt = build_extraction_prompt(email_content)
        last_error: Exception | None = None

        for attempt in range(1, self.max_retries + 1):
            try:
                model_name = self.settings.gemini_model
                logger.info(
                    "Requesting Gemini placement extraction, attempt %s (model: %s)",
                    attempt,
                    model_name,
                )
                response = self._generate_content(prompt)
                raw_text = _response_text(response)
                parsed = parse_json_response(raw_text)
                return validate_extraction_result(parsed)
            except (
                GeminiExtractionError,
                ValidationError,
                json.JSONDecodeError,
                TypeError,
                ValueError,
                genai_errors.APIError,
            ) as error:
                last_error = error
                logger.warning("Gemini extraction attempt %s failed: %s", attempt, error)
                if attempt < self.max_retries:
                    backoff = 2**attempt
                    time.sleep(backoff)

        logger.error("Gemini extraction failed after %s attempts", self.max_retries)
        if last_error:
            raise GeminiExtractionError(str(last_error)) from last_error
        raise GeminiExtractionError("Unknown Gemini extraction failure")"""

new_extract = """    def extract_from_text(self, email_content: str) -> dict[str, Any]:
        \"\"\"Send cleaned email content to Gemini and return validated dictionaries.\"\"\"
        if not self.settings.gemini_api_key and self._model is None:
            logger.warning("Gemini API key is missing; returning empty extraction result")
            return empty_extraction_result()

        prompt = build_extraction_prompt(email_content)
        last_error: Exception | None = None

        models_to_try = [self.settings.gemini_model] + self.settings.gemini_fallback_models

        for idx, model_name in enumerate(models_to_try):
            if idx == 0:
                logger.info("[GEMINI]\\nUsing model: %s", model_name)
            else:
                logger.info("[GEMINI]\\nSwitching to fallback model:\\n%s", model_name)

            for attempt in range(1, self.max_retries + 1):
                try:
                    logger.info(
                        "Requesting Gemini placement extraction, attempt %s (model: %s)",
                        attempt,
                        model_name,
                    )
                    response = self._generate_content(prompt, model_name)
                    raw_text = _response_text(response)
                    parsed = parse_json_response(raw_text)
                    return validate_extraction_result(parsed)
                except (
                    GeminiExtractionError,
                    ValidationError,
                    json.JSONDecodeError,
                    TypeError,
                    ValueError,
                    genai_errors.APIError,
                ) as error:
                    last_error = error
                    logger.warning("Gemini extraction attempt %s failed: %s", attempt, error)
                    if attempt < self.max_retries:
                        backoff = 2**attempt
                        time.sleep(backoff)

        logger.error("Gemini extraction failed after trying all fallback models")
        if last_error:
            raise GeminiExtractionError(str(last_error)) from last_error
        raise GeminiExtractionError("Unknown Gemini extraction failure")"""

old_generate = """    def _generate_content(self, prompt: str) -> Any:
        \"\"\"Generate content using an injected fake model or the Gemini API.\"\"\"
        if self._model is not None:
            return self._model.generate_content(prompt)

        if self._client is None:
            self._client = genai.Client(api_key=self.settings.gemini_api_key)

        model_name = self.settings.gemini_model

        return self._client.models.generate_content(
            model=model_name,
            contents=prompt,
            config={
                "temperature": 0.0,
                "response_mime_type": "application/json",
            },
        )"""

new_generate = """    def _generate_content(self, prompt: str, model_name: str) -> Any:
        \"\"\"Generate content using an injected fake model or the Gemini API.\"\"\"
        if self._model is not None:
            return self._model.generate_content(prompt)

        if self._client is None:
            self._client = genai.Client(api_key=self.settings.gemini_api_key)

        return self._client.models.generate_content(
            model=model_name,
            contents=prompt,
            config={
                "temperature": 0.0,
                "response_mime_type": "application/json",
            },
        )"""

if old_extract not in content:
    print("old_extract not found!")
    sys.exit(1)

if old_generate not in content:
    print("old_generate not found!")
    sys.exit(1)

content = content.replace(old_extract, new_extract)
content = content.replace(old_generate, new_generate)

file_path.write_text(content, encoding="utf-8")
print("Patch applied successfully.")
