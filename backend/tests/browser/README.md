# 浏览器回归

安装开发测试依赖 playwright，Windows测试使用已安装的Edge及虚拟麦克风。测试启动隔离数据库与模拟云服务，不会使用真实百炼凭证。

在项目根目录运行 python tests/browser/full_ui_test.py 和 python tests/browser/reliability_ui_test.py。Pages子路径测试先运行 python tools/build_github_pages.py，再运行 python tests/browser/pages_ui_test.py。

证据写入work；真实iPhone与三小时耐久不能由这些测试代替。
