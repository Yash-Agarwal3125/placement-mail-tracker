"""Shared "is this a transient network blip worth retrying" check.

Gmail fetch (scheduler/runner.py) and Google Calendar sync
(calendar_sync/client.py) both talk to Google APIs over the same kind of
HTTP transport and hit the same real-world failure shapes -- see the
2026-09-01 incident: an intermittent local TLS blip surfaces as
``google.auth.exceptions.TransportError`` (during an OAuth token refresh)
or an ``httplib2.HttpLib2Error``/socket-level ``OSError`` (during the API
call itself), essentially never as the ``googleapiclient`` ``HttpError``
429/5xx that both retry loops originally checked for exclusively (0 of 209
observed Gmail failures were ``HttpError`` at all).

Kept as one function so the two retry loops -- and Calendar's own token-
refresh path, which had no retry at all -- can't independently drift the
way scheduler.runner's and calendar_sync.client's copies of this check
already had before being unified here.
"""

from __future__ import annotations

import http.client

import httplib2
from google.auth.exceptions import TransportError
from googleapiclient.errors import HttpError

_RETRYABLE_HTTP_STATUSES = frozenset({429, 500, 502, 503, 504})


def is_transient_network_error(error: Exception) -> bool:
    """True when ``error`` is a transient network/API failure worth retrying.

    Does NOT account for "this token is dead, don't bother retrying" cases
    (``google.auth.exceptions.RefreshError``, this repo's own
    ``GmailAuthenticationError``/``CalendarAuthenticationError``) -- callers
    with those concepts should check for them first and return False before
    ever reaching this function.
    """
    if isinstance(error, HttpError):
        return error.resp.status in _RETRYABLE_HTTP_STATUSES
    if isinstance(error, TransportError):
        return True
    if isinstance(error, httplib2.HttpLib2Error):
        return True
    if isinstance(error, (OSError, http.client.HTTPException)):
        return True
    return False
