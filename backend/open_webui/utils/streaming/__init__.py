"""Streaming turn machinery, carved out of the middleware monolith.

Modules:
  accumulate — byte-exact growth of streamed text/reasoning content
  wire       — v2.1 delta translator + wire-size/coalescing constants
  blocks     — content-block inspection/finalization helpers

utils/middleware.py re-exports these names for backward compatibility;
new code should import from the specific module."""
