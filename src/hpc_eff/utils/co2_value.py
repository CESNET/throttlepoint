import requests
import statistics
from datetime import datetime, timedelta


def _nowtricity_url(config, endpoint):
    """Build Nowtricity API URL from config: BASE_URL/endpoint/ZONE/"""
    base = config.get('CO2_API', 'NOWTRICITY_BASE_URL',
                      fallback='https://www.nowtricity.com/api').rstrip('/')
    zone = config.get('CO2_API', 'NOWTRICITY_ZONE', fallback='czech-republic')
    return f'{base}/{endpoint}/{zone}/'


def fetch_emissions_data(api_headers, config):
    """
    Fetch emissions data for the past 24 hours from the Nowtricity API.

    Returns:
        list of int: A list of emission values (in g CO2eq/kWh).
    """
    url = _nowtricity_url(config, 'emissions-previous-24h')

    response = requests.get(url, headers=api_headers, timeout=10)
    response.raise_for_status()
    data = response.json()

    # Extract emission values
    return [entry['value'] for entry in data['emissions']]


def fetch_current_emission(api_headers, config):
    """
    Fetch the current emission value from the Nowtricity API.

    Returns:
        int: The current emission value (in g CO2eq/kWh).
    """
    url = _nowtricity_url(config, 'current-emissions')

    response = requests.get(url, headers=api_headers, timeout=10)
    response.raise_for_status()
    data = response.json()

    return data['emissions']['value']


def fetch_wattnet_24h(config):
    """
    Fetch emissions data for the past 24 hours from Wattnet API.

    Calls GET /v1/footprints with start/end parameters and parses
    the 15-minute interval data into 24 hourly averages.

    Config keys used from [CO2_API]:
      - WATTNET_URL: base URL (e.g. https://api.wattnet.eu/v1/footprints)
      - WATTNET_API_KEY: Bearer token
      - WATTNET_ZONE: default CZ
      - WATTNET_FOOTPRINT_TYPE: default carbon
      - WATTNET_SCOPE: default operational

    Returns:
        tuple: (hourly_values, current_hourly)
            - hourly_values: list of int, 24 hourly averages (newest first)
            - current_hourly: int, most recent hourly average (or None if no data)
    """
    base_url = config.get('CO2_API', 'WATTNET_URL', fallback='https://api.wattnet.eu/v1/footprints')
    api_key = config.get('CO2_API', 'WATTNET_API_KEY', fallback=None)
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

    headers = {
        'Accept': 'application/json'
    }
    if api_key and api_key != 'YOUR_API_KEY_HERE':
        headers['Authorization'] = f'Bearer {api_key}'

    response = requests.get(base_url, params=params, headers=headers, timeout=15)
    response.raise_for_status()
    data = response.json()

    # Parse wattnet structure: [{"series": [{"values": [[timestamp, value], ...]}]}]
    try:
        values_15min = data[0]['series'][0]['values']
    except (KeyError, IndexError, TypeError) as e:
        raise ValueError(
            f"Unexpected wattnet API structure: {e}. "
            f"Expected: data[0]['series'][0]['values'] = [[ts, value], ...]"
        )

    if not values_15min:
        raise ValueError("Wattnet API returned empty values array")

    # Sort by timestamp ascending (oldest first) for proper chunking
    values_15min.sort(key=lambda x: x[0])

    # Convert 15-minute intervals to hourly averages (4 values per hour)
    hourly_values = []
    for i in range(0, len(values_15min), 4):
        chunk = values_15min[i:i + 4]
        if chunk:
            avg = sum(float(v[1]) for v in chunk) / len(chunk)
            hourly_values.append(round(avg))

    # Reverse to newest first (to match Nowtricity order) and take last 24
    result = hourly_values[::-1][:24]

    # Most recent hourly average is the "current" value for wattnet
    # (consistent with nowtricity where current ≈ history[0]). Read it after
    # the reverse: before it, index 0 is the OLDEST hour.
    current_hourly = result[0] if result else None

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
    Main execution function - unified for both Nowtricity and Wattnet APIs.

    Args:
        config: configparser.ConfigParser instance with [CO2_API] section

    Returns:
        tuple: (historical_values, current_value, median_value, grade)
            - historical_values: list of 24 hourly values (newest first)
            - current_value: current CO2 value
            - median_value: median of historical_values
            - grade: 1-10 rating based on percentile
    """
    api_type = config.get('CO2_API', 'TYPE', fallback='nowtricity').strip().lower()

    if api_type == "wattnet":
        historical_values, current_hourly = fetch_wattnet_24h(config)
        # For current value, use the most recent hourly average
        # (consistent with nowtricity where current ≈ history[0])
        current_value = current_hourly if current_hourly is not None else historical_values[0]
    else:
        # Nowtricity: read credentials from [CO2_API]
        headers = {
            'User-Agent': config.get('CO2_API', 'NOWTRICITY_USER_AGENT', fallback='HPC-Eff-Agent'),
            'X-Api-Key': config.get('CO2_API', 'NOWTRICITY_API_KEY')
        }

        historical_values = fetch_emissions_data(headers, config)
        current_value = fetch_current_emission(headers, config)

    median_value = statistics.median(historical_values)
    grade = assign_grade(current_value, historical_values)

    return historical_values, current_value, median_value, grade
