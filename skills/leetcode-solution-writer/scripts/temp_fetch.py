#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import sys
import json
import io
import os

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fetch_problem import fetch_using_leetcode_api

result = fetch_using_leetcode_api('8')
print(json.dumps(result, ensure_ascii=False, indent=2))