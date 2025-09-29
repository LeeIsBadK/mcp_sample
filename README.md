- tab 1
    `uv run <your-mcp-server.py>`
- tab 2
    `uvx mcpo --port 8000 --host 127.0.0.1 --server-type "streamable-http"  -- http://127.0.0.1:9000/mcp`
- tab 3
    `open-webui serve`
- tab 4
    `npx @modelcontextprotocol/inspector`
- tab 5 (optional, for external access)
    `ngrok http 8000`