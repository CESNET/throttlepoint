import base64
import json
import os
import requests
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path

WATTNET_TOKEN_URL = 'https://api.wattnet.eu/token-request/get_token'
WATTNET_TOKEN_CACHE = '/var/lib/hpc_eff/wattnet_token.json'
# Refresh this long before expiry so a token never dies mid-request
TOKEN_REFRESH_MARGIN = timedelta(minutes=10)


def _jwt_expiry(token):
    """The `exp` claim of a JWT as an aware datetime, or None. Not verified: only used for caching."""
    try:
        payload = token.split('.')[1]
        payload += '=' * (-len(payload) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload))['exp']
        return datetime.fromtimestamp(exp, timezone.utc)
    except Exception:
        return None


def _parse_expiry(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _request_wattnet_token(config, email, password):
    """Exchange email/password for a 24 h token at the Wattnet token-request service."""
    url = config.get('CO2_API', 'WATTNET_TOKEN_URL', fallback=WATTNET_TOKEN_URL)
    response = requests.post(url, json={'email': email, 'password': password}, timeout=15)
    if response.status_code == 401:
        raise ValueError("Wattnet login rejected: check WATTNET_EMAIL / WATTNET_PASSWORD")
    response.raise_for_status()
    data = response.json()
    token = (data.get('access_token') or data.get('token')) if isinstance(data, dict) else None
    if not token:
        raise ValueError(f"Wattnet token response has no token field (keys: {list(data) if isinstance(data, dict) else type(data).__name__})")
    expires_at = _parse_expiry(data.get('expires_at')) or _jwt_expiry(token) \
        or datetime.now(timezone.utc) + timedelta(hours=23)
    return token, expires_at


def _wattnet_token(config, force_refresh=False):
    """Bearer token for Wattnet.

    Wattnet has no long-lived API keys: tokens last 24 h and cannot be
    refreshed. The token is requested with WATTNET_EMAIL + WATTNET_PASSWORD and
    cached in WATTNET_TOKEN_CACHE (mode 600) until shortly before it expires,
    as Wattnet asks.
    """
    # raw=True: ConfigParser would otherwise treat '%' in a password as interpolation
    email = config.get('CO2_API', 'WATTNET_EMAIL', fallback='', raw=True).strip()
    password = config.get('CO2_API', 'WATTNET_PASSWORD', fallback='', raw=True).strip()
    if not (email and password):
        raise ValueError("Wattnet needs WATTNET_EMAIL and WATTNET_PASSWORD in [CO2_API]")

    cache = Path(config.get('CO2_API', 'WATTNET_TOKEN_CACHE', fallback=WATTNET_TOKEN_CACHE))
    if not force_refresh:
        try:
            cached = json.loads(cache.read_text())
            expires_at = _parse_expiry(cached.get('expires_at'))
            if cached.get('email') == email and expires_at \
                    and expires_at - TOKEN_REFRESH_MARGIN > datetime.now(timezone.utc):
                return cached['token']
        except Exception:
            pass  # missing or unreadable cache: request a new token

    token, expires_at = _request_wattnet_token(config, email, password)
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache.with_suffix('.tmp')
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump({'email': email, 'token': token, 'expires_at': expires_at.isoformat()}, f)
        os.replace(tmp, cache)
    except OSError:
        pass  # not cacheable (e.g. not root): still usable for this run
    return token


def fetch_wattnet_24h(config):
    """
    Fetch emissions data for the past 24 hours from Wattnet API.

    Calls GET /v1/footprints with start/end parameters and parses
    the 15-minute interval data into 24 hourly averages.

    Config keys used from [CO2_API]:
      - WATTNET_URL: base URL (e.g. https://api.wattnet.eu/v1/footprints)
      - WATTNET_EMAIL / WATTNET_PASSWORD: login, token fetched and cached
      - WATTNET_ZONE: default CZ
      - WATTNET_FOOTPRINT_TYPE: default carbon
      - WATTNET_SCOPE: default operational

    Returns:
        tuple: (hourly_values, current_hourly)
            - hourly_values: list of int, 24 hourly averages (newest first)
            - current_hourly: int, most recent hourly average (or None if no data)
    """
    base_url = config.get('CO2_API', 'WATTNET_URL', fallback='https://api.wattnet.eu/v1/footprints')
    zone = config.get('CO2_API', 'WATTNET_ZONE', fallback='CZ')
    footprint_type = config.get('CO2_API', 'WATTNET_FOOTPRINT_TYPE', fallback='carbon')
    scope = config.get('CO2_API', 'WATTNET_SCOPE', fallback='operational')

    # Calculate time window: previous 24 hours (UTC)
    end_time = datetime.utcnow()
    start_time = end_time - timedelta(hours=24)

    # Format as ISO 8601 with Z suffix (Wattnet expects this format)
    iso_format = "%Y-%m-%dT%H:%M:%SZ"
    params = {
        'zone': zone,
        'footprint_type': footprint_type,
        'scope': scope,
        'start': start_time.strftime(iso_format),
        'end': end_time.strftime(iso_format),
        'aggregate': 'false',
        'use_global': 'false'
    }

    def get(token):
        headers = {'Accept': 'application/json', 'Authorization': f'Bearer {token}'}
        # Redirects are not followed: a rejected token is answered with a 302 to
        # the Wattnet login page (not the 401 its docs describe), and following
        # it would return that HTML page with status 200.
        return requests.get(base_url, params=params, headers=headers, timeout=15,
                            allow_redirects=False)

    def rejected(response):
        return response.status_code == 401 or response.is_redirect

    response = get(_wattnet_token(config))
    if rejected(response):
        # Cached token revoked, or expired before its stated expiry: log in again once
        response = get(_wattnet_token(config, force_refresh=True))
        if rejected(response):
            raise ValueError("Wattnet rejected a freshly issued token "
                             f"(HTTP {response.status_code}); check the account's API access")
    response.raise_for_status()
    try:
        data = response.json()
    except ValueError:
        raise ValueError(f"Wattnet returned non-JSON data ({response.headers.get('content-type')})")

    # Parse wattnet structure: [{"series": [{"valid": bool, "zone_status": str,
    # "values": [[timestamp, value], ...]}, ...]}]. Series split one time range by
    # data state: final (valid) data ends ~4 h ago, newer data comes in pending
    # and preview series. All are merged so the newest point is "now"; pending
    # values may still be revised slightly by Wattnet.
    try:
        series = data[0]['series']
        merged = {}
        for entry in series:
            for ts, value in entry['values']:
                if value is None:
                    continue
                # On overlap, final data wins over pending/preview
                if ts not in merged or entry.get('valid', True):
                    merged[ts] = float(value)
    except (KeyError, IndexError, TypeError, ValueError) as e:
        raise ValueError(
            f"Unexpected wattnet API structure: {e}. "
            f"Expected: data[0]['series'][i]['values'] = [[ts, value], ...]"
        )

    if not merged:
        raise ValueError("Wattnet API returned empty values array")

    # Average the 15-minute points per clock hour ("2026-10-13T12" -> mean)
    by_hour = {}
    for ts, value in merged.items():
        by_hour.setdefault(ts[:13], []).append(value)

    # Newest clock hour first, last 24 hours (the 24 h history for grading)
    result = [round(sum(v) / len(v)) for _, v in sorted(by_hour.items(), reverse=True)][:24]

    # "Current" is the trailing 1 h average: the newest four 15-minute points,
    # the signal the GreenDIGIT threshold simulator uses. Unlike the newest
    # clock hour it never shrinks to a single point just after the hour.
    newest = [merged[ts] for ts in sorted(merged)[-4:]]
    current_hourly = round(sum(newest) / len(newest), 1)

    if len(result) < 24:
        raise ValueError(f"Wattnet API returned only {len(result)}/24 hourly values")

    return result, current_hourly


def assign_grade(current_value, historical_values):
    """
    Assign a grade (1 to 10) based on the position of the current value
    within the distribution of historical values.

    Args:
        current_value (int|float): The current emission value.
        historical_values (list of int|float): Historical emission values.

    Returns:
        int: Grade from 1 (low) to 10 (high).
    """
    if not historical_values:
        raise ValueError("Cannot assign CO2 grade: empty history")

    sorted_values = sorted(historical_values)
    position = sum(1 for v in sorted_values if v < current_value)
    percentile = position / len(sorted_values)

    # Scale percentile to 1–10 range
    return round(percentile * 9) + 1


def co2_value(config):
    """
    CO2 intensity from Wattnet, graded against the last 24 hours.

    Args:
        config: configparser.ConfigParser instance with [CO2_API] section

    Returns:
        tuple: (historical_values, current_value, median_value, grade)
            - historical_values: list of 24 hourly values (newest first)
            - current_value: current CO2 value
            - median_value: median of historical_values
            - grade: 1-10 rating based on percentile
    """
    # Wattnet is the only backend. An old config still naming another one
    # (e.g. TYPE=nowtricity, kept as a conffile across upgrades) fails loudly
    # instead of silently switching source.
    api_type = config.get('CO2_API', 'TYPE', fallback='wattnet').strip().lower()
    if api_type != 'wattnet':
        raise ValueError(f"[CO2_API] TYPE={api_type} is no longer supported: Nowtricity was "
                         "removed, set WATTNET_EMAIL and WATTNET_PASSWORD and drop TYPE")

    historical_values, current_hourly = fetch_wattnet_24h(config)
    # The most recent hourly average is the "current" value
    current_value = current_hourly if current_hourly is not None else historical_values[0]

    median_value = statistics.median(historical_values)
    grade = assign_grade(current_value, historical_values)

    return historical_values, current_value, median_value, grade
