import sys
import os
import json
import time
from pathlib import Path

file_path = Path("src/placement_mail_tracker/scheduler/runner.py")
content = file_path.read_text(encoding="utf-8")

# Part 1: Update fetch logic with fetch_state.json
old_fetch = """        try:
            for attempt in range(1, 4):
                try:
                    messages = gmail_client.fetch_recent_messages(
                        max_results=self.settings.gmail_max_results
                    )
                    break
                except HttpError as api_error:
                    if api_error.resp.status in {429, 503} and attempt < 3:
                        sleep_time = attempt * 2.0
                        logger.warning(
                            "Gmail API rate limit hit (%s). Retrying in %ss...",
                            api_error.resp.status, sleep_time,
                        )
                        time.sleep(sleep_time)
                    else:
                        raise
        except Exception as fetch_error:
            logger.error("Could not fetch messages from Gmail API: %s", fetch_error)
            report.mark_component(
                "gmail",
                False,
                str(fetch_error),
                critical=self.settings.is_production,
            )
            return report"""

new_fetch = """        # Read fetch_state.json
        fetch_state_path = Path(self.settings.fetch_state_file)
        if fetch_state_path.exists():
            try:
                state_data = json.loads(fetch_state_path.read_text(encoding="utf-8"))
                last_fetch_iso = state_data.get("last_successful_fetch")
                last_fetch_timestamp = int(datetime.fromisoformat(last_fetch_iso.replace("Z", "+00:00")).timestamp())
            except Exception:
                # Default to 7 days ago if parsing fails
                last_fetch_timestamp = int(time.time()) - (7 * 24 * 3600)
        else:
            # Default to 7 days ago
            last_fetch_timestamp = int(time.time()) - (7 * 24 * 3600)

        try:
            for attempt in range(1, 4):
                try:
                    messages = gmail_client.fetch_recent_messages_since(
                        timestamp_seconds=last_fetch_timestamp,
                        max_results=self.settings.gmail_max_results
                    )
                    break
                except HttpError as api_error:
                    if api_error.resp.status in {429, 503} and attempt < 3:
                        sleep_time = attempt * 2.0
                        logger.warning(
                            "Gmail API rate limit hit (%s). Retrying in %ss...",
                            api_error.resp.status, sleep_time,
                        )
                        time.sleep(sleep_time)
                    else:
                        raise
        except Exception as fetch_error:
            logger.error("Could not fetch messages from Gmail API: %s", fetch_error)
            report.mark_component(
                "gmail",
                False,
                str(fetch_error),
                critical=self.settings.is_production,
            )
            return report"""


# Part 2: Update Gemini fallback in processing loop
old_gemini_processing = """                if rule_result.needs_gemini:
                    # Fall back to Gemini for missing critical fields
                    logger.info("Rule extraction incomplete; calling Gemini")
                    extracted = extractor.extract_from_email(msg)
                    if not extracted:
                        raise ValueError("Gemini extraction returned empty results")

                    extracted_dict = (
                        asdict(extracted) if not isinstance(extracted, dict) else extracted
                    )
                    opp_data = map_extraction_to_opportunity(extracted_dict)
                    gemini_calls += 1

                    # Merge: prefer Gemini data but keep rule-based status/classification
                    if rule_result.current_status != "OPEN":
                        opp_data["current_status"] = rule_result.current_status
                    if not opp_data.get("current_status"):
                        opp_data["current_status"] = rule_result.current_status
                else:
                    # Phase 3: Rule extraction sufficient - no Gemini call!
                    opp_data = rule_result.to_dict()
                    rule_only_count += 1
                    logger.info("Rule extraction sufficient; skipping Gemini (saved API call)")"""

new_gemini_processing = """                if rule_result.needs_gemini:
                    # Fall back to Gemini for missing critical fields
                    logger.info("Rule extraction incomplete; calling Gemini")
                    try:
                        extracted = extractor.extract_from_email(msg)
                        if not extracted:
                            raise ValueError("Gemini extraction returned empty results")

                        extracted_dict = (
                            asdict(extracted) if not isinstance(extracted, dict) else extracted
                        )
                        opp_data = map_extraction_to_opportunity(extracted_dict)
                        gemini_calls += 1

                        # Merge: prefer Gemini data but keep rule-based status/classification
                        if rule_result.current_status != "OPEN":
                            opp_data["current_status"] = rule_result.current_status
                        if not opp_data.get("current_status"):
                            opp_data["current_status"] = rule_result.current_status
                    except Exception as gemini_err:
                        logger.warning("Gemini failed completely, falling back to rule engine: %s", gemini_err)
                        opp_data = rule_result.to_dict()
                else:
                    # Phase 3: Rule extraction sufficient - no Gemini call!
                    opp_data = rule_result.to_dict()
                    rule_only_count += 1
                    logger.info("Rule extraction sufficient; skipping Gemini (saved API call)")"""


# Part 3: Write success state
old_report_return = """        if error_count:
            report.add_warning(f"{error_count} email(s) failed processing")

        return report"""

new_report_return = """        if error_count:
            report.add_warning(f"{error_count} email(s) failed processing")
        else:
            # If everything succeeded (no errors), update fetch state
            import json
            fetch_state_path = Path(self.settings.fetch_state_file)
            fetch_state_path.parent.mkdir(parents=True, exist_ok=True)
            fetch_state_path.write_text(
                json.dumps({"last_successful_fetch": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")}),
                encoding="utf-8"
            )

        return report"""

if old_fetch not in content:
    print("old_fetch not found!")
    sys.exit(1)

if old_gemini_processing not in content:
    print("old_gemini_processing not found!")
    sys.exit(1)

if old_report_return not in content:
    print("old_report_return not found!")
    sys.exit(1)

content = content.replace(old_fetch, new_fetch)
content = content.replace(old_gemini_processing, new_gemini_processing)
content = content.replace(old_report_return, new_report_return)

file_path.write_text(content, encoding="utf-8")
print("Patch applied successfully.")
