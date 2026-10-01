"""PaperAgent paperparse 供应商代码（ADR-0010 决议 5b/8）。

本目录不是供点分路径 import 的包：``reading.gateway`` 把本目录注入
``sys.path`` 后，其中的 ``paperparse/`` 以顶级包生效。文件与上游字节级一致
（见 ``SHA256SUMS``），**禁止在本目录内修改任何文件**——需要偏离上游时，
在 ``reading/`` 层包适配并在 NOTICE 登记。
"""
