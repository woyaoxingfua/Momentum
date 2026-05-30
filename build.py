#!/usr/bin/env python3
"""
Momentum Task Agent 打包脚本
使用 PyInstaller 打包成独立可执行文件
"""

import subprocess
import sys
from pathlib import Path


def main():
    print("=" * 60)
    print("Momentum Task Agent - 打包脚本")
    print("=" * 60)
    print()

    # 检查是否安装了 PyInstaller
    try:
        import PyInstaller
        print(f"✓ PyInstaller 已安装 (版本: {PyInstaller.__version__})")
    except ImportError:
        print("✗ PyInstaller 未安装，正在安装...")
        subprocess.run([sys.executable, "-m", "pip", "install", "pyinstaller"], check=True)
        print("✓ PyInstaller 安装成功")
    print()

    # 检查 spec 文件是否存在
    spec_file = Path("momentum.spec")
    if not spec_file.exists():
        print("✗ 找不到 momentum.spec 文件！")
        return 1

    print(f"✓ 找到 spec 文件: {spec_file}")
    print()

    # 执行打包
    print("开始打包...")
    print("-" * 60)

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--clean",
        str(spec_file)
    ]

    try:
        result = subprocess.run(cmd, check=True)
        print("-" * 60)
        print()
        print("=" * 60)
        print("✓ 打包成功！")
        print("=" * 60)
        print()
        print("可执行文件位置:")
        print(f"  dist/momentum-agent")
        print()
        print("使用方式:")
        print(f"  ./dist/momentum-agent serve")
        print()
        return 0
    except subprocess.CalledProcessError as e:
        print("-" * 60)
        print()
        print(f"✗ 打包失败: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
