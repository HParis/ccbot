"""Application entry point — CLI dispatcher and bot bootstrap.

Handles two execution modes:
  1. `ccbot hook` — delegates to hook.hook_main() for Claude Code hook processing.
  2. Default — configures logging, verifies iTerm2 connectivity, and starts
     the Telegram bot polling loop via bot.create_bot().
"""

import asyncio
import logging
import sys


def main() -> None:
    """Main entry point."""
    if len(sys.argv) > 1:
        if sys.argv[1] == "hook":
            from .hook import hook_main

            hook_main()
            return
        # Reject anything else: silently falling through to "start the bot"
        # means a typo (or `ccbot --help`) launches a second bot instance
        # that races the real one for Telegram updates.
        usage = "Usage: ccbot        start the Telegram bot\n       ccbot hook   run as Claude Code SessionStart hook"
        if sys.argv[1] in ("-h", "--help"):
            print(usage)
            return
        print(f"Unknown argument: {sys.argv[1]}\n{usage}", file=sys.stderr)
        sys.exit(2)

    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.WARNING,
    )

    # Import config before enabling DEBUG — avoid leaking debug logs on config errors
    try:
        from .config import config
    except ValueError as e:
        from .utils import ccbot_dir

        config_dir = ccbot_dir()
        env_path = config_dir / ".env"
        print(f"Error: {e}\n")
        print(f"Create {env_path} with the following content:\n")
        print("  TELEGRAM_BOT_TOKEN=your_bot_token_here")
        print("  ALLOWED_USERS=your_telegram_user_id")
        print()
        print("Get your bot token from @BotFather on Telegram.")
        print("Get your user ID from @userinfobot on Telegram.")
        sys.exit(1)

    logging.getLogger("ccbot").setLevel(logging.DEBUG)
    # AIORateLimiter (max_retries=5) handles retries itself; keep INFO for visibility
    logging.getLogger("telegram.ext.AIORateLimiter").setLevel(logging.INFO)
    # Surface polling retry-loop failures for debugging stuck-updater issues
    logging.getLogger("telegram.ext._utils.networkloop").setLevel(logging.DEBUG)
    logger = logging.getLogger(__name__)

    from .terminal.manager import terminal_manager

    logger.info("Allowed users: %s", config.allowed_users)
    logger.info("Claude projects path: %s", config.claude_projects_path)

    # Verify the terminal backend is reachable before starting the bot.
    # The bot can't do anything useful if its terminal isn't running, so
    # fail fast with a clear message. The connection here is bound to this
    # short-lived event loop; we reset it so the bot's own event loop opens
    # a fresh one.
    logger.info("Using terminal backend: %s", config.backend)
    logger.info("Verifying %s connectivity...", config.backend)
    try:
        asyncio.run(terminal_manager.preflight())
    except ConnectionError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        terminal_manager.reset_connection()
    logger.info("%s connection verified", config.backend)

    logger.info("Starting Telegram bot...")
    from .bot import create_bot

    application = create_bot()
    application.run_polling(
        allowed_updates=["message", "callback_query"],
        timeout=3,  # Telegram long-poll wait (default 10s → 3s for faster recovery)
    )


if __name__ == "__main__":
    main()
