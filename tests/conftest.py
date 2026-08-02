import os
import sys

# 讓測試能 import 專案根目錄的模組，不必在每個測試檔重複 sys.path 手續
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
