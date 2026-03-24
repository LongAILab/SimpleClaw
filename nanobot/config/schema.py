"""Configuration schema using Pydantic."""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel
from pydantic_settings import BaseSettings


class Base(BaseModel):
    """Base model that accepts both camelCase and snake_case keys."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class WhatsAppConfig(Base):
    """WhatsApp channel configuration."""

    enabled: bool = False
    bridge_url: str = "ws://localhost:3001"
    bridge_token: str = ""  # Shared token for bridge auth (optional, recommended)
    allow_from: list[str] = Field(default_factory=list)  # Allowed phone numbers


class TelegramConfig(Base):
    """Telegram channel configuration."""

    enabled: bool = False
    token: str = ""  # Bot token from @BotFather
    allow_from: list[str] = Field(default_factory=list)  # Allowed user IDs or usernames
    proxy: str | None = (
        None  # HTTP/SOCKS5 proxy URL, e.g. "http://127.0.0.1:7890" or "socks5://127.0.0.1:1080"
    )
    reply_to_message: bool = False  # If true, bot replies quote the original message
    group_policy: Literal["open", "mention"] = "mention"  # "mention" responds when @mentioned or replied to, "open" responds to all


class FeishuConfig(Base):
    """Feishu/Lark channel configuration using WebSocket long connection."""

    enabled: bool = False
    app_id: str = ""  # App ID from Feishu Open Platform
    app_secret: str = ""  # App Secret from Feishu Open Platform
    encrypt_key: str = ""  # Encrypt Key for event subscription (optional)
    verification_token: str = ""  # Verification Token for event subscription (optional)
    allow_from: list[str] = Field(default_factory=list)  # Allowed user open_ids
    react_emoji: str = (
        "THUMBSUP"  # Emoji type for message reactions (e.g. THUMBSUP, OK, DONE, SMILE)
    )
    group_policy: Literal["open", "mention"] = "mention"  # "mention" responds when @mentioned, "open" responds to all


class DingTalkConfig(Base):
    """DingTalk channel configuration using Stream mode."""

    enabled: bool = False
    client_id: str = ""  # AppKey
    client_secret: str = ""  # AppSecret
    allow_from: list[str] = Field(default_factory=list)  # Allowed staff_ids


class DiscordConfig(Base):
    """Discord channel configuration."""

    enabled: bool = False
    token: str = ""  # Bot token from Discord Developer Portal
    allow_from: list[str] = Field(default_factory=list)  # Allowed user IDs
    gateway_url: str = "wss://gateway.discord.gg/?v=10&encoding=json"
    intents: int = 37377  # GUILDS + GUILD_MESSAGES + DIRECT_MESSAGES + MESSAGE_CONTENT
    group_policy: Literal["mention", "open"] = "mention"


class MatrixConfig(Base):
    """Matrix (Element) channel configuration."""

    enabled: bool = False
    homeserver: str = "https://matrix.org"
    access_token: str = ""
    user_id: str = ""  # @bot:matrix.org
    device_id: str = ""
    e2ee_enabled: bool = True  # Enable Matrix E2EE support (encryption + encrypted room handling).
    sync_stop_grace_seconds: int = (
        2  # Max seconds to wait for sync_forever to stop gracefully before cancellation fallback.
    )
    max_media_bytes: int = (
        20 * 1024 * 1024
    )  # Max attachment size accepted for Matrix media handling (inbound + outbound).
    allow_from: list[str] = Field(default_factory=list)
    group_policy: Literal["open", "mention", "allowlist"] = "open"
    group_allow_from: list[str] = Field(default_factory=list)
    allow_room_mentions: bool = False


class EmailConfig(Base):
    """Email channel configuration (IMAP inbound + SMTP outbound)."""

    enabled: bool = False
    consent_granted: bool = False  # Explicit owner permission to access mailbox data

    # IMAP (receive)
    imap_host: str = ""
    imap_port: int = 993
    imap_username: str = ""
    imap_password: str = ""
    imap_mailbox: str = "INBOX"
    imap_use_ssl: bool = True

    # SMTP (send)
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = True
    smtp_use_ssl: bool = False
    from_address: str = ""

    # Behavior
    auto_reply_enabled: bool = (
        True  # If false, inbound email is read but no automatic reply is sent
    )
    poll_interval_seconds: int = 30
    mark_seen: bool = True
    max_body_chars: int = 12000
    subject_prefix: str = "Re: "
    allow_from: list[str] = Field(default_factory=list)  # Allowed sender email addresses


class MochatMentionConfig(Base):
    """Mochat mention behavior configuration."""

    require_in_groups: bool = False


class MochatGroupRule(Base):
    """Mochat per-group mention requirement."""

    require_mention: bool = False


class MochatConfig(Base):
    """Mochat channel configuration."""

    enabled: bool = False
    base_url: str = "https://mochat.io"
    socket_url: str = ""
    socket_path: str = "/socket.io"
    socket_disable_msgpack: bool = False
    socket_reconnect_delay_ms: int = 1000
    socket_max_reconnect_delay_ms: int = 10000
    socket_connect_timeout_ms: int = 10000
    refresh_interval_ms: int = 30000
    watch_timeout_ms: int = 25000
    watch_limit: int = 100
    retry_delay_ms: int = 500
    max_retry_attempts: int = 0  # 0 means unlimited retries
    claw_token: str = ""
    agent_user_id: str = ""
    sessions: list[str] = Field(default_factory=list)
    panels: list[str] = Field(default_factory=list)
    allow_from: list[str] = Field(default_factory=list)
    mention: MochatMentionConfig = Field(default_factory=MochatMentionConfig)
    groups: dict[str, MochatGroupRule] = Field(default_factory=dict)
    reply_delay_mode: str = "non-mention"  # off | non-mention
    reply_delay_ms: int = 120000


class SlackDMConfig(Base):
    """Slack DM policy configuration."""

    enabled: bool = True
    policy: str = "open"  # "open" or "allowlist"
    allow_from: list[str] = Field(default_factory=list)  # Allowed Slack user IDs


class SlackConfig(Base):
    """Slack channel configuration."""

    enabled: bool = False
    mode: str = "socket"  # "socket" supported
    webhook_path: str = "/slack/events"
    bot_token: str = ""  # xoxb-...
    app_token: str = ""  # xapp-...
    user_token_read_only: bool = True
    reply_in_thread: bool = True
    react_emoji: str = "eyes"
    allow_from: list[str] = Field(default_factory=list)  # Allowed Slack user IDs (sender-level)
    group_policy: str = "mention"  # "mention", "open", "allowlist"
    group_allow_from: list[str] = Field(default_factory=list)  # Allowed channel IDs if allowlist
    dm: SlackDMConfig = Field(default_factory=SlackDMConfig)


class QQConfig(Base):
    """QQ channel configuration using botpy SDK."""

    enabled: bool = False
    app_id: str = ""  # 机器人 ID (AppID) from q.qq.com
    secret: str = ""  # 机器人密钥 (AppSecret) from q.qq.com
    allow_from: list[str] = Field(
        default_factory=list
    )  # Allowed user openids (empty = public access)


class WecomConfig(Base):
    """WeCom (Enterprise WeChat) AI Bot channel configuration."""

    enabled: bool = False
    bot_id: str = ""  # Bot ID from WeCom AI Bot platform
    secret: str = ""  # Bot Secret from WeCom AI Bot platform
    allow_from: list[str] = Field(default_factory=list)  # Allowed user IDs
    welcome_message: str = ""  # Welcome message for enter_chat event


class ChannelsConfig(Base):
    """Configuration for chat channels."""

    send_progress: bool = True  # stream agent's text progress to the channel
    send_tool_hints: bool = False  # stream tool-call hints (e.g. read_file("…"))
    whatsapp: WhatsAppConfig = Field(default_factory=WhatsAppConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    discord: DiscordConfig = Field(default_factory=DiscordConfig)
    feishu: FeishuConfig = Field(default_factory=FeishuConfig)
    mochat: MochatConfig = Field(default_factory=MochatConfig)
    dingtalk: DingTalkConfig = Field(default_factory=DingTalkConfig)
    email: EmailConfig = Field(default_factory=EmailConfig)
    slack: SlackConfig = Field(default_factory=SlackConfig)
    qq: QQConfig = Field(default_factory=QQConfig)
    matrix: MatrixConfig = Field(default_factory=MatrixConfig)
    wecom: WecomConfig = Field(default_factory=WecomConfig)


class AgentDefaults(Base):
    """Default agent configuration."""

    workspace: str = "~/.nanobot/workspace"
    model: str = "anthropic/claude-opus-4-5"
    provider: str = (
        "auto"  # Provider name (e.g. "anthropic", "openrouter") or "auto" for auto-detection
    )
    max_tokens: int = 8192
    context_window_tokens: int = 65_536
    temperature: float = 0.1
    max_tool_iterations: int = 40
    thinking: bool | None = False  # Explicit thinking toggle for providers that support it
    responses_prefix_cache: bool = False
    # Deprecated compatibility field: accepted from old configs but ignored at runtime.
    memory_window: int | None = Field(default=None, exclude=True)
    reasoning_effort: str | None = None  # low / medium / high for providers with reasoning effort knobs

    @property
    def should_warn_deprecated_memory_window(self) -> bool:
        """Return True when old memoryWindow is present without contextWindowTokens."""
        return self.memory_window is not None and "context_window_tokens" not in self.model_fields_set


CronExecutionPolicy = Literal["isolated-per-job", "isolated-per-run", "reuse-origin-session"]


class CronAgentConfig(Base):
    """Cron/background execution configuration."""

    enabled: bool = True
    model: str | None = None
    provider: str | None = None
    max_tokens: int | None = None
    context_window_tokens: int | None = None
    temperature: float | None = None
    max_tool_iterations: int | None = None
    thinking: bool | None = None
    responses_prefix_cache: bool | None = None
    reasoning_effort: str | None = None
    execution_policy: CronExecutionPolicy = "isolated-per-job"
    deliver_default: bool = True

    def resolve(self, defaults: AgentDefaults) -> AgentDefaults:
        """Resolve cron settings by falling back to the main agent defaults."""
        return AgentDefaults(
            workspace=defaults.workspace,
            model=self.model or defaults.model,
            provider=self.provider or defaults.provider,
            max_tokens=self.max_tokens if self.max_tokens is not None else defaults.max_tokens,
            context_window_tokens=(
                self.context_window_tokens
                if self.context_window_tokens is not None
                else defaults.context_window_tokens
            ),
            temperature=self.temperature if self.temperature is not None else defaults.temperature,
            max_tool_iterations=(
                self.max_tool_iterations
                if self.max_tool_iterations is not None
                else defaults.max_tool_iterations
            ),
            thinking=self.thinking if self.thinking is not None else defaults.thinking,
            responses_prefix_cache=(
                self.responses_prefix_cache
                if self.responses_prefix_cache is not None
                else defaults.responses_prefix_cache
            ),
            reasoning_effort=(
                self.reasoning_effort
                if self.reasoning_effort is not None
                else defaults.reasoning_effort
            ),
        )


class HeartbeatAgentConfig(Base):
    """Heartbeat decider configuration."""

    enabled: bool = True
    model: str | None = None
    provider: str | None = None
    max_tokens: int | None = None
    context_window_tokens: int | None = None
    temperature: float | None = None
    max_tool_iterations: int | None = None
    thinking: bool | None = None
    responses_prefix_cache: bool | None = None
    reasoning_effort: str | None = None

    def resolve(self, defaults: AgentDefaults) -> AgentDefaults:
        """Resolve heartbeat settings by falling back to the main agent defaults."""
        return AgentDefaults(
            workspace=defaults.workspace,
            model=self.model or defaults.model,
            provider=self.provider or defaults.provider,
            max_tokens=self.max_tokens if self.max_tokens is not None else defaults.max_tokens,
            context_window_tokens=(
                self.context_window_tokens
                if self.context_window_tokens is not None
                else defaults.context_window_tokens
            ),
            temperature=self.temperature if self.temperature is not None else defaults.temperature,
            max_tool_iterations=(
                self.max_tool_iterations
                if self.max_tool_iterations is not None
                else defaults.max_tool_iterations
            ),
            thinking=self.thinking if self.thinking is not None else defaults.thinking,
            responses_prefix_cache=(
                self.responses_prefix_cache
                if self.responses_prefix_cache is not None
                else defaults.responses_prefix_cache
            ),
            reasoning_effort=(
                self.reasoning_effort
                if self.reasoning_effort is not None
                else defaults.reasoning_effort
            ),
        )


class PostprocessAgentConfig(Base):
    """Async postprocess agent configuration."""

    enabled: bool = True
    model: str | None = None
    provider: str | None = None
    max_tokens: int | None = None
    context_window_tokens: int | None = None
    temperature: float | None = None
    max_tool_iterations: int | None = None
    thinking: bool | None = None
    responses_prefix_cache: bool | None = None
    reasoning_effort: str | None = None

    def resolve(self, defaults: AgentDefaults) -> AgentDefaults:
        """Resolve postprocess settings by falling back to the main agent defaults."""
        return AgentDefaults(
            workspace=defaults.workspace,
            model=self.model or defaults.model,
            provider=self.provider or defaults.provider,
            max_tokens=self.max_tokens if self.max_tokens is not None else defaults.max_tokens,
            context_window_tokens=(
                self.context_window_tokens
                if self.context_window_tokens is not None
                else defaults.context_window_tokens
            ),
            temperature=self.temperature if self.temperature is not None else defaults.temperature,
            max_tool_iterations=(
                self.max_tool_iterations
                if self.max_tool_iterations is not None
                else defaults.max_tool_iterations
            ),
            thinking=self.thinking if self.thinking is not None else defaults.thinking,
            responses_prefix_cache=(
                self.responses_prefix_cache
                if self.responses_prefix_cache is not None
                else defaults.responses_prefix_cache
            ),
            reasoning_effort=(
                self.reasoning_effort
                if self.reasoning_effort is not None
                else defaults.reasoning_effort
            ),
        )


class AgentsConfig(Base):
    """Agent configuration."""

    defaults: AgentDefaults = Field(default_factory=AgentDefaults)
    cron: CronAgentConfig = Field(default_factory=CronAgentConfig)
    heartbeat: HeartbeatAgentConfig = Field(default_factory=HeartbeatAgentConfig)
    postprocess: PostprocessAgentConfig = Field(default_factory=PostprocessAgentConfig)


class ProviderConfig(Base):
    """LLM provider configuration."""

    api_key: str = ""
    api_base: str | None = None
    extra_headers: dict[str, str] | None = None  # Custom headers (e.g. APP-Code for AiHubMix)


class ProvidersConfig(Base):
    """Configuration for LLM providers."""

    custom: ProviderConfig = Field(default_factory=ProviderConfig)  # Any OpenAI-compatible endpoint
    azure_openai: ProviderConfig = Field(default_factory=ProviderConfig)  # Azure OpenAI (model = deployment name)
    anthropic: ProviderConfig = Field(default_factory=ProviderConfig)
    openai: ProviderConfig = Field(default_factory=ProviderConfig)
    openrouter: ProviderConfig = Field(default_factory=ProviderConfig)
    deepseek: ProviderConfig = Field(default_factory=ProviderConfig)
    groq: ProviderConfig = Field(default_factory=ProviderConfig)
    zhipu: ProviderConfig = Field(default_factory=ProviderConfig)
    dashscope: ProviderConfig = Field(default_factory=ProviderConfig)
    vllm: ProviderConfig = Field(default_factory=ProviderConfig)
    ollama: ProviderConfig = Field(default_factory=ProviderConfig)  # Ollama local models
    gemini: ProviderConfig = Field(default_factory=ProviderConfig)
    moonshot: ProviderConfig = Field(default_factory=ProviderConfig)
    minimax: ProviderConfig = Field(default_factory=ProviderConfig)
    aihubmix: ProviderConfig = Field(default_factory=ProviderConfig)  # AiHubMix API gateway
    siliconflow: ProviderConfig = Field(default_factory=ProviderConfig)  # SiliconFlow (硅基流动)
    volcengine: ProviderConfig = Field(default_factory=ProviderConfig)  # VolcEngine (火山引擎)
    volcengine_coding_plan: ProviderConfig = Field(default_factory=ProviderConfig)  # VolcEngine Coding Plan
    byteplus: ProviderConfig = Field(default_factory=ProviderConfig)  # BytePlus (VolcEngine international)
    byteplus_coding_plan: ProviderConfig = Field(default_factory=ProviderConfig)  # BytePlus Coding Plan
    openai_codex: ProviderConfig = Field(default_factory=ProviderConfig)  # OpenAI Codex (OAuth)
    github_copilot: ProviderConfig = Field(default_factory=ProviderConfig)  # Github Copilot (OAuth)


class CronConfig(Base):
    """Cron scheduler configuration."""

    enabled: bool = True


class HeartbeatConfig(Base):
    """Heartbeat service configuration."""

    enabled: bool = True
    interval_s: int = 30 * 60  # 30 minutes
    poll_interval_s: int = 15
    max_concurrency: int = 4
    stagger_s: int = 5 * 60
    target_strategy: Literal["tenant-primary-session", "configured-session", "latest-external-session"] = "tenant-primary-session"
    target_tenant_key: str | None = None
    target_session_key: str | None = None
    target_channel: str | None = None
    target_chat_id: str | None = None
    busy_defer_s: int = 5 * 60
    recent_activity_cooldown_s: int = 2 * 60


class LaneLimitsConfig(Base):
    """Lane-based concurrency controls."""

    main: int = 8
    cron: int = 8
    heartbeat: int = 4
    subagent: int = 4
    main_backlog: int = 200
    main_per_tenant: int = 2


class PostprocessConfig(Base):
    """Deferred postprocess execution for record/write actions."""

    enabled: bool = True
    max_concurrency: int = 2
    defer_cron_writes: bool = True
    defer_heartbeat_writes: bool = True
    defer_memory_writes: bool = True
    structured_memory_enabled: bool = True
    structured_memory_recent_messages: int = 6


class GatewayConfig(Base):
    """Gateway/server configuration."""

    host: str = "0.0.0.0"
    port: int = 18790
    cron: CronConfig = Field(default_factory=CronConfig)
    heartbeat: HeartbeatConfig = Field(default_factory=HeartbeatConfig)
    lanes: LaneLimitsConfig = Field(default_factory=LaneLimitsConfig)
    postprocess: PostprocessConfig = Field(default_factory=PostprocessConfig)


RuntimeRole = Literal[
    "gateway",
    "api",
    "chat-api",
    "scheduler-service",
    "postprocess-worker",
    "background-worker",
]


class RedisRuntimeConfig(Base):
    """Redis coordination and task-queue settings."""

    enabled: bool = False
    url: str = "redis://127.0.0.1:6379/0"
    key_prefix: str = "nanobot"
    stream_prefix: str = "nanobot:tasks"
    consumer_block_ms: int = 5000
    consumer_batch_size: int = 8


class MySQLRuntimeConfig(Base):
    """MySQL connection settings for shared runtime state."""

    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 3306
    user: str = "root"
    password: str = ""
    database: str = "nanobot"
    charset: str = "utf8mb4"
    connect_timeout: int = 5


class TaskRuntimeConfig(Base):
    """Background task stream and reliability settings."""

    consumer_group: str = "nanobot"
    postprocess_stream: str = "postprocess"
    background_stream: str = "background"
    outbound_stream: str = "outbound"
    dead_letter_stream: str = "dead-letter"
    max_retry: int = 3
    ownership_ttl_s: int = 60
    dead_letter_on_max_retry: bool = True


class RuntimeConfig(Base):
    """Multi-service runtime configuration."""

    role: RuntimeRole = "gateway"
    redis: RedisRuntimeConfig = Field(default_factory=RedisRuntimeConfig)
    mysql: MySQLRuntimeConfig = Field(default_factory=MySQLRuntimeConfig)
    tasks: TaskRuntimeConfig = Field(default_factory=TaskRuntimeConfig)


class WebSearchConfig(Base):
    """Web search tool configuration."""

    provider: str = "brave"  # brave, tavily, duckduckgo, searxng, jina
    api_key: str = ""
    base_url: str = ""  # SearXNG base URL
    max_results: int = 5


class WebToolsConfig(Base):
    """Web tools configuration."""

    proxy: str | None = (
        None  # HTTP/SOCKS5 proxy URL, e.g. "http://127.0.0.1:7890" or "socks5://127.0.0.1:1080"
    )
    search: WebSearchConfig = Field(default_factory=WebSearchConfig)


class ExecToolConfig(Base):
    """Shell exec tool configuration."""

    timeout: int = 60
    path_append: str = ""


class MCPServerConfig(Base):
    """MCP server connection configuration (stdio or HTTP)."""

    type: Literal["stdio", "sse", "streamableHttp"] | None = None  # auto-detected if omitted
    command: str = ""  # Stdio: command to run (e.g. "npx")
    args: list[str] = Field(default_factory=list)  # Stdio: command arguments
    env: dict[str, str] = Field(default_factory=dict)  # Stdio: extra env vars
    url: str = ""  # HTTP/SSE: endpoint URL
    headers: dict[str, str] = Field(default_factory=dict)  # HTTP/SSE: custom headers
    tool_timeout: int = 30  # seconds before a tool call is cancelled


class ToolsConfig(Base):
    """Tools configuration."""

    web: WebToolsConfig = Field(default_factory=WebToolsConfig)
    exec: ExecToolConfig = Field(default_factory=ExecToolConfig)
    restrict_to_workspace: bool = False  # If true, restrict all tool access to workspace directory
    mcp_servers: dict[str, MCPServerConfig] = Field(default_factory=dict)


class Config(BaseSettings):
    """Root configuration for nanobot."""

    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    channels: ChannelsConfig = Field(default_factory=ChannelsConfig)
    providers: ProvidersConfig = Field(default_factory=ProvidersConfig)
    gateway: GatewayConfig = Field(default_factory=GatewayConfig)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)

    @property
    def workspace_path(self) -> Path:
        """Get expanded workspace path."""
        return Path(self.agents.defaults.workspace).expanduser()

    def _match_provider(
        self,
        model: str | None = None,
        provider_override: str | None = None,
    ) -> tuple["ProviderConfig | None", str | None]:
        """Match provider config and its registry name. Returns (config, spec_name)."""
        from nanobot.providers.registry import PROVIDERS

        forced = provider_override or self.agents.defaults.provider
        if forced != "auto":
            p = getattr(self.providers, forced, None)
            return (p, forced) if p else (None, None)

        model_lower = (model or self.agents.defaults.model).lower()
        model_normalized = model_lower.replace("-", "_")
        model_prefix = model_lower.split("/", 1)[0] if "/" in model_lower else ""
        normalized_prefix = model_prefix.replace("-", "_")

        def _kw_matches(kw: str) -> bool:
            kw = kw.lower()
            return kw in model_lower or kw.replace("-", "_") in model_normalized

        # Explicit provider prefix wins — prevents `github-copilot/...codex` matching openai_codex.
        for spec in PROVIDERS:
            p = getattr(self.providers, spec.name, None)
            if p and model_prefix and normalized_prefix == spec.name:
                if spec.is_oauth or spec.is_local or p.api_key:
                    return p, spec.name

        # Match by keyword (order follows PROVIDERS registry)
        for spec in PROVIDERS:
            p = getattr(self.providers, spec.name, None)
            if p and any(_kw_matches(kw) for kw in spec.keywords):
                if spec.is_oauth or spec.is_local or p.api_key:
                    return p, spec.name

        # Fallback: configured local providers can route models without
        # provider-specific keywords (for example plain "llama3.2" on Ollama).
        # Prefer providers whose detect_by_base_keyword matches the configured api_base
        # (e.g. Ollama's "11434" in "http://localhost:11434") over plain registry order.
        local_fallback: tuple[ProviderConfig, str] | None = None
        for spec in PROVIDERS:
            if not spec.is_local:
                continue
            p = getattr(self.providers, spec.name, None)
            if not (p and p.api_base):
                continue
            if spec.detect_by_base_keyword and spec.detect_by_base_keyword in p.api_base:
                return p, spec.name
            if local_fallback is None:
                local_fallback = (p, spec.name)
        if local_fallback:
            return local_fallback

        # Fallback: gateways first, then others (follows registry order)
        # OAuth providers are NOT valid fallbacks — they require explicit model selection
        for spec in PROVIDERS:
            if spec.is_oauth:
                continue
            p = getattr(self.providers, spec.name, None)
            if p and p.api_key:
                return p, spec.name
        return None, None

    def get_provider(
        self,
        model: str | None = None,
        provider_override: str | None = None,
    ) -> ProviderConfig | None:
        """Get matched provider config (api_key, api_base, extra_headers). Falls back to first available."""
        p, _ = self._match_provider(model, provider_override=provider_override)
        return p

    def get_provider_name(
        self,
        model: str | None = None,
        provider_override: str | None = None,
    ) -> str | None:
        """Get the registry name of the matched provider (e.g. "deepseek", "openrouter")."""
        _, name = self._match_provider(model, provider_override=provider_override)
        return name

    def get_api_key(
        self,
        model: str | None = None,
        provider_override: str | None = None,
    ) -> str | None:
        """Get API key for the given model. Falls back to first available key."""
        p = self.get_provider(model, provider_override=provider_override)
        return p.api_key if p else None

    def get_api_base(
        self,
        model: str | None = None,
        provider_override: str | None = None,
    ) -> str | None:
        """Get API base URL for the given model. Applies default URLs for gateway/local providers."""
        from nanobot.providers.registry import find_by_name

        p, name = self._match_provider(model, provider_override=provider_override)
        if p and p.api_base:
            return p.api_base
        # Only gateways get a default api_base here. Standard providers
        # (like Moonshot) set their base URL via env vars in _setup_env
        # to avoid polluting the global litellm.api_base.
        if name:
            spec = find_by_name(name)
            if spec and (spec.is_gateway or spec.is_local) and spec.default_api_base:
                return spec.default_api_base
        return None

    model_config = ConfigDict(env_prefix="NANOBOT_", env_nested_delimiter="__")
