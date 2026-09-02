"""MCP portal: hashed bearer keys, the activity log, and the JSON-RPC data plane.

The control plane (key minting/revocation) is ordinary DRF; the data plane is a
FastAPI application in `endpoint.py` that the project's ASGI entrypoint mounts.
"""
