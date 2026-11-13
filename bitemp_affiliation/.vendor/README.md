# 本地依赖（离线）

运行环境没有系统级 duckdb 也没有 pip，因此把与解释器/架构匹配的
duckdb wheel 下载到本目录并解包：

- `duckdb-2.0.0-cp311-cp311-manylinux_2_26_aarch64.manylinux_2_28_aarch64.whl`
  ：从 PyPI 下载的原始 wheel（cp311 / aarch64），保留以便离线重新解包。
- `duckdb_pkg/`：上述 wheel 的解包目录，运行时由
  `bitemp/ledger/_bootstrap.py` 自动加入 `sys.path`。

若系统已安装 duckdb，`_bootstrap.ensure_vendor_duckdb()` 会优先使用系统
包，不会加载本目录。更换 Python 版本或架构时，下载对应 wheel 并重新
解包覆盖 `duckdb_pkg/` 即可（无网络的机器之间直接拷贝本目录也行）。
