"""OpenDota API client with retry logic and parse monitoring."""

import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlencode

logger = logging.getLogger(__name__)

USER_AGENT = "dota-replays/1.0"


class ApiResponse:
    """An HTTP answer holding the status and body the client's callers read."""

    def __init__(self, status_code: int, body: bytes):
        self.status_code = status_code
        self.body = body

    def json(self) -> Any:
        """Decode the body as JSON."""
        return json.loads(self.body.decode("utf-8"))


class OpenDotaClient:
    """Client for interacting with the OpenDota API."""

    BASE_URL = "https://api.opendota.com/api"

    # Rate limit: 60 requests/min without key, 1200/min with key
    DEFAULT_DELAY = 1.0  # seconds between requests (safe for no-key usage)
    KEYED_DELAY = 0.5

    # Every answer waits at most this long; a retried request is bounded by
    # 4 attempts x 30 s plus the 1, 2 and 4 s backoff.
    REQUEST_TIMEOUT = 30
    RETRY_STATUSES = (429, 500, 502, 503, 504)
    RETRY_DELAYS = (1, 2, 4)
    RATE_LIMIT_WAIT = 60

    def __init__(self, api_key: str | None = None, base_url: str | None = None):
        self.api_key = api_key
        self.base_url = (base_url or self.BASE_URL).rstrip("/")
        self.last_request_time = 0.0

    def _rate_limit(self) -> None:
        """Enforce rate limiting between requests."""
        elapsed = time.time() - self.last_request_time
        delay = self.KEYED_DELAY if self.api_key else self.DEFAULT_DELAY
        if elapsed < delay:
            time.sleep(delay - elapsed)
        self.last_request_time = time.time()

    def _request(
        self, method: str, endpoint: str, params: dict | None = None
    ) -> ApiResponse:
        """Make a rate-limited request, retrying 429 and 5xx answers."""
        query = dict(params or {})
        if self.api_key:
            query["api_key"] = self.api_key

        url = f"{self.base_url}{endpoint}"
        if query:
            url = f"{url}?{urlencode(query)}"

        response = self._request_with_retries(method, endpoint, url)
        if response.status_code == 429:
            # Still rate limited after the retries - wait and retry once.
            logger.warning(f"Rate limited, waiting {self.RATE_LIMIT_WAIT} seconds...")
            time.sleep(self.RATE_LIMIT_WAIT)
            response = self._request_with_retries(method, endpoint, url)

        return response

    def _request_with_retries(self, method: str, endpoint: str, url: str) -> ApiResponse:
        """Attempt one request up to four times, backing off on 429 and 5xx."""
        attempts = len(self.RETRY_DELAYS) + 1
        for attempt in range(attempts):
            if attempt:
                time.sleep(self.RETRY_DELAYS[attempt - 1])
            self._rate_limit()
            try:
                return self._open(method, url)
            except urllib.error.HTTPError as error:
                body = error.read() or b""
                if error.code not in self.RETRY_STATUSES or attempt == attempts - 1:
                    return ApiResponse(error.code, body)
                logger.warning(f"{method} {endpoint} answered {error.code}, retrying...")
            except urllib.error.URLError as error:
                if attempt == attempts - 1:
                    raise
                logger.warning(f"{method} {endpoint} failed ({error.reason}), retrying...")

        raise RuntimeError(f"{method} {endpoint} failed after {attempts} attempts")

    def _open(self, method: str, url: str) -> ApiResponse:
        """Send one request and read the whole body."""
        request = urllib.request.Request(
            url,
            method=method,
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
        )
        with urllib.request.urlopen(request, timeout=self.REQUEST_TIMEOUT) as response:
            return ApiResponse(response.status, response.read())

    def get_player(self, player_id: int) -> dict | None:
        """Get player profile data."""
        response = self._request("GET", f"/players/{player_id}")
        if response.status_code == 200:
            data = response.json()
            if "profile" in data:
                return data
        return None

    def get_player_matches(self, player_id: int, limit: int | None = None) -> list[dict]:
        """Get matches for a player."""
        params = {}
        if limit:
            params["limit"] = limit

        response = self._request("GET", f"/players/{player_id}/matches", params=params)
        if response.status_code == 200:
            return response.json()

        logger.error(f"Failed to get matches for player {player_id}: {response.status_code}")
        return []

    def get_match_details(self, match_id: int) -> dict | None:
        """Get detailed match data."""
        response = self._request("GET", f"/matches/{match_id}")
        if response.status_code == 200:
            return response.json()

        logger.error(f"Failed to get match {match_id}: {response.status_code}")
        return None

    def request_parse(self, match_id: int) -> int | None:
        """
        Submit a parse request for a match.
        Returns job_id if successful, None otherwise.
        Note: This counts as 10 API calls for rate limiting.
        """
        response = self._request("POST", f"/request/{match_id}")

        # Parse requests count as 10 calls - add extra delay
        delay = 5.0 if self.api_key else 10.0  # 10 calls worth of delay
        time.sleep(delay)

        if response.status_code == 200:
            data = response.json()
            job = data.get("job", {})
            job_id = job.get("jobId")
            if job_id:
                logger.info(f"Parse requested for match {match_id}, job_id: {job_id}")
                return job_id

        logger.warning(f"Failed to request parse for match {match_id}: {response.status_code}")
        return None

    def get_parse_status(self, job_id: int) -> str | None:
        """
        Check the status of a parse job.
        Returns "completed", "failed", "pending", or None if request failed.
        """
        response = self._request("GET", f"/request/{job_id}")
        if response.status_code == 200:
            # If the job is still pending, it returns job info
            # If it's completed, it might return empty or complete info
            data = response.json()
            if not data:
                return "completed"

            # Check state if present
            # Typical OpenDota job response: {"jobId": "...", "state": "active", ...}
            # Or it might just return null/empty when finished.
            # Based on docs: "The response will tell you if the job is 'pending', 'failed', or 'completed'."
            return data.get("state") or "completed"

        return None

    def poll_parse_completion(
        self,
        job_id: int,
        timeout: int = 300,
        poll_interval: int = 20
    ) -> bool:
        """
        Poll until a parse job completes or timeout.

        Args:
            job_id: The parse job ID to monitor
            timeout: Maximum seconds to wait (default 5 minutes)
            poll_interval: Seconds between polls (default 20)

        Returns:
            True if parse completed, False if timed out or failed
        """
        start = time.time()
        while time.time() - start < timeout:
            status = self.get_parse_status(job_id)
            if status == "completed":
                logger.info(f"Parse job {job_id} completed")
                return True
            elif status == "failed":
                logger.warning(f"Parse job {job_id} failed")
                return False

            logger.info(f"Parse job {job_id} status: {status}, waiting...")
            time.sleep(poll_interval)

        logger.warning(f"Parse job {job_id} timed out after {timeout}s")
        return False

    def validate_player(self, player_id: int) -> bool:
        """Check if a player ID is valid."""
        player = self.get_player(player_id)
        return player is not None and "profile" in player
