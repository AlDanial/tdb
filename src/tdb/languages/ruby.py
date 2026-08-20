"""The Ruby language profile (the bundled rdbg DAP bridge)."""

from __future__ import annotations

import sys
from typing import Any

from tdb.dap.types import Capabilities
from tdb.languages.base import (
    AdapterQuirks,
    AdapterSpec,
    LanguageNotSupportedError,
    LanguageProfile,
    Presentation,
    ProfileCapabilities,
)
from tdb.languages.errors import parse_ruby_error


class RdbgAdapter(AdapterSpec):
    """Bundled bridge for the DAP endpoint exposed by ``debug``/``rdbg``.

    ``vscode-rdbg`` is a VS Code extension, not a standalone executable.
    tdb instead starts its own stdio-to-TCP bridge, which launches ``rdbg
    --open`` and relays its native DAP connection.
    """

    id = "rdbg"

    # rdbg suspends the debuggee when an `evaluate`d expression raises
    # (see AdapterQuirks.suppress_exception_breakpoints_during_evaluate);
    # the controller must clear the catch breakpoint around evaluate or a
    # typo'd inspect deadlocks the session.
    quirks = AdapterQuirks(suppress_exception_breakpoints_during_evaluate=True)

    def __init__(self, rdbg_executable: str | None = None) -> None:
        """
        Args:
            rdbg_executable: ``rdbg`` executable path.  When omitted, the
                bundled bridge resolves it from ``PATH``.
        """
        self._rdbg = rdbg_executable

    def command(self) -> list[str]:
        """Start tdb's bridge; it validates ``rdbg`` when launching."""
        command = [sys.executable, "-m", "tdb.adapters.ruby"]
        if self._rdbg:
            command.extend(["--rdbg", self._rdbg])
        return command

    def launch_body(
        self,
        *,
        program: str,
        args: list[str],
        cwd: str,
        env: dict[str, str] | None,
        stop_on_entry: bool,
        console: str,
        opts: dict[str, Any],
    ) -> dict[str, Any]:
        """launch リクエストボディを構築。

        Ruby でのプログラム起動設定を DAP 形式で返す。
        """
        body: dict[str, Any] = {
            "type": "rdbg",
            "request": "launch",
            "program": program,
            "args": args,
            "cwd": cwd,
            "stopOnEntry": stop_on_entry,
            "console": console,
        }

        if env:
            body["env"] = env

        # Ruby 固有オプション
        if opts.get("show_protocol_messages"):
            # デバッグ用：DAP プロトコルメッセージ表示
            body["showProtocolMessages"] = True

        if opts.get("use_bundler"):
            # Bundler 経由で実行（Rails 対応）
            body["useBundler"] = True

        # リモートデバッグ用リスナーポート
        if opts.get("debug_port"):
            body["debugPort"] = opts["debug_port"]

        return body

    def attach_body(
        self, *, host: str, port: int, opts: dict[str, Any]
    ) -> dict[str, Any]:
        """attach リクエストボディを構築。

        実行中の Ruby プロセスへのリモート接続設定。
        """
        body: dict[str, Any] = {
            "type": "rdbg",
            "request": "attach",
            "host": host,
            "port": port,
        }

        if opts.get("path_mappings"):
            body["pathMappings"] = [
                {"localRoot": local, "remoteRoot": remote}
                for local, remote in opts["path_mappings"]
            ]

        return body

    def pick_exception_filters(self, caps: Capabilities) -> list[str]:
        """例外フィルター選択。

        Ruby では例外の分類が標準化されているため、主要なものを選択。
        """
        filters = []

        # 利用可能なフィルターをチェック
        if caps.exception_breakpoint_filters:
            filter_names = [f.get("filter") for f in caps.exception_breakpoint_filters]

            # debug.gem exposes ``any`` and exception-class filters (for
            # example ``RuntimeError``), rather than debugpy's names.
            if "any" in filter_names:
                filters.append("any")

        # フィルターが見つからない場合：アダプターのデフォルトを使用
        if not filters and caps.exception_breakpoint_filters:
            filters = [
                f["filter"]
                for f in caps.exception_breakpoint_filters
                if f.get("default")
            ]

        return filters


def build_ruby_profile(
    adapter: str | None = None, adapter_paths: dict[str, str] | None = None
) -> LanguageProfile:
    """Ruby 言語プロファイルを構築。

    Args:
        adapter: アダプター指定（"rdbg" のみサポート）
        adapter_paths: アダプターへのパスマッピング
            例: {"rdbg": "/path/to/rdbg"}

    Returns:
        Ruby 用 LanguageProfile インスタンス。

    Raises:
        LanguageNotSupportedError: 未知のアダプター指定時。
    """
    if adapter not in (None, "rdbg"):
        raise LanguageNotSupportedError(
            f"unknown adapter {adapter!r} for ruby (known: rdbg)"
        )

    return LanguageProfile(
        id="ruby",
        display_name="Ruby",
        adapter=RdbgAdapter(rdbg_executable=(adapter_paths or {}).get("rdbg")),
        presentation=Presentation(
            lexer="ruby",  # Pygments lexer
            parse_error=parse_ruby_error,
            frame_placeholder="<main>",
        ),
        capabilities=ProfileCapabilities(
            compute_step_units=None,  # Ruby では statement stepping 未対応
            child_process_strategy=None,  # 子プロセス追跡未対応
            task_inspection=False,  # asyncio なし
            pause_while_running=True,  # 実行中断対応
        ),
    )


RUBY_PROFILE = build_ruby_profile()
