"""OpenDota API client with retry logic and parse monitoring."""

import time
import logging
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)


class OpenDotaClient:
    """Client for interacting with the OpenDota API."""

    BASE_URL = "https://api.opendota.com/api"
    
    # Rate limit: 60 requests/min without key, 1200/min with key
    DEFAULT_DELAY = 1.0  # seconds between requests (safe for no-key usage)

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key
        self.session = self._create_session()
        self.last_request_time = 0.0

    def _create_session(self) -> requests.Session:
        """Create a session with retry logic."""
        session = requests.Session()
        
        # Retry on common transient errors
        retry_strategy = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "POST"],
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        
        return session

    def _rate_limit(self) -> None:
        """Enforce rate limiting between requests."""
        elapsed = time.time() - self.last_request_time
        delay = 0.5 if self.api_key else self.DEFAULT_DELAY
        if elapsed < delay:
            time.sleep(delay - elapsed)
        self.last_request_time = time.time()

    def _request(self, method: str, endpoint: str, **kwargs: Any) -> requests.Response:
        """Make a rate-limited request."""
        self._rate_limit()
        
        url = f"{self.BASE_URL}{endpoint}"
        params = kwargs.pop("params", {})
        if self.api_key:
            params["api_key"] = self.api_key
        
        response = self.session.request(method, url, params=params, **kwargs)
        
        if response.status_code == 429:
            # Rate limited - wait and retry once
            logger.warning("Rate limited, waiting 60 seconds...")
            time.sleep(60)
            response = self.session.request(method, url, params=params, **kwargs)
        
        return response

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
        if response.status_code == 200:
            data = response.json()
            job = data.get("job", {})
            job_id = job.get("jobId")
            if job_id:
                logger.info(f"Parse requested for match {match_id}, job_id: {job_id}")
                return job_id
        
        logger.warning(f"Failed to request parse for match {match_id}: {response.status_code}")
        return None

    def get_parse_status(self, job_id: int) -> bool:
        """
        Check if a parse job is complete.
        Returns True if complete, False if still pending.
        """
        response = self._request("GET", f"/request/{job_id}")
        # Job is complete when we get a 200 response
        # (the endpoint returns the job status or empty if done)
        return response.status_code == 200

    def poll_parse_completion(
        self, 
        job_id: int, 
        timeout: int = 300, 
        poll_interval: int = 10
    ) -> bool:
        """
        Poll until a parse job completes or timeout.
        
        Args:
            job_id: The parse job ID to monitor
            timeout: Maximum seconds to wait (default 5 minutes)
            poll_interval: Seconds between polls (default 10)
            
        Returns:
            True if parse completed, False if timed out
        """
        start = time.time()
        while time.time() - start < timeout:
            if self.get_parse_status(job_id):
                logger.info(f"Parse job {job_id} completed")
                return True
            logger.debug(f"Parse job {job_id} still pending, waiting...")
            time.sleep(poll_interval)
        
        logger.warning(f"Parse job {job_id} timed out after {timeout}s")
        return False

    def validate_player(self, player_id: int) -> bool:
        """Check if a player ID is valid."""
        player = self.get_player(player_id)
        return player is not None and "profile" in player
