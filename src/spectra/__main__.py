"""エントリポイント。引数なしなら GUI、サブコマンド指定なら CLI として動作する。"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args:
        from spectra.cli import main as cli_main

        return cli_main(args)
    from spectra.ui.app import main as gui_main

    return gui_main()


if __name__ == "__main__":
    sys.exit(main())
