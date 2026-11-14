"""Existing Stagehand module environment for browser clients."""


def browser_environment(binary):
    return {"ARCHIVEBOX_STAGEHAND_MODULE": str(binary.loaded_abspath)}


def agent_environment(endpoint, binaries):
    """Connection settings for the registered Stagehand agent tool."""
    return {**browser_environment(binaries["stagehand"]), "BU_CDP_WS": endpoint}
