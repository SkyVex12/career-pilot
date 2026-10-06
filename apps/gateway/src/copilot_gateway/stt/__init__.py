"""Streaming STT behind one STTStream interface (ARCHITECTURE.md, Gateway internals)."""

from copilot_gateway.stt.base import STTFactory, STTResult, STTStream
from copilot_gateway.stt.resilient import ResilientSTT, TimedResult

__all__ = ["ResilientSTT", "STTFactory", "STTResult", "STTStream", "TimedResult"]
