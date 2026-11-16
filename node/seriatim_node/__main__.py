"""`python -m seriatim_node`: runs the sidecar's `main()` (from slice 1)."""

import importlib
import sys

NOT_YET = "seriatim-node: not implemented until slice 1"


def run() -> int:
    """Call `seriatim_node.app.main()`, or exit 2 while slice 1 has not landed."""
    try:
        app = importlib.import_module("seriatim_node.app")
    except ModuleNotFoundError as e:
        if e.name != "seriatim_node.app":
            raise
        print(NOT_YET, file=sys.stderr)
        return 2
    result = app.main()
    return result if isinstance(result, int) else 0


if __name__ == "__main__":
    sys.exit(run())
