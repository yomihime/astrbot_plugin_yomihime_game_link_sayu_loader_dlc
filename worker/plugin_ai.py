"""GsCore plugin declarations when its AI switch is off and no vector backend exists."""

from dataclasses import dataclass, field
from typing import Any, NotRequired, TypedDict

from port_errors import Unsupported


@dataclass
class ToolContext:
    bot: Any = None
    ev: Any = None
    extra: dict = field(default_factory=dict)
    parent_session_id: str | None = None
    dynamic_tool_names: set[str] = field(default_factory=set)
    blocked_tool_names: set[str] = field(default_factory=set)
    alias_callable_tools: set[str] = field(default_factory=set)
    allow_user_outbound: bool = True
    tool_scope: Any = None


class KnowledgeBase(TypedDict):
    id: str
    plugin: str
    title: str
    content: str
    tags: list[str]
    source: NotRequired[str]
    entity: NotRequired[str]


class KnowledgePoint(KnowledgeBase):
    _hash: NotRequired[str]


class ImageEntity(TypedDict):
    id: str
    plugin: str
    path: str
    tags: list[str]
    content: str
    source: str
    _hash: str | None


class DisabledAI:
    def get_config(self, key):
        from gsuid_core.utils.plugins_config.models import GsBoolConfig

        if key != "enable":
            raise Unsupported("AI backend configuration is unavailable")
        return GsBoolConfig("是否启用AI服务", "指定是否启用AI服务", False)

    def set_config(self, *args, **kwargs):
        raise Unsupported("AI configuration requires an enabled AI backend")


def ai_tools(func=None, /, **options):
    def decorate(fn):
        return fn

    return decorate if func is None else decorate(func)


def ai_entity(entity):
    return None


def ai_image(entity):
    return None


def ai_alias(name, alias, scope="global"):
    return None


async def sync_knowledge():
    return None


async def query_knowledge(
    query,
    limit=5,
    plugin_filter=None,
    category_filter=None,
    exclude_plugins=None,
    exclude_sources=None,
):
    return []


def install_ai(package):
    package("gsuid_core.ai_core")
    package("gsuid_core.ai_core.configs")
    package("gsuid_core.ai_core.configs.ai_config").ai_config = DisabledAI()
    models = package("gsuid_core.ai_core.models")
    for model in (ToolContext, KnowledgeBase, KnowledgePoint, ImageEntity):
        setattr(models, model.__name__, model)
    register = package("gsuid_core.ai_core.register")
    register._ENTITIES = []
    register._IMAGE_ENTITIES = []
    register._ALIASES = {}
    register._TOOL_REGISTRY = {}
    register.get_registered_tools = lambda: register._TOOL_REGISTRY
    register.get_all_tools = lambda: {
        name: tool
        for group in register._TOOL_REGISTRY.values()
        for name, tool in group.items()
    }
    register.get_aliases_for_scope = lambda scope="global": {
        key: list(
            dict.fromkeys(
                register._ALIASES.get("global", {}).get(key, [])
                + register._ALIASES.get(scope, {}).get(key, [])
            )
        )
        for key in register._ALIASES.get("global", {}).keys()
        | register._ALIASES.get(scope, {}).keys()
    }
    for fn in (ai_tools, ai_alias, ai_image, ai_entity):
        setattr(register, fn.__name__, fn)
    rag = package("gsuid_core.ai_core.rag")
    rag.query_knowledge = query_knowledge
    knowledge = package("gsuid_core.ai_core.rag.knowledge")
    knowledge.sync_knowledge = sync_knowledge
    knowledge.query_knowledge = query_knowledge
