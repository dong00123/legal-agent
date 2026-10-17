"""站点适配器：把"不是标准 HTML 站点"的来源接进采集体系。

通用抽象（列表页 HTML + 正则抽链接 + DOM/正则取字段）适用于静态或半静态站点。
但有一类站点是 **SPA + JSON 接口驱动**的：列表在 XHR 里、正文是下载下来的文档、
结果项可能连 ``<a>`` 都没有。对这类站点，"HTML 列表 + 抽链接"那套完全用不上，
硬套只会得到"第 1 页就 done、新增 0 条"这类静默空跑。

适配器只做一件事：**把某个站点的接口语义翻译成统一的产物**（这里是
``data/raw/`` 下的规范命名文档），而随机延迟、重试、验证码转人工、代理、
单日配额这些横切能力，仍然由访问层（``backend.crawler.fetcher``）统一提供。
"""

from backend.crawler.sites.flk import FlkCollector, FlkOutcome, FlkRow

__all__ = ["FlkCollector", "FlkOutcome", "FlkRow"]
