"""LLM Gateway V3 client package marker.

Originally this directory was meant to be imported flat (the gateway
itself runs from inside it: `from client import LLM`). The agent
code imports it as a package — `from llm_gatewayV3.client import LLM`
— which requires this file to exist.

The gateway's run.sh stays unchanged; it sets cwd before importing.
"""
