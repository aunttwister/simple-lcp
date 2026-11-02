"""Cost tracking plugin system.

Provider-specific plugins for usage data, cost calculation, balance queries,
and local token tracking. Each plugin extends CostPlugin and registers itself
via the REGISTRY.

Included plugins:
  - deepseek     : Official DeepSeek API usage/balance queries
  - opencode     : Local SQLite reader for OpenCode's opencode.db
  - llamacpp     : Local tracking for self-hosted llama.cpp instances
  - commandcode  : Command Code billing API + gateway DB cost tracking
  - anthropic    : Anthropic (Claude) API — Messages wire, quick-add preset
"""

from .base import (
    CostPlugin,
    CostPluginsComponent,
    PluginRegistry,
    bind_runtime,
    get_registry,
)

# Import plugins to trigger registration
from . import deepseek
from . import opencode
from . import llamacpp
from . import commandcode
from . import anthropic

__all__ = [
    "CostPlugin",
    "CostPluginsComponent",
    "PluginRegistry",
    "bind_runtime",
    "get_registry",
]
