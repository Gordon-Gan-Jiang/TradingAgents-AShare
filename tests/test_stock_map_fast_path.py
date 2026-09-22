"""「读接口不触发冷加载」这条不变量。

## 为什么必须连 `_cn_stock_reverse_map` 一起控制

`_get_reverse_stock_map_cached_only()` 读的**不是** `_cn_stock_map`（name→code），
而是 `_cn_stock_reverse_map`（code→name）。此前这里只把 `_cn_stock_map` 置空，
于是在**单独运行**时通过（反向映射本来就没被填过），而在**全量运行**时失败：
更早的用例（或一次后台预热）已经把 `_cn_stock_reverse_map` 填好了，函数直接返回它，
断言 `== {}` 落空。

这就是"单独跑过、全量跑挂"的**顺序依赖**来源——不是被测代码有时序问题，
而是这个用例操纵了错的全局变量，从而依赖了别人的残留状态。

## 为什么必须掐掉后台刷新

冷路径会调用 `_schedule_stock_map_refresh()` 起一个守护线程去真正加载映射。
不掐掉它，这个用例就会**在断言之后**继续改写那两个全局变量，
把状态泄漏给后面的用例——顺序依赖就是这样在生产测试套件里互相传染的。
"""

from unittest.mock import patch


def test_reverse_stock_map_cached_only_does_not_trigger_cold_load():
    from api import main as main_mod

    original_map = main_mod._cn_stock_map
    original_reverse = main_mod._cn_stock_reverse_map
    try:
        # 冷缓存 = 两个映射都没填。
        main_mod._cn_stock_map = None
        main_mod._cn_stock_reverse_map = None
        with patch.object(main_mod, "_load_cn_stock_map", side_effect=AssertionError("slow load should not run")), \
             patch.object(main_mod, "_schedule_stock_map_refresh") as refresh:
            assert main_mod._get_reverse_stock_map_cached_only() == {}
            # 冷缓存下应当**安排**一次后台刷新（不阻塞），而不是自己去做加载。
            assert refresh.called, "冷缓存未安排后台刷新：接口会一直返回空映射"
    finally:
        main_mod._cn_stock_map = original_map
        main_mod._cn_stock_reverse_map = original_reverse


def test_reverse_stock_map_cached_only_uses_existing_cache():
    from api import main as main_mod

    original_map = main_mod._cn_stock_map
    original_reverse = main_mod._cn_stock_reverse_map
    try:
        main_mod._cn_stock_map = {
            "贵州茅台": "600519.SH",
            "宁德时代": "300750.SZ",
        }
        main_mod._cn_stock_reverse_map = {
            "600519.SH": "贵州茅台",
            "300750.SZ": "宁德时代",
        }
        assert main_mod._get_reverse_stock_map_cached_only() == {
            "600519.SH": "贵州茅台",
            "300750.SZ": "宁德时代",
        }
    finally:
        main_mod._cn_stock_map = original_map
        main_mod._cn_stock_reverse_map = original_reverse
