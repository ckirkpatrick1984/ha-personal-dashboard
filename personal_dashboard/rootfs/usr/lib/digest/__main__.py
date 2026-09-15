"""Entry point: ``python3 -m digest --config /data/digest-config.json``."""

from __future__ import annotations

import argparse
import logging
import sys

from .service import load_config, serve


def main(argv=None):
    parser = argparse.ArgumentParser(prog="digest", description=__doc__)
    parser.add_argument("--config", default="/data/digest-config.json")
    parser.add_argument("--log-level", default="info")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Poll once, print the digest to stdout and exit. Use this to debug "
             "credentials without touching the dashboard.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="[%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )

    try:
        conf = load_config(args.config)
    except (OSError, ValueError) as exc:
        logging.error("cannot read config %s: %s", args.config, exc)
        return 2

    if not conf["accounts"]:
        logging.warning(
            "No accounts configured. The agenda and mail widgets will show a "
            "setup prompt until you add one."
        )

    if args.once:
        import json

        from .service import Digest

        digest = Digest(conf)
        digest.refresh()
        json.dump({"agenda": digest.agenda(), "mail": digest.mail()}, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0

    serve(conf)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
