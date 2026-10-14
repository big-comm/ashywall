import sys

from . import main

# Guarded so that importing this module (as worker processes may) never starts the app.
if __name__ == "__main__":
    sys.exit(main())
