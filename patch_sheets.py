from pathlib import Path

path = Path("src/placement_mail_tracker/sheets/sheets_sync.py")
content = path.read_text(encoding="utf-8")

# 1. Update _sync_tab_data to use batchUpdate
sync_tab_old = """        rows_to_append = []
        for row in data_rows:
            if len(row) > key_index:
                key = row[key_index].strip()
                existing_row_number = existing_by_key.get(key)
                if existing_row_number:
                    values.update(
                        spreadsheetId=self.settings.google_sheet_id,
                        range=f"{_quote(tab_name)}!A{existing_row_number}:Z{existing_row_number}",
                        valueInputOption="USER_ENTERED",
                        body={"values": [row]},
                    ).execute()
                else:
                    rows_to_append.append(row)"""

sync_tab_new = """        rows_to_append = []
        update_data = []
        for row in data_rows:
            if len(row) > key_index:
                key = row[key_index].strip()
                existing_row_number = existing_by_key.get(key)
                if existing_row_number:
                    update_data.append({
                        "range": f"{_quote(tab_name)}!A{existing_row_number}:Z{existing_row_number}",
                        "values": [row]
                    })
                else:
                    rows_to_append.append(row)

        if update_data:
            values.batchUpdate(
                spreadsheetId=self.settings.google_sheet_id,
                body={
                    "valueInputOption": "USER_ENTERED",
                    "data": update_data
                }
            ).execute()"""

content = content.replace(sync_tab_old, sync_tab_new)

# 2. Rename sync_active_opportunities to _sync_active_opportunities_internal
sync_opp_old = """    def sync_active_opportunities(self, database: DatabaseManager) -> dict[str, int]:
        \"\"\"Sync all drives, companies, and dashboard to Google Sheets.\"\"\"
        if not self.settings.google_sheet_id:"""

sync_opp_new = """    def sync_active_opportunities(self, database: DatabaseManager) -> dict[str, int]:
        \"\"\"Sync all drives, companies, and dashboard to Google Sheets with resilience.\"\"\"
        backoffs = [2, 5, 10]
        for attempt, backoff in enumerate(backoffs + [0]):
            try:
                return self._sync_active_opportunities_internal(database)
            except Exception as e:
                is_retryable = False
                import socket
                import http.client
                from googleapiclient.errors import HttpError
                
                if isinstance(e, HttpError):
                    if e.resp.status in {429, 500, 502, 503, 504}:
                        is_retryable = True
                elif isinstance(e, (socket.error, socket.timeout, http.client.HTTPException, ConnectionError, TimeoutError)):
                    is_retryable = True
                    
                if is_retryable and attempt < len(backoffs):
                    logger.warning("Google Sheets network error: %s. Retrying in %ss...", e, backoff)
                    import time
                    time.sleep(backoff)
                else:
                    # Non-retryable or out of retries
                    if isinstance(e, HttpError):
                        self.last_error = str(e)
                        if self.settings.is_production:
                            raise
                        logger.exception("Unable to sync Google Sheet: %s", e)
                        return {"created": 0, "updated": 0, "skipped": 0}
                    elif isinstance(e, SheetsAuthenticationError):
                        self.last_error = str(e)
                        if self.settings.is_production:
                            raise e
                        logger.warning("%s", e)
                        return {"created": 0, "updated": 0, "skipped": 0}
                    else:
                        raise

    def _sync_active_opportunities_internal(self, database: DatabaseManager) -> dict[str, int]:
        \"\"\"Internal method to sync all drives, companies, and dashboard to Google Sheets.\"\"\"
        if not self.settings.google_sheet_id:"""

content = content.replace(sync_opp_old, sync_opp_new)

# Remove the old exception handling in _sync_active_opportunities_internal to avoid double handling
# The old one had:
exc_old = """        except SheetsAuthenticationError as error:
            self.last_error = str(error)
            if self.settings.is_production:
                raise
            logger.warning("%s", error)
            return {"created": 0, "updated": 0, "skipped": 0}
        except HttpError as error:
            self.last_error = str(error)
            if self.settings.is_production:
                raise
            logger.exception("Unable to sync Google Sheet: %s", error)
            return {"created": 0, "updated": 0, "skipped": 0}"""

exc_new = ""

content = content.replace(exc_old, exc_new)

path.write_text(content, encoding="utf-8")
print("Patched sheets_sync.py successfully.")
