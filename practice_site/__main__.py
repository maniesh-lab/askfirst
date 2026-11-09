"""Run the practice site: `python -m practice_site` (port 8800 by default)."""

import uvicorn

from practice_site.app import create_app
from practice_site.config import get_practice_settings

if __name__ == "__main__":
    settings = get_practice_settings()
    # proxy_headers: on Render the site sits behind a proxy that sets X-Forwarded-*.
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, proxy_headers=True)
