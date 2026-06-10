from pathlib import Path

# --- email_notifier.py ---
path = Path("src/placement_mail_tracker/notifications/email_notifier.py")
content = path.read_text(encoding="utf-8")

old_send_email = """        try:
            logger.info("Connecting to Gmail SMTP server for email: %s", subject)
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                server.login(self.smtp_email, self.smtp_app_password)
                server.send_message(message)
            logger.info("SMTP email sent successfully to %s", self.email_receiver)
            return True
        except Exception as error:
            logger.error("Failed to deliver SMTP email: %s", error)
            return False"""

new_send_email = """        return self._send_smtp_message_with_retry(message, "email")"""

old_send_alert = """        try:
            logger.info("Connecting to Gmail SMTP server for alert: %s", subject)
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                server.login(self.smtp_email, self.smtp_app_password)
                server.send_message(message)
            logger.info("SMTP notification sent successfully to %s", self.email_receiver)
            return True
        except Exception as error:
            logger.error("Failed to deliver SMTP email notification: %s", error)
            return False"""

new_send_alert = """        return self._send_smtp_message_with_retry(message, "alert")"""

new_helper = """
    def _send_smtp_message_with_retry(self, message: EmailMessage, subject_log: str) -> bool:
        import time
        import ssl
        
        backoffs = [2, 5, 10]
        for attempt, backoff in enumerate(backoffs + [0], 1):
            try:
                logger.info("Connecting to Gmail SMTP server for %s: %s", subject_log, message["Subject"])
                with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
                    server.ehlo()
                    server.starttls()
                    server.ehlo()
                    server.login(self.smtp_email, self.smtp_app_password)
                    server.send_message(message)
                logger.info("SMTP email sent successfully to %s", self.email_receiver)
                return True
            except (smtplib.SMTPServerDisconnected, ssl.SSLEOFError, TimeoutError, ConnectionResetError) as error:
                if attempt <= len(backoffs):
                    logger.warning("[SMTP]\\nRetry attempt %s/%s", attempt, len(backoffs))
                    time.sleep(backoff)
                else:
                    logger.error("Failed to deliver SMTP email after retries: %s", error)
                    return False
            except Exception as error:
                logger.error("Failed to deliver SMTP email: %s", error)
                return False
        return False
"""

content = content.replace(old_send_email, new_send_email)
content = content.replace(old_send_alert, new_send_alert)
content += new_helper

path.write_text(content, encoding="utf-8")
print("Patched email_notifier.py")

# --- gemini_extractor.py ---
path2 = Path("src/placement_mail_tracker/ai/gemini_extractor.py")
content2 = path2.read_text(encoding="utf-8")

old_except = """                except (
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
                        time.sleep(backoff)"""

new_except = """                except (
                    GeminiExtractionError,
                    ValidationError,
                    json.JSONDecodeError,
                    TypeError,
                    ValueError,
                    genai_errors.APIError,
                    ConnectionError,
                    TimeoutError,
                ) as error:
                    last_error = error
                    logger.warning("Gemini extraction attempt %s failed: %s", attempt, error)
                    
                    if attempt < self.max_retries:
                        if isinstance(error, genai_errors.APIError):
                            # Do not retry invalid API key (400, 401), quota exhausted (429), permission denied (403)
                            # within the same model's attempt loop.
                            if error.code in {400, 401, 403, 429}:
                                break
                                
                        backoffs = [2, 5, 10]
                        sleep_time = backoffs[attempt - 1] if attempt <= len(backoffs) else 10
                        time.sleep(sleep_time)"""

content2 = content2.replace(old_except, new_except)

path2.write_text(content2, encoding="utf-8")
print("Patched gemini_extractor.py")
