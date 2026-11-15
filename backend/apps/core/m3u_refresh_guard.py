"""Prevent replays of refresh POSTs whose admission cannot be established.

Dispatcharr's account status describes live imports. In particular, a terminal
live status does not establish completion of the separately queued VOD work.
An ambiguous start therefore needs an operator-confirmed resolution; neither a
timeout nor a stale/idle account snapshot is permission to submit it again.
"""

import hashlib
import json
import time
import uuid

import requests

from apps.database.repositories.m3u_refresh_repository import M3URefreshRepository


BUSY_STATUSES = frozenset({
    'building', 'downloading', 'fetching', 'importing', 'refreshing', 'in_progress',
    'loading', 'parsing', 'pending', 'preparing', 'processing', 'queued', 'running',
    'started', 'syncing', 'updating',
})
BUSY_KEYS = ('is_refreshing', 'refreshing', 'is_updating', 'updating', 'processing',
             'in_progress', 'task_running', 'queued', 'is_queued')


def account_is_busy(account):
    return any(
        account.get(key) is True or str(account.get(key)).strip().lower() in {'true', 'yes', '1', 'running'}
        for key in BUSY_KEYS
    ) or any(
        str(account.get(key, '')).strip().lower() in BUSY_STATUSES
        for key in ('status', 'state', 'refresh_status', 'last_refresh_status')
    )


class RefreshStartBlocked(requests.exceptions.RequestException):
    """A previous start may still be running; a new POST is not permitted."""


def accepted_existing_response():
    response = requests.Response()
    response.status_code = 202
    response._content = json.dumps({'success': True, 'already_running': True}).encode()
    return response


class M3URefreshGuard:
    def __init__(self, base_url, fetch_account, repository=None):
        self.scope = hashlib.sha256(base_url.rstrip('/').encode()).hexdigest()
        self.fetch_account = fetch_account
        self.repository = repository or M3URefreshRepository()

    def _account(self, account_id):
        account = self.fetch_account(account_id)
        if not isinstance(account, dict) or account.get('id') != account_id:
            raise RefreshStartBlocked(
                f'Could not verify current Dispatcharr status for M3U account {account_id}; refresh was not sent'
            )
        return account

    def start(self, account_id, send):
        attempt_id = str(uuid.uuid4())
        # Commit before sending: a crash at any later point leaves a durable guard.
        if not self.repository.claim(self.scope, account_id, attempt_id, time.time()):
            raise RefreshStartBlocked(
                f'M3U account {account_id} has an unresolved refresh start; no new POST was sent. '
                'Check live and VOD tasks in Dispatcharr, then resolve it under '
                'Settings -> Scheduling -> Playlist refresh protection.'
            )
        try:
            account = self._account(account_id)
            if account_is_busy(account):
                self.repository.release(self.scope, account_id, attempt_id)
                return accepted_existing_response()
        except Exception:
            # No start has been attempted yet; there is no ambiguous POST to protect.
            self.repository.release(self.scope, account_id, attempt_id)
            raise

        try:
            response = send()
        except requests.exceptions.ConnectTimeout:
            self.repository.release(self.scope, account_id, attempt_id)
            raise
        except requests.exceptions.HTTPError as exc:
            status = getattr(exc.response, 'status_code', None)
            if status is not None and 400 <= status < 500 and status != 408:
                self.repository.release(self.scope, account_id, attempt_id)
            else:
                self.repository.uncertain(self.scope, account_id, attempt_id)
            raise
        except BaseException:
            self.repository.uncertain(self.scope, account_id, attempt_id)
            raise
        # An explicit acceptance resolves admission uncertainty. The existing
        # refresh monitor owns completion; this is not a claim that VOD is done.
        self.repository.release(self.scope, account_id, attempt_id)
        return response

    def pending(self):
        result = []
        for attempt in self.repository.pending(self.scope):
            item = {key: value for key, value in attempt.items() if key != 'server_scope'}
            item['request_in_flight'] = attempt['state'] == 'sending' and time.time() - attempt['started_at'] < 180
            try:
                account = self._account(attempt['account_id'])
                item.update(name=account.get('name'), status=account.get('status'),
                            busy=account_is_busy(account), status_available=True)
            except requests.exceptions.RequestException:
                item.update(status_available=False, busy=None)
            result.append(item)
        return result

    def resolve(self, account_id, attempt_id, confirmed_finished):
        if confirmed_finished is not True:
            raise ValueError('Confirm that both live and VOD refresh tasks have finished or stopped in Dispatcharr')
        attempt = next((item for item in self.repository.pending(self.scope)
                        if item['account_id'] == account_id and item['attempt_id'] == attempt_id), None)
        if attempt is None:
            raise RefreshStartBlocked('This refresh attempt no longer exists; reload its status')
        if attempt['state'] == 'sending' and time.time() - attempt['started_at'] < 180:
            raise RefreshStartBlocked('The refresh start request may still be in flight; wait and reload its status')
        if account_is_busy(self._account(account_id)):
            raise RefreshStartBlocked('Dispatcharr still reports a running refresh; no protection was removed')
        if not self.repository.release(self.scope, account_id, attempt_id):
            raise RefreshStartBlocked('The refresh attempt changed; reload its status')
