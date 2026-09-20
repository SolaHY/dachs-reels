"""ストレージの切り替え口。

いまは GitHub バックエンドのみ（クレジットカード不要で完結させるため）。
別のストレージに移す場合はここで差し替える。
"""
from __future__ import annotations

from .ghstore import (  # noqa: F401
    StoreError,
    delete,
    exists,
    iter_json,
    list_names,
    move,
    public_url,
    read_bytes,
    read_json,
    read_range,
    release,
    rename_asset,
    signed_url,
    size,
    unmanaged_assets,
    write_bytes,
    write_json,
)
