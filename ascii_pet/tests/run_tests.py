"""运行全部测试：python tests/run_tests.py（需要 pytest；桌宠自带环境里已包含）。"""
import os
import sys

import pytest

here = os.path.dirname(os.path.abspath(__file__))
sys.exit(pytest.main([here, "-q", "-p", "no:cacheprovider"] + sys.argv[1:]))
