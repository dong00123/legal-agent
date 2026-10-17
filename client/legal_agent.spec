# -*- mode: python -*-
"""PyInstaller 打包配置（单文件 GUI）。

用法（项目根目录）：
    python -m PyInstaller client/legal_agent.spec --noconfirm
产物： ``dist/LegalAgent.exe``

几个刻意的选择：

- **``exclude`` 排掉 torch / sentence-transformers / pymilvus 等。**
  桌面端是瘦客户端，只用 httpx + PySide6。让 PyInstaller 自己分析的话，
  它会顺着 ``backend.*`` 的引用链把整个推理栈打进去 ——
  产物从 60MB 变成 3GB，启动还要解压半分钟。
- **产物名用 ASCII 的 ``LegalAgent``**，不用中文。
  Windows 上中文可执行文件名的编码问题（尤其是被脚本或 CI 处理时）
  不值得为好看冒。
- **``console=False``**：给律师用的是窗口程序，不该弹黑框。
  排查启动问题时临时改成 ``True`` 再看输出。
"""

from PyInstaller.utils.hooks import collect_submodules

# client 包下的模块 PyInstaller 未必能静态分析全（面板是在函数里 import 的）
hiddenimports = collect_submodules("client")

block_cipher = None

a = Analysis(
    ["client/entry.py"],
    pathex=["."],
    binaries=[],
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 服务端与推理栈：桌面端一个都不需要
        "torch", "transformers", "sentence_transformers", "numpy",
        "pymilvus", "langgraph", "langchain", "uvicorn", "fastapi",
        "sqlalchemy", "pymysql", "pymupdf", "docx", "whoosh", "jieba",
        "redis", "sklearn", "scipy", "pandas", "matplotlib",
        # 测试与文档工具
        "pytest", "PIL", "cv2",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="LegalAgent",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
