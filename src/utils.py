import time, random
import requests

def get(url, params=None, headers=None, retries=6, timeout=30):
    """
    HTTP GET met robuuste retries en backoff, speciaal voor 429-rate limits.
    - Exponentiële backoff: 2, 4, 8, 16, 24, 32s (+ jitter)
    - Respecteert 'Retry-After' header als die wordt meegegeven.
    """
    for i in range(retries):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=timeout)
            # 2xx => klaar
            if 200 <= r.status_code < 300:
                ct = r.headers.get("Content-Type", "")
                return r.json() if "application/json" in ct else r.text

            # 429 => backoff
            if r.status_code == 429:
                # Respecteer Retry-After (indien aanwezig)
                retry_after = r.headers.get("Retry-After")
                if retry_after:
                    try:
                        wait = float(retry_after)
                    except:
                        wait = 2 ** (i + 1)
                else:
                    wait = 2 ** (i + 1)
                wait = wait + random.uniform(0, 0.5*i)  # kleine jitter
            else:
                # andere niet-2xx: korte backoff
                wait = min(2 ** (i + 1), 20) + random.uniform(0, 0.5*i)

            if i < retries - 1:
                time.sleep(wait)
                continue

            # retries op, gooi duidelijke fout
            raise RuntimeError(f"GET failed {url} -> {r.status_code}: {r.text[:200]}")
        except requests.RequestException as e:
            # netwerkfout: backoff en opnieuw
            if i < retries - 1:
                time.sleep(2 ** (i + 1) + random.uniform(0, 0.3*i))
                continue
            raise RuntimeError(f"GET exception {url}: {e}")
