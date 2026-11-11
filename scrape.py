import sys

ALLOWED_DOMAINS = []
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)

if __name__ == "__main__":
    from mcp_server import _extract_text
    print(_extract_text(sys.argv[1], max_chars=8000, as_markdown=True))
