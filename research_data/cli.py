"""研究资料命令行：原始摘要、分层加噪发布与账本查询。"""
import argparse
import json
import sys

from .domain import summarize
from .io import load
from .ledger import Ledger, LedgerError
from .publish import publish_release, recompute_diagnostics, RequestError


def _print(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False))


def _cmd_inspect(args):
    value, digest = load(args.file)
    _print({"source_sha256": digest, "summary": summarize(value)})


def _cmd_release(args):
    value, digest = load(args.file)
    with Ledger(args.ledger) as ledger:
        receipt = publish_release(value, digest, ledger)
    if args.receipt_out:
        with open(args.receipt_out, "w", encoding="utf-8") as handle:
            json.dump(receipt, handle, ensure_ascii=False, indent=2,
                      allow_nan=False)
    if args.recompute:
        receipt["independent_recompute"] = recompute_diagnostics(receipt)
    _print(receipt)


def _cmd_ledger(args):
    with Ledger(args.ledger) as ledger:
        status = ledger.scope_status(args.privacy_scope)
    if status is None:
        raise SystemExit(f"账本中没有 scope: {args.privacy_scope}")
    _print(status)


def main():
    parser = argparse.ArgumentParser(
        prog="research_data",
        description="合成研究资料的分层隐私计数发布工具",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_inspect = sub.add_parser("inspect", help="输出原始资料的本地摘要（不加噪）")
    p_inspect.add_argument("file")
    p_inspect.set_defaults(func=_cmd_inspect)

    p_release = sub.add_parser("release", help="预留预算并生成一次性分层加噪回执")
    p_release.add_argument("file", help="研究资料 JSON 路径")
    p_release.add_argument("--ledger", required=True, help="SQLite 账本路径")
    p_release.add_argument("--receipt-out", help="可选：回执同时写入该文件")
    p_release.add_argument("--recompute", action="store_true",
                           help="附上联机重算的目标与残差")
    p_release.set_defaults(func=_cmd_release)

    p_ledger = sub.add_parser("ledger-status", help="查看 scope 预算与发布清单")
    p_ledger.add_argument("--ledger", required=True)
    p_ledger.add_argument("--privacy-scope", required=True)
    p_ledger.set_defaults(func=_cmd_ledger)

    args = parser.parse_args()
    try:
        args.func(args)
    except (LedgerError, RequestError, ValueError) as exc:
        print(f"拒绝发布: {exc}", file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
