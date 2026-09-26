#!/usr/bin/env python
import os
import sys


def main() -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "hoptalk_relay.settings")

    from django.core.management import execute_from_command_line

    from hoptalk_relay.database_pools import close_database_pools
    from hoptalk_relay.environment import InvalidConfigurationError

    # The message already names every wrong variable; a traceback would only bury it.
    try:
        execute_from_command_line(sys.argv)
    except InvalidConfigurationError as invalid_configuration_error:
        sys.stderr.write(f"{invalid_configuration_error}\n")
        sys.exit(2)
    finally:
        close_database_pools()


if __name__ == "__main__":
    main()
