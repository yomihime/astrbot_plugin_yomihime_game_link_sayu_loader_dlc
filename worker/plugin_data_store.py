"""Profile-local paths exposed to plugins through gsuid_core.data_store."""

DIRECTORIES = {
    "image_res": "IMAGE_TEMP",
    "data_cache_path": "DATA_CACHE_PATH",
    "CORE_STATUS_CACHE_DIR": "data_cache/core_status",
    "backup_path": "GsCore_BACKUP_PATH",
    "gscore_data_path": "GsCore",
    "error_mark_path": "logs/error_reports",
    "CONFIGS_PATH": "configs",
    "PLUGINS_CONFIGS_PATH": "plugins_configs",
    "THEME_CONFIGS_PATH": "themes",
    "LIVE_CHAT_DIR": "webconsole_live_chat",
    "BRAND_DIR": "brand",
    "AI_CORE_PATH": "ai_core",
}

PATHS = {
    "THEME_CONFIG_PATH": "theme_config.json",
    "LOGS_CONFIG_PATH": "logs/configs/logs_config.json",
    "DIST_EX_PATH": "dist",
    "WEB_SESSIONS_PATH": "webconsole_sessions.json",
    "CHAT_PREFS_PATH": "webconsole_chat_prefs.json",
    "PLUGIN_PREFS_PATH": "webconsole_plugin_prefs.json",
    "GITHUB_AUTH_PATH": "webconsole_github.json",
    "GITHUB_STARS_CACHE_PATH": "webconsole_github_stars.json",
    "LIVE_CHAT_IDENTITY_PATH": "webconsole_live_chat/identity.json",
    "LIVE_CHAT_INDEX_PATH": "webconsole_live_chat/index.json",
    "LIVE_CHAT_CONVS_DIR": "webconsole_live_chat/conversations",
    "BRAND_CONFIG_PATH": "plugins_configs/brand.json",
    "BRAND_ICON_PATH": "brand/ICON.png",
}


def install_data_store(module, get_res_path):
    root = get_res_path()
    module.gs_data_path = module.RES = root
    module.get_res_path = get_res_path
    for key, directory in DIRECTORIES.items():
        setattr(module, key, get_res_path(directory))
    for key, relative in PATHS.items():
        setattr(module, key, root / relative)
