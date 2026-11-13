import os


def test_no_vault_setting_reaches_a_test_from_the_shell() -> None:
    leaked = {name for name in os.environ if name.startswith(("VAULT_", "CLAUDE_PLUGIN_OPTION_"))}
    assert leaked == {"VAULT_CACHE_DIR", "VAULT_SIGNALS"}
